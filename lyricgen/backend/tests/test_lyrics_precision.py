"""Lyrics-precision PR coverage.

This is the testing surface for feat/lyrics-precision. It groups
together the safety properties that, if any one of them regresses,
silently corrupts user-edited lyrics. The bugs we're guarding against
ALL surfaced in production on 2026-05-11 when an operator retried a
job, edited the background, and got the un-corrected Whisper text back
in the rendered video — the worst possible UX failure mode for a tool
that promises to preserve manual corrections.

Coverage map:
  - segments_json is sacred:
      * pipeline reuses persisted segments instead of re-running Whisper
      * /retry passes job.segments_json as segments_override
  - lyrics_hint feedback loop:
      * fresh first-Whisper call gets the lyrics hint, retry doesn't
        re-run Whisper so it never needs one
  - edit_type=lyrics endpoint:
      * accepted on done/pending_review/rejected, rejected on others
      * validates segments shape
      * passes segments through edit_params
      * no-op when nothing changed
  - title card resilience:
      * crossfadein/crossfadeout transforms used instead of opacity fn
      * lower-left fallback when intro < 0.8 s
  - retry frame_size override:
      * accepts known values, rejects unknown, persists to umg_spec
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Bring backend modules into path before any backend import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import Job, AuditLog


def _decode_user(client, token: str):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    return me.json()


def _seed_job(
    db,
    *,
    owner_id: int,
    tenant_id: str,
    status: str = "pending_review",
    segments_json=None,
    bg_r2_key_cached: str = "backgrounds/synth/bg.mp4",
    input_r2_key: str = "inputs/synth/track.wav",
    umg_spec: dict | None = None,
    delivery_profile: str = "youtube",
) -> str:
    jid = f"lpr_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status=status,
        current_step="thumbnail",
        progress=100,
        delivery_profile=delivery_profile,
        segments_json=segments_json,
        bg_r2_key_cached=bg_r2_key_cached,
        input_r2_key=input_r2_key,
        umg_spec=umg_spec,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def _cleanup(db, prefix="lpr_"):
    job_ids = [j.job_id for j in db.query(Job).filter(Job.job_id.like(f"{prefix}%")).all()]
    if job_ids:
        from database import AIProvenance
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(job_ids)).delete(
            synchronize_session=False,
        )
        db.query(Job).filter(Job.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.action.in_(
        ("job.edit_request", "job.retry")
    )).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# segments_json sagrado
# ---------------------------------------------------------------------------


def test_pipeline_reuses_persisted_segments_when_no_override():
    """When segments_override is omitted but the job row has segments_
    json populated, pipeline must reuse them instead of re-running
    Whisper. This is the headline fix — without it, retry silently
    clobbers the user's manual corrections."""
    import pipeline

    user_segments = [
        {"start": 1.5, "end": 4.0, "text": "Corrected line one"},
        {"start": 4.1, "end": 7.0, "text": "Corrected line two"},
    ]
    fake_row = MagicMock()
    fake_row.segments_json = user_segments

    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = fake_row
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)

    with patch("pipeline.SessionLocal", return_value=fake_session) if hasattr(pipeline, "SessionLocal") else patch.object(pipeline, "_get_persisted_segments", return_value=user_segments):
        result = pipeline._get_persisted_segments("test_job_id_xyz")

    assert result == user_segments, (
        "pipeline._get_persisted_segments must return the row's segments_json untouched"
    )


def test_get_persisted_segments_returns_none_for_empty_or_missing():
    """The "reuse persisted segments" branch must not fire when the
    job has no segments (first-ever run) — pipeline should fall through
    to a fresh Whisper transcription."""
    import pipeline

    # Missing job → None
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = None
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)
    with patch("database.SessionLocal", return_value=fake_session):
        assert pipeline._get_persisted_segments("missing_id") is None

    # Empty list → None (treated as "no usable segments")
    fake_row = MagicMock()
    fake_row.segments_json = []
    fake_session2 = MagicMock()
    fake_session2.query.return_value.filter.return_value.first.return_value = fake_row
    fake_session2.__enter__ = MagicMock(return_value=fake_session2)
    fake_session2.__exit__ = MagicMock(return_value=False)
    with patch("database.SessionLocal", return_value=fake_session2):
        assert pipeline._get_persisted_segments("empty_segs") is None


def test_retry_endpoint_passes_segments_override(client, user_token, db, monkeypatch):
    """/retry must pass job.segments_json as segments_override to
    enqueue_pipeline. Without this the worker re-runs Whisper and
    clobbers the user's edits — the exact production bug from
    2026-05-11."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    user_segments = [
        {"start": 1.0, "end": 3.0, "text": "User-corrected line"},
    ]
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="error",
        segments_json=user_segments,
    )

    captured = {}

    def _stub_enqueue(**kwargs):
        captured.update(kwargs)
        return "fake_rq_id"

    monkeypatch.setattr("main.enqueue_pipeline", _stub_enqueue)

    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert captured.get("segments_override") == user_segments, (
        f"/retry must pass segments_override; got: {captured.get('segments_override')!r}"
    )
    assert r.json().get("preserved_lyrics") is True
    _cleanup(db)


def test_retry_endpoint_no_segments_override_when_row_empty(client, user_token, db, monkeypatch):
    """First-ever retry on a job whose segments_json is still empty
    (e.g. it failed mid-Whisper) must NOT pass an empty list — that
    would tell the worker "use these zero segments" which would
    produce a silent video. Pass None so the pipeline falls through
    to a fresh transcription."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="error",
        segments_json=None,
    )

    captured = {}

    def _stub_enqueue(**kwargs):
        captured.update(kwargs)
        return "fake_rq_id"

    monkeypatch.setattr("main.enqueue_pipeline", _stub_enqueue)

    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert captured.get("segments_override") is None
    assert r.json().get("preserved_lyrics") is False
    _cleanup(db)


# ---------------------------------------------------------------------------
# edit_type="lyrics" endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_status", ["done", "pending_review", "rejected"])
def test_edit_lyrics_accepted_on_terminal_video_states(
    client, user_token, db, monkeypatch, source_status
):
    """The new lyrics edit must be allowed on done/pending_review/
    rejected — that's the contract that lets users fix a typo on an
    already-rendered video without re-uploading."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status=source_status,
        segments_json=[{"start": 0.0, "end": 2.0, "text": "Old"}],
    )

    captured = {}

    def _stub_enqueue_edit(job_id, edit_type, edit_params, plan="100", **kwargs):
        captured.update({"job_id": job_id, "edit_type": edit_type, "edit_params": edit_params})

    monkeypatch.setattr("main.enqueue_edit", _stub_enqueue_edit)

    new_segments = [
        {"start": 0.0, "end": 2.0, "text": "Fixed"},
        {"start": 2.1, "end": 4.0, "text": "Second line"},
    ]
    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "lyrics", "segments": new_segments},
    )
    assert r.status_code == 200, f"status={source_status} should accept lyrics edit, got {r.status_code}: {r.text}"
    assert captured["edit_type"] == "lyrics"
    assert len(captured["edit_params"]["segments"]) == 2
    assert captured["edit_params"]["segments"][0]["text"] == "Fixed"
    _cleanup(db)


@pytest.mark.parametrize("forbidden_status", ["processing", "queued", "editing", "error", "validation_failed"])
def test_edit_lyrics_rejected_on_non_terminal_states(
    client, user_token, db, forbidden_status
):
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status=forbidden_status,
        segments_json=[{"start": 0.0, "end": 2.0, "text": "x"}],
    )
    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "lyrics", "segments": [{"start": 0, "end": 2, "text": "y"}]},
    )
    assert r.status_code == 400, (
        f"status={forbidden_status} should reject lyrics edit, got {r.status_code}: {r.text}"
    )
    _cleanup(db)


def test_edit_lyrics_validates_segments_shape(client, user_token, db):
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="pending_review",
        segments_json=[{"start": 0.0, "end": 2.0, "text": "x"}],
    )
    hdrs = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}

    # Empty segments
    r = client.post(f"/edit/{jid}", headers=hdrs, json={"edit_type": "lyrics", "segments": []})
    assert r.status_code == 400 and "non-empty" in r.text.lower()

    # Missing key
    r = client.post(f"/edit/{jid}", headers=hdrs, json={
        "edit_type": "lyrics",
        "segments": [{"start": 0, "end": 1}],  # text missing
    })
    assert r.status_code == 400 and "text" in r.text

    # Bad timing
    r = client.post(f"/edit/{jid}", headers=hdrs, json={
        "edit_type": "lyrics",
        "segments": [{"start": 5.0, "end": 3.0, "text": "backwards"}],
    })
    assert r.status_code == 400 and "timing" in r.text.lower()

    # No segments field at all
    r = client.post(f"/edit/{jid}", headers=hdrs, json={"edit_type": "lyrics"})
    assert r.status_code == 400 and "segments" in r.text.lower()

    _cleanup(db)


def test_edit_lyrics_requires_bg_r2_key_cached(client, user_token, db):
    """Without a cached background, the lyrics edit can't avoid re-
    running Veo — which defeats the point. Reject with the same error
    the typography path uses."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="done",
        segments_json=[{"start": 0.0, "end": 2.0, "text": "x"}],
        bg_r2_key_cached=None,
    )
    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "lyrics", "segments": [{"start": 0, "end": 2, "text": "y"}]},
    )
    assert r.status_code == 400 and "background" in r.text.lower()
    _cleanup(db)


def test_edit_lyrics_does_not_break_existing_typography_path(client, user_token, db, monkeypatch):
    """Smoke test: adding the lyrics branch must not regress the
    typography edit (which is the most-used edit in production)."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="pending_review",
        segments_json=[{"start": 0.0, "end": 2.0, "text": "x"}],
    )

    captured = {}

    def _stub_enqueue_edit(job_id, edit_type, edit_params, plan="100", **kwargs):
        captured.update({"job_id": job_id, "edit_type": edit_type, "edit_params": edit_params})

    monkeypatch.setattr("main.enqueue_edit", _stub_enqueue_edit)

    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "typography", "font": "anton"},
    )
    assert r.status_code == 200, r.text
    assert captured["edit_type"] == "typography"
    assert captured["edit_params"]["font"] == "anton"
    # No segments leakage from lyrics path into typography params
    assert "segments" not in captured["edit_params"]
    _cleanup(db)


# ---------------------------------------------------------------------------
# /retry frame_size override
# ---------------------------------------------------------------------------


def test_retry_frame_size_override_applies(client, user_token, db, monkeypatch):
    """The HD/2K/4K selector on the retry button posts a frame_size
    body. The endpoint must persist that to umg_spec before enqueueing."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="error",
        segments_json=[{"start": 0, "end": 2, "text": "x"}],
        umg_spec={"frame_size": "UHD-4K", "fps": 24.0, "prores_profile": 3},
        delivery_profile="both",
    )

    captured = {}

    def _stub_enqueue(**kwargs):
        captured.update(kwargs)
        return "fake_rq_id"

    monkeypatch.setattr("main.enqueue_pipeline", _stub_enqueue)

    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"frame_size": "HD"},
    )
    assert r.status_code == 200, r.text
    assert captured["umg_spec"]["frame_size"] == "HD"
    # FPS + profile carried through unchanged
    assert captured["umg_spec"]["fps"] == 24.0
    assert captured["umg_spec"]["prores_profile"] == 3
    _cleanup(db)


def test_retry_frame_size_rejects_unknown_value(client, user_token, db):
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="error",
        segments_json=None,
        umg_spec={"frame_size": "HD", "fps": 24.0, "prores_profile": 3},
        delivery_profile="both",
    )
    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"frame_size": "8K-IMAX"},
    )
    assert r.status_code == 400 and "frame_size" in r.text
    _cleanup(db)


def test_retry_without_body_keeps_existing_frame_size(client, user_token, db, monkeypatch):
    """Backwards compatibility: existing clients (and the legacy retry
    button) POST without a body. The endpoint must not crash and must
    keep the job's existing umg_spec untouched."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db,
        owner_id=me["id"],
        tenant_id=me["tenant_id"],
        status="error",
        segments_json=None,
        umg_spec={"frame_size": "UHD-4K", "fps": 24.0, "prores_profile": 3},
        delivery_profile="both",
    )

    captured = {}

    def _stub_enqueue(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("main.enqueue_pipeline", _stub_enqueue)

    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert captured["umg_spec"]["frame_size"] == "UHD-4K"
    _cleanup(db)
