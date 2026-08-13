"""Retry / edit / variant must fail fast when the source audio is gone.

Incident 2026-05-19 (staging, agus.cafisi / Una Vez Más):
  1. Job had a successful initial render.
  2. A failed variant of the same audio was deleted from the UI.
  3. Pre-Bug-A-fix code path: variant delete nuked the shared
     input_r2_key from R2 (no reference counting).
  4. User clicked "Reintentar sin re-subir" on a sibling job.
  5. /retry passed all DB-level checks (input_r2_key column was still
     set), enqueued the job, and the worker crashed 30 s into the run
     with `Could not download source audio from R2`.
  6. Operator saw "El video falló" with no actionable hint.

Bug A (jobs._delete_r2_objects sibling-count) fixes the root cause —
the audio is no longer nuked. Bug B is the belt-and-suspenders: every
endpoint that re-uses input_r2_key (retry, edit, variant) does a HEAD
on R2 first and returns 422 with a clear "audio is gone, re-upload"
message instead of letting the worker fail loud minutes later.

These tests pin the pre-check behavior on each of the three endpoints.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _seed_done_job(db, *, status: str, owner_id: int, tenant_id: str,
                   input_r2_key: str | None = "inputs/t/j/audio.wav",
                   bg_cached: str | None = "bg/cache/key.mp4",
                   segments=None) -> str:
    """Insert a Job row with the fields needed for retry/edit/variant
    to reach the R2 pre-check. Different endpoints need different
    starting statuses (validation_failed for retry, pending_review for
    edit, done for variant)."""
    from database import Job
    jid = f"r2pre_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        filename="x.wav",
        style="oscuro",
        status=status,
        progress=100 if status == "done" else 0,
        current_step="thumbnail" if status == "done" else "video",
        delivery_profile="youtube",
        input_r2_key=input_r2_key,
        bg_r2_key_cached=bg_cached,
        segments_json=segments or [{"start": 0.0, "end": 1.0, "text": "hi"}],
        created_at=datetime.now(timezone.utc),
        last_progress_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def _patch_object_exists(monkeypatch, exists: bool):
    """Stub storage.object_exists so the endpoint sees R2 as present
    or absent without hitting Cloudflare. Also stubs is_enabled to
    True — without R2 enabled the pre-check is skipped entirely, so
    we can't assert its behavior."""
    import storage
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "object_exists", lambda _key: exists)


# ---------------------------------------------------------------------------
# /retry/{job_id}
# ---------------------------------------------------------------------------


def test_retry_returns_422_when_input_r2_key_missing_from_r2(
    client, user_token, db, monkeypatch,
):
    """The DB column says "audio at X" but R2 returns 404. /retry must
    fail loud with 422 + a re-upload hint instead of enqueueing a job
    that will crash 30 s in."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    job_id = _seed_done_job(
        db, status="error", owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    _patch_object_exists(monkeypatch, exists=False)

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 422, res.text
    body = res.json()
    msg = (body.get("detail") or "").lower()
    assert "storage" in msg or "audio" in msg, (
        f"422 detail must explain audio is gone, got: {body!r}"
    )
    # Must mention the recovery action so the operator isn't left guessing.
    assert "subí" in msg or "re-subir" in msg or "mp3" in msg, (
        f"422 detail must hint the operator to re-upload, got: {body!r}"
    )


def test_retry_succeeds_when_input_r2_key_present(
    client, user_token, db, monkeypatch,
):
    """Sanity: when the R2 object is present, /retry must NOT 422 on
    the pre-check (it would proceed to enqueue, which we stub)."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    job_id = _seed_done_job(
        db, status="error", owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    _patch_object_exists(monkeypatch, exists=True)

    # Stub enqueue so we don't need Redis. Returns the same job_id
    # convention the real enqueue_pipeline uses.
    import main
    monkeypatch.setattr(main, "enqueue_pipeline", lambda **kw: kw["job_id"])

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("status") == "processing"
    assert body.get("job_id") == job_id


def test_retry_proceeds_when_storage_probe_throws(
    client, user_token, db, monkeypatch,
):
    """Storage probe error (network blip, credential failure) must not
    block the operator. Worker will fail loud if the file is truly
    missing — better than stranding the operator on a transient probe
    error."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    job_id = _seed_done_job(
        db, status="error", owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    import storage

    def _boom(_key):
        raise RuntimeError("R2 probe network timeout")

    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "object_exists", _boom)

    import main
    monkeypatch.setattr(main, "enqueue_pipeline", lambda **kw: kw["job_id"])

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    # Probe error → permissive: retry proceeds.
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# /edit/{job_id}
# ---------------------------------------------------------------------------


def test_edit_returns_422_when_input_r2_key_missing_from_r2(
    client, user_token, db, monkeypatch,
):
    """Same protection on /edit. Background/typography/lyrics edits all
    download the source audio — pre-check belongs here too."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    job_id = _seed_done_job(
        db, status="pending_review", owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    _patch_object_exists(monkeypatch, exists=False)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"edit_type": "typography", "font": "anton"},
    )
    assert res.status_code == 422, res.text
    msg = (res.json().get("detail") or "").lower()
    assert "storage" in msg or "audio" in msg


# ---------------------------------------------------------------------------
# /jobs/{parent}/variant
# ---------------------------------------------------------------------------


def test_variant_returns_422_when_parent_input_r2_key_missing_from_r2(
    client, user_token, db, monkeypatch,
):
    """Variant inherits parent.input_r2_key. If the parent's audio is
    gone, the variant would be dead-on-arrival."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    parent_id = _seed_done_job(
        db, status="done", owner_id=me["id"], tenant_id=me["tenant_id"],
    )

    _patch_object_exists(monkeypatch, exists=False)

    res = client.post(
        f"/jobs/{parent_id}/variant",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"background_hint": "abstract neon"},
    )
    assert res.status_code == 422, res.text
    msg = (res.json().get("detail") or "").lower()
    assert "audio" in msg
