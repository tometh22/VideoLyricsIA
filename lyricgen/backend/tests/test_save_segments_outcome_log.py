"""Outcome metric del autosave (issue #934).

save-segments loguea outcome estructurado por tenant — éxito (INFO
"[save-segments] ok") y bloqueo por status (WARNING "outcome=409-status") —
para poder medir la tasa real de fallas del autosave desde los logs del
backend sin depender de los console.warn del browser.
"""
import logging
import json
import uuid

from database import AuditLog, Job as JobModel, User as UserModel

_SEGS = {"segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}


def _mk_job(db, status="pending_review", *, machine_snapshot_required=False):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=admin.id, tenant_id=admin.tenant_id,
        artist="T", song_title="Log", filename="t.mp3", status=status,
        segments_json=[], edit_count=0,
        machine_snapshot_required=machine_snapshot_required,
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


def test_segment_diff_never_persists_raw_lyrics(client, admin_token, db):
    secret_before = "sentinel lyric before private"
    secret_after = "sentinel lyric after private"
    job_id = _mk_job(db, machine_snapshot_required=True)
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    row.segments_json = [{"start": 0.0, "end": 1.0, "text": secret_before}]
    db.commit()
    audit_id = None
    try:
        response = client.post(
            f"/jobs/{job_id}/save-segments",
            json={"segments": [{"start": 0.0, "end": 1.0, "text": secret_after}]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 200, response.text
        audit = db.query(AuditLog).filter(
            AuditLog.action == "lyrics.segments_diff",
        ).order_by(AuditLog.id.desc()).first()
        audit_id = audit.id
        encoded = json.dumps(audit.detail, sort_keys=True)
        assert secret_before not in encoded and secret_after not in encoded
        assert audit.detail["schema"] == "editor-line-delta-v2"
        change = audit.detail["changes"][0]
        assert "text" not in (change["before"] or {})
        assert "text" not in (change["after"] or {})
        assert change["fields"]["text"] is True
        for side in ("before", "after"):
            value = change[side]["text_hmac"]
            assert value is None or len(value) == 64
    finally:
        if audit_id is not None:
            db.query(AuditLog).filter(AuditLog.id == audit_id).delete(
                synchronize_session=False,
            )
        db.query(JobModel).filter(JobModel.job_id == job_id).delete(synchronize_session=False)
        db.commit()
