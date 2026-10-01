"""End-to-end threading tests for the title_song_break field (UI v1.1, 2026-05-30).

The unit tests in test_ass_render.py cover the leaf — title_card_lines(song_lines=...)
respects/falls back/preserves-legacy correctly. This file covers the WHOLE
plumbing chain from the HTTP API down to render_params persistence and the
heritable-render-params whitelist, because the leaf can be right and the
field can still never reach it (typo in a Form param, missing entry in the
heritable list, the /edit handler forgetting to persist it, etc.).

Why this matters: the title_song_break field is gated. The backend MUST
treat empty string / unset / absent identically to "no change". A regression
in any of the 9 threading sites would silently produce a different render
for jobs that don't even use the new field.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

# Bring backend modules into path before any backend import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import Job


# ---------------------------------------------------------------------------
# Seed helper — mirrors test_lyrics_precision._seed_job at the field level
# we need (pending_review with a usable bg cache). Kept inline to avoid a
# fragile import dependency on the lyrics-precision test module.
# ---------------------------------------------------------------------------
def _seed_pending_review_job(
    db,
    *,
    owner_id: int,
    tenant_id: str,
    render_params: dict | None = None,
) -> str:
    """Insert a Job in pending_review that satisfies /edit's pre-checks."""
    jid = f"tsb_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        song_title="Test Song",
        filename="x.mp3",
        style="oscuro",
        status="pending_review",
        current_step="thumbnail",
        progress=100,
        delivery_profile="youtube",
        segments_json=[{"start": 0.0, "end": 2.0, "text": "x"}],
        bg_r2_key_cached="backgrounds/synth/bg.mp4",
        input_r2_key="inputs/synth/track.wav",
        render_params=render_params,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def _cleanup(db, prefix="tsb_"):
    job_ids = [j.job_id for j in db.query(Job).filter(Job.job_id.like(f"{prefix}%")).all()]
    if job_ids:
        from database import AIProvenance
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(job_ids)).delete(
            synchronize_session=False,
        )
        db.query(Job).filter(Job.job_id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()


def _decode_user(client, token: str):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()


def _capture_enqueue_edit(monkeypatch):
    """Replace enqueue_edit with a capturing stub so we can assert on the
    edit_params + plan that the endpoint would have queued. Returns the
    captured-kwargs list."""
    captured: list[dict] = []
    def _stub(job_id, edit_type, edit_params, plan="100", **kwargs):
        captured.append({
            "job_id": job_id,
            "edit_type": edit_type,
            "edit_params": dict(edit_params),
            "plan": plan,
            **kwargs,
        })
    monkeypatch.setattr("main.enqueue_edit", _stub)
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_edit_endpoint_persists_title_song_break_to_render_params(
    client, user_token, db, monkeypatch,
):
    """POST /edit with title_song_break must write the new value to
    Job.render_params (so future retry/variant/typography edits see it) AND
    forward it via edit_params (so THIS render uses it). Pinning both so a
    regression in either path can't slip through."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
    )
    captured = _capture_enqueue_edit(monkeypatch)

    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={
            "edit_type": "typography",
            "title_song_break": "Donde Estan\nCorazón",
        },
    )
    assert r.status_code == 202, r.text

    # Forwarded to the worker
    assert len(captured) == 1
    assert captured[0]["edit_type"] == "typography"
    assert captured[0]["edit_params"]["title_song_break"] == "Donde Estan\nCorazón"

    # Persisted in render_params (durable across edits/retries/variants)
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == jid).first()
    assert row.render_params is not None
    assert row.render_params.get("title_song_break") == "Donde Estan\nCorazón"
    _cleanup(db)


def test_edit_endpoint_without_title_song_break_leaves_render_params_intact(
    client, user_token, db, monkeypatch,
):
    """If the body doesn't include title_song_break, the /edit handler must
    NOT touch render_params.title_song_break. Otherwise a typography edit
    that only changes the font would silently wipe a previously-set manual
    break."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        # Pre-set a manual break so we can confirm it survives.
        render_params={"title_song_break": "Pre\nExisting"},
    )
    captured = _capture_enqueue_edit(monkeypatch)

    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        # No title_song_break — only a font change.
        json={"edit_type": "typography", "font": "anton"},
    )
    assert r.status_code == 202, r.text
    # edit_params doesn't carry the field (it wasn't asked to change)
    assert "title_song_break" not in captured[0]["edit_params"]
    # And the pre-set value in render_params is untouched
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == jid).first()
    assert row.render_params.get("title_song_break") == "Pre\nExisting"
    _cleanup(db)


def test_edit_endpoint_can_clear_title_song_break_with_empty_string(
    client, user_token, db, monkeypatch,
):
    """Operator wants to REVERT from a manual break back to auto-wrap.
    Sending title_song_break='' (explicit empty string, NOT None) must
    overwrite the previously-stored manual break."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"title_song_break": "Old\nBreak"},
    )
    captured = _capture_enqueue_edit(monkeypatch)

    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "typography", "title_song_break": ""},
    )
    assert r.status_code == 202, r.text
    assert captured[0]["edit_params"]["title_song_break"] == ""
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == jid).first()
    # Cleared (empty string IS a deliberate value).
    assert row.render_params.get("title_song_break") == ""
    _cleanup(db)


def test_edit_endpoint_rejects_title_song_break_over_200_chars(
    client, user_token, db,
):
    """EditJobRequest.title_song_break has max_length=200. A longer value
    must be rejected by Pydantic with a 422 — this prevents accidental
    storage of giant strings that would later overflow the title card or
    bloat render_params."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    too_long = "A" * 201
    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"edit_type": "typography", "title_song_break": too_long},
    )
    assert r.status_code == 422, r.text
    _cleanup(db)


def test_retry_inherits_title_song_break_from_render_params(
    client, user_token, db, monkeypatch,
):
    """/retry rebuilds pipeline kwargs from the stored render_params. The
    title_song_break key MUST be in the heritable whitelist (main.py:8504+)
    so the retry produces the same title-card layout as the original
    render. Without this, retrying a job with a manual break would silently
    fall back to auto-wrap."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    # Seed with render_params already carrying the break — simulates a job
    # that was originally rendered with title_song_break set, then errored.
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={
            "title_song_break": "Linea\nDos",
            "font": "anton",
        },
    )
    # Move to a status that /retry accepts (error or pending_review work).
    row = db.query(Job).filter(Job.job_id == jid).first()
    row.status = "error"
    row.error = "fake error to enable retry"
    db.commit()

    captured = {}
    def _stub_enqueue_pipeline(**kwargs):
        captured.update(kwargs)
    monkeypatch.setattr("main.enqueue_pipeline", _stub_enqueue_pipeline)

    r = client.post(
        f"/retry/{jid}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    # The new manual break is forwarded to the worker as a pipeline kwarg.
    assert captured.get("title_song_break") == "Linea\nDos"
    _cleanup(db)


def test_render_params_includes_title_song_break_after_explicit_set(
    client, user_token, db, monkeypatch,
):
    """End-to-end persistence sanity: after the /edit handler runs, the
    JSONB column round-trips the value as-is — UTF-8 + newline survive."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_pending_review_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
    )
    _capture_enqueue_edit(monkeypatch)

    # Includes an accented char + newline — exactly the field's intended use.
    payload = {"edit_type": "typography", "title_song_break": "El Árbol\nDe La Vida"}
    r = client.post(
        f"/edit/{jid}",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json=payload,
    )
    assert r.status_code == 202, r.text
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == jid).first()
    assert row.render_params["title_song_break"] == "El Árbol\nDe La Vida"
    # And the newline survived JSONB serialization (not escaped to \\n).
    assert "\n" in row.render_params["title_song_break"]
    _cleanup(db)
