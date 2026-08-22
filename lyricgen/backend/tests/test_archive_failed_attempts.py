"""Tests de archive_failed_attempts — archivado Fase 1 (2026-06-10).

Cuando un job llega a `done` (aprobación), los intentos FALLIDOS previos
del mismo user+filename se marcan archived_at — nunca se borran. La
historia del cliente los esconde por default (HistoryView, toggle
"Archivados"). Contexto: la historia de universal_argentina acumulaba
filas de transcripciones muertas del P0 + re-subidas que parecían
duplicados.
"""
from datetime import datetime, timedelta, timezone
import uuid

from auth import create_user
from database import Job, SessionLocal
from jobs import archive_failed_attempts

_T = "tenant_archive_test"


def _seed(db, *, job_id, status, filename="cancion.wav", age_min=60,
          user_id=1, archived=False):
    j = Job(
        job_id=job_id, user_id=user_id, tenant_id=_T, artist="A",
        filename=filename, style="oscuro", status=status,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
        archived_at=(datetime.now(timezone.utc) if archived else None),
    )
    db.add(j)
    db.commit()
    return j


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


def _archived_ids(db):
    return {
        j.job_id
        for j in db.query(Job).filter(Job.tenant_id == _T).all()
        if j.archived_at is not None
    }


def test_archiva_fallidos_previos_del_mismo_filename():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="fail-transcr", status="transcription_failed", age_min=120)
        _seed(db, job_id="fail-error", status="error", age_min=90)
        _seed(db, job_id="fail-reject", status="rejected", age_min=80)
        keep = _seed(db, job_id="winner", status="done", age_min=10)

        n = archive_failed_attempts(db, keep_job=keep)
        db.commit()

        assert n == 3
        assert _archived_ids(db) == {"fail-transcr", "fail-error", "fail-reject"}
        assert db.query(Job).filter(Job.job_id == "winner").one().archived_at is None
    finally:
        _cleanup(db)
        db.close()


def test_no_archiva_entregables_ni_trabajo_vivo():
    """done / pending_review / processing / drafts jamás se archivan —
    aunque compartan filename. Dos versiones aprobadas del mismo audio
    son dos entregables, no ruido."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="otro-done", status="done", age_min=200)
        _seed(db, job_id="en-revision", status="pending_review", age_min=150)
        _seed(db, job_id="corriendo", status="processing", age_min=100)
        _seed(db, job_id="draft", status="transcribed_pending", age_min=90)
        keep = _seed(db, job_id="winner", status="done", age_min=10)

        n = archive_failed_attempts(db, keep_job=keep)
        db.commit()

        assert n == 0
        assert _archived_ids(db) == set()
    finally:
        _cleanup(db)
        db.close()


def test_no_archiva_fallos_posteriores_al_exito():
    """Un fallo DESPUÉS del job aprobado es información nueva (algo se
    rompió de nuevo), no ruido del pasado."""
    db = SessionLocal()
    try:
        _cleanup(db)
        keep = _seed(db, job_id="winner", status="done", age_min=60)
        _seed(db, job_id="fallo-nuevo", status="error", age_min=5)

        n = archive_failed_attempts(db, keep_job=keep)
        db.commit()

        assert n == 0
    finally:
        _cleanup(db)
        db.close()


def test_no_cruza_filename_usuario_ni_tenant():
    db = SessionLocal()
    try:
        _cleanup(db)
        other_user = create_user(
            db,
            username=f"archive_other_{uuid.uuid4().hex[:8]}",
            password="testpass12345",
            tenant_id=f"archive_other_{uuid.uuid4().hex[:8]}",
        )
        _seed(db, job_id="otro-archivo", status="error", filename="otra.wav", age_min=100)
        _seed(
            db, job_id="otro-user", status="error",
            user_id=other_user.id, age_min=100,
        )
        keep = _seed(db, job_id="winner", status="done", age_min=10)

        n = archive_failed_attempts(db, keep_job=keep)
        db.commit()

        assert n == 0
        assert _archived_ids(db) == set()
    finally:
        _cleanup(db)
        db.close()


def test_idempotente_no_rearchiva():
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed(db, job_id="ya-archivado", status="error", age_min=100, archived=True)
        first_ts = db.query(Job).filter(Job.job_id == "ya-archivado").one().archived_at
        keep = _seed(db, job_id="winner", status="done", age_min=10)

        n = archive_failed_attempts(db, keep_job=keep)
        db.commit()

        assert n == 0
        assert db.query(Job).filter(Job.job_id == "ya-archivado").one().archived_at == first_ts
    finally:
        _cleanup(db)
        db.close()


def test_to_list_dict_expone_archived_at():
    """El frontend filtra por archived_at — tiene que viajar en /jobs."""
    db = SessionLocal()
    try:
        _cleanup(db)
        j = _seed(db, job_id="row", status="error", archived=True)
        d = j.to_list_dict()
        assert d["archived_at"] is not None
        j2 = _seed(db, job_id="row2", status="error")
        assert j2.to_list_dict()["archived_at"] is None
    finally:
        _cleanup(db)
        db.close()
