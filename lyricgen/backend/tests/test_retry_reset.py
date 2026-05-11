"""Tests para POST /retry/{job_id}:
1. Resetea job.edit_count a 0 (sin esto job stuck en límite tras retry).
2. AuditLog captura previous_status REAL (no "processing" tras mutate).
"""

import uuid

from database import Job as JobModel, AuditLog


def _create_error_job(db, edit_count: int = 3, tenant_id: str = "default"):
    """Crea job en status=error con input_r2_key para que /retry pase
    los checks de retryability (status in ('error','validation_failed')
    + input_r2_key presente)."""
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="Retry Test",
        status="error",
        delivery_profile="youtube",
        progress=42,
        edit_count=edit_count,
        error="Worker died mid-render",
        input_r2_key="inputs/test/track.wav",  # required para retry
    )
    db.add(job)
    db.commit()
    return job_id


def test_retry_resets_edit_count_to_zero(client, admin_token, db):
    """Job en error con edit_count=3 → /retry → edit_count=0 en DB.
    Sin este reset, el job retried queda bloqueado para nuevos edits
    porque el límite ya está consumido."""
    job_id = _create_error_job(db, edit_count=3)

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:200]}"

    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh is not None
    assert fresh.edit_count == 0, (
        f"edit_count debería resetearse a 0 tras retry, quedó en {fresh.edit_count}. "
        f"Sin esto el job no puede ser re-editado."
    )
    assert fresh.status == "processing"


def test_retry_audit_log_captures_actual_previous_status(client, admin_token, db):
    """AuditLog debe registrar `previous_status` ANTES del mutate.
    Bug viejo: capturaba job.status después de mutarlo a "processing",
    así que TODOS los retry logs decían previous_status="processing"
    (inservible para forensics)."""
    job_id = _create_error_job(db, edit_count=0)

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200

    db.expire_all()
    log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.retry")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log is not None, "AuditLog entry for retry no se creó"
    assert log.detail.get("job_id") == job_id
    assert log.detail.get("previous_status") == "error", (
        f"AuditLog.previous_status debería ser 'error' (status antes del retry), "
        f"quedó '{log.detail.get('previous_status')}'. Bug del capture-after-mutate."
    )


def test_retry_preserves_artist_song_title(client, admin_token, db):
    """Regression: campos que NO deberían tocarse en retry siguen
    intactos."""
    job_id = _create_error_job(db, edit_count=2)
    db.expire_all()
    pre = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    pre_artist = pre.artist
    pre_song = pre.song_title

    res = client.post(
        f"/retry/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200

    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.artist == pre_artist
    assert fresh.song_title == pre_song
    assert fresh.input_r2_key is not None  # input se preserva
