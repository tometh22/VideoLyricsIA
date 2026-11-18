"""Rejected editor operations release row locks before dependency cleanup."""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from database import EditorDocument, SessionLocal
from tests.test_editor_documents import _users_and_job


@pytest.mark.parametrize("operation,expected", [
    ("lock_actor", 403), ("activity_expired", 409), ("activity_sequence", 409),
    ("velocity", 429), ("restore_invalid", 422), ("bridge_invalid", 422),
])
def test_editor_rejections_release_transaction(operation, expected, monkeypatch):
    import main

    user, _, job_id = _users_and_job("reject_audit6")
    actor = {"id": user.id, "role": "user", "tenant_id": user.tenant_id,
             "features": {"editor_v2": True}}
    def invalid(*args, **kwargs):
        raise ValueError("invalid_editor_operation")
    with SessionLocal() as db:
        if operation == "lock_actor":
            monkeypatch.setattr(main, "assert_pilot_actor", invalid)
            call = main.editor_lock(job_id, x_editor_session="session-audit6", current_user=actor, db=db)
        elif operation.startswith("activity_"):
            document = db.query(EditorDocument).filter_by(job_id=job_id).one()
            document.lock_user_id = user.id
            document.lock_expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=-1 if operation == "activity_expired" else 5)
            db.commit()
            body = main.EditorActivityHeartbeatRequest(session_id="audit-session-000006", activity_seq=9, task="text")
            call = inspect.unwrap(main.editor_activity_heartbeat)(
                Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
                job_id, body, current_user=actor, db=db)
        elif operation == "velocity":
            import anchor_structural_guard
            monkeypatch.setattr(anchor_structural_guard, "segment_write_velocity_exceeded", lambda *_: (True, 3, 2))
            call = main.patch_editor_document(job_id,
                main.EditorPatchRequest(base_revision=0, segments=[{"start": 0, "end": 1, "text": "unchanged"}]),
                current_user=actor, db=db)
        elif operation == "restore_invalid":
            monkeypatch.setattr(main, "restore_version", invalid)
            call = main.restore_editor_version(job_id,
                main.EditorRestoreRequest(version_id="invalid", base_revision=0), current_user=actor, db=db)
        else:
            bridge = main.get_or_create_document
            def invalid_bridge(*args, **kwargs):
                bridge(*args, **kwargs)  # Take the real Job/Document locks first.
                raise ValueError("invalid legacy segments")
            monkeypatch.setattr(main, "get_or_create_document", invalid_bridge)
            call = main.get_editor_document(job_id, current_user=actor, db=db)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(call)
        assert caught.value.status_code == expected
        assert not db.in_transaction()
