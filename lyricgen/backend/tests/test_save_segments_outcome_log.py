"""Outcome metric del autosave (issue #934).

save-segments loguea outcome estructurado por tenant — éxito (INFO
"[save-segments] ok") y bloqueo por status (WARNING "outcome=409-status") —
para poder medir la tasa real de fallas del autosave desde los logs del
backend sin depender de los console.warn del browser.
"""
import logging
import uuid

from database import Job as JobModel, User as UserModel

_SEGS = {"segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}


def _mk_job(db, status="pending_review"):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=admin.id, tenant_id=admin.tenant_id,
        artist="T", song_title="Log", filename="t.mp3", status=status,
        segments_json=[], edit_count=0,
    ))
    db.commit()
    return job_id


def test_success_logs_ok_outcome(client, admin_token, db, caplog):
    job_id = _mk_job(db)
    try:
        with caplog.at_level(logging.INFO, logger="genly"):
            r = client.post(f"/jobs/{job_id}/save-segments", json=_SEGS,
                            headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200, r.text
        assert any(
            "[save-segments] ok" in m and job_id in m for m in caplog.messages
        ), caplog.messages
    finally:
        db.query(JobModel).filter(JobModel.job_id == job_id).delete(synchronize_session=False)
        db.commit()


def test_status_gate_logs_409_outcome(client, admin_token, db, caplog):
    job_id = _mk_job(db, status="processing")
    try:
        with caplog.at_level(logging.WARNING, logger="genly"):
            r = client.post(f"/jobs/{job_id}/save-segments", json=_SEGS,
                            headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 409, r.text
        assert any(
            "outcome=409-status" in m and job_id in m for m in caplog.messages
        ), caplog.messages
    finally:
        db.query(JobModel).filter(JobModel.job_id == job_id).delete(synchronize_session=False)
        db.commit()
