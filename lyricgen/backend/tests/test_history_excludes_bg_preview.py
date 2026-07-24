"""Regression test: bg_preview ghost jobs don't leak into the user's history.

The Capa C feature (2026-05-24, /generate-preview) creates a "ghost" job row
with status `bg_preview_*` to let the wizard poll the Veo background
pre-render while the operator edits lyrics. Those rows shadow the real
render in the Historial — the user reported seeing "Hermanos De Sangre"
twice: once as `processing` (the real generate) and once as `bg_preview_done`
(the ghost). The fix filters bg_preview_* out of get_all_jobs, the
function that backs the user-facing /jobs endpoint.
"""

from datetime import datetime, timedelta, timezone

from database import Job, SessionLocal
from jobs import get_all_jobs

_T = "tenant_bgpreview_history_test"


def _seed(db, *, job_id, status, age_min, user_id=1, song_title="Hermanos De Sangre"):
    db.add(Job(
        job_id=job_id, user_id=user_id, tenant_id=_T,
        artist="Viejas Locas", song_title=song_title,
        filename=f"{job_id}.mp3", style="rock", status=status,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    ))
    db.commit()


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


def test_get_all_jobs_excludes_bg_preview_ghosts():
    db = SessionLocal()
    try:
        _cleanup(db)
        # Real render kicked off by /generate.
        _seed(db, job_id="real0001", status="processing", age_min=6)
        # Ghost row created by /generate-preview, promoted by the bg worker.
        _seed(db, job_id="ghost001", status="bg_preview_done", age_min=4)
        # Other ghost states the worker can leave behind.
        _seed(db, job_id="ghost002", status="bg_preview_queued", age_min=3)
        _seed(db, job_id="ghost003", status="bg_preview_generating", age_min=3)
        _seed(db, job_id="ghost004", status="bg_preview_failed", age_min=3)

        rows = get_all_jobs(db, tenant_id=_T, user_id=1)
        ids = {r["job_id"] for r in rows}

        assert ids == {"real0001"}, (
            f"Expected only the real render. Ghost rows leaked: "
            f"{ids - {'real0001'}}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_get_all_jobs_still_returns_normal_states():
    """Sanity check — the filter doesn't accidentally hide real work."""
    db = SessionLocal()
    try:
        _cleanup(db)
        for jid, status in (
            ("done0001", "done"),
            ("proc0001", "processing"),
            ("que00001", "queued"),
            ("err00001", "error"),
            ("draft001", "transcribed_pending"),
            ("trans001", "transcribed"),
            ("editg001", "editing"),
        ):
            _seed(db, job_id=jid, status=status, age_min=1)

        rows = get_all_jobs(db, tenant_id=_T, user_id=1)
        ids = {r["job_id"] for r in rows}

        assert ids == {
            "done0001", "proc0001", "que00001", "err00001",
            "draft001", "trans001", "editg001",
        }
    finally:
        _cleanup(db)
        db.close()
