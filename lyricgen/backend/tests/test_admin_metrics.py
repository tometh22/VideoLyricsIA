"""Tests de admin_metrics — Fases 1+2 del panel world-class (2026-06-11).

Series temporales, funnel con percentiles, unit economics por tenant,
health score y alertas de negocio — todo computado de Job + AuditLog +
AIProvenance sembrados en la DB de test.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import AIProvenance, AuditLog, Job, SessionLocal, User  # noqa: E402
import admin_metrics  # noqa: E402
from admin_metrics import (  # noqa: E402
    metrics_economics,
    metrics_funnel,
    metrics_health,
    metrics_timeseries,
    run_business_alerts,
)

_T = "tenant_metrics_test"
_NOW = datetime.now(timezone.utc)


def _seed_user(db, uid=9101, plan="250"):
    if not db.query(User).filter(User.id == uid).first():
        db.add(User(id=uid, username=f"metrics-user-{uid}", hashed_password="x",
                    role="user", tenant_id=_T, plan_id=plan))
        db.commit()
    return uid


def _seed_job(db, job_id, *, days_ago=1.0, status="done", approved_days_ago=None,
              uid=9101):
    j = Job(
        job_id=job_id, user_id=uid, tenant_id=_T, artist="A",
        filename=f"{job_id}.wav", style="oscuro", status=status,
        delivery_profile="youtube",
        created_at=_NOW - timedelta(days=days_ago),
        approved_at=(_NOW - timedelta(days=approved_days_ago)
                     if approved_days_ago is not None else None),
    )
    db.add(j)
    db.commit()
    return j


def _seed_audit(db, action, job_id, *, days_ago=1.0, uid=9101):
    db.add(AuditLog(user_id=uid, action=action, detail={"job_id": job_id},
                    created_at=_NOW - timedelta(days=days_ago)))
    db.commit()


def _seed_prov(db, job_id, *, days_ago=1.0, tool="veo-3.1-fast-generate-001",
               provider="google_vertex", step="video_bg"):
    db.add(AIProvenance(job_id=job_id, step=step, tool_name=tool,
                        tool_provider=provider, prompt_sent="p",
                        created_at=_NOW - timedelta(days=days_ago)))
    db.commit()


def _cleanup(db):
    ids = [j.job_id for j in db.query(Job).filter(Job.tenant_id == _T).all()]
    if ids:
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(ids)).delete(
            synchronize_session=False)
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.user_id == 9101).delete(synchronize_session=False)
    db.commit()


def test_timeseries_agrupa_por_dia_y_tenant():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        _seed_job(db, "ts-job-1", days_ago=1, approved_days_ago=0.5)
        _seed_job(db, "ts-job-2", days_ago=1.1)
        _seed_audit(db, "job.edit_request", "ts-job-1", days_ago=0.8)
        out = metrics_timeseries(db, days=7)
        flat_created = sum(d.get(_T, {}).get("created", 0) for d in out["series"].values())
        flat_approved = sum(d.get(_T, {}).get("approved", 0) for d in out["series"].values())
        flat_edits = sum(d.get(_T, {}).get("edit_requests", 0) for d in out["series"].values())
        assert flat_created == 2
        assert flat_approved == 1
        assert flat_edits == 1
    finally:
        _cleanup(db); db.close()


def test_funnel_calcula_etapas_y_percentiles():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        # Job completo: created → diffs → provenance → approve → download.
        _seed_job(db, "fn-job-1", days_ago=2, approved_days_ago=1.0)
        _seed_audit(db, "lyrics.segments_diff", "fn-job-1", days_ago=1.9)
        _seed_audit(db, "lyrics.segments_diff", "fn-job-1", days_ago=1.7)
        _seed_prov(db, "fn-job-1", days_ago=1.5)
        _seed_audit(db, "job.download", "fn-job-1", days_ago=0.5)
        out = metrics_funnel(db, days=7)
        stages = {s["stage"]: s for s in out["stages"]}
        assert out["total_jobs"] == 1
        assert stages["hasta_review"]["reached"] == 1
        assert stages["review"]["p50_s"] is not None and stages["review"]["p50_s"] > 0
        assert stages["render"]["reached"] == 1
        assert stages["aprobacion"]["reached"] == 1
        assert stages["descarga"]["reached"] == 1
    finally:
        _cleanup(db); db.close()


def test_economics_margen_por_tenant():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db, plan="250")  # $8/video
        _seed_job(db, "ec-job-1", days_ago=2, approved_days_ago=1)
        _seed_job(db, "ec-job-2", days_ago=2, approved_days_ago=1)
        _seed_prov(db, "ec-job-1", days_ago=1.5)
        with mock.patch.object(admin_metrics, "cost_for_record", return_value=3.0):
            out = metrics_economics(db, days=7)
        row = next(r for r in out["tenants"] if r["tenant_id"] == _T)
        assert row["approved_videos"] == 2
        assert row["price_per_video"] == 8.00
        assert row["revenue_usd"] == 16.00
        assert row["ai_cost_usd"] == 3.00
        assert row["margin_usd"] == 13.00
        assert row["cost_per_approved_usd"] == 1.50
    finally:
        _cleanup(db); db.close()


def test_health_score_y_componentes():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        # Semana actual: 2 jobs, 1 aprobado sin edits (first-pass), 0 errores.
        _seed_job(db, "hl-job-1", days_ago=2, approved_days_ago=1)
        _seed_job(db, "hl-job-2", days_ago=3)
        # Semana previa: 2 jobs → delta WoW = 0.
        _seed_job(db, "hl-job-3", days_ago=9)
        _seed_job(db, "hl-job-4", days_ago=10)
        out = metrics_health(db)
        t = next(x for x in out["tenants"] if x["tenant_id"] == _T)
        assert t["jobs_7d"] == 2 and t["jobs_prev_7d"] == 2
        assert t["usage_delta_wow"] == 0.0
        assert t["first_pass_rate"] == 1.0
        assert t["error_rate"] == 0.0
        assert t["score"] >= 70 and t["status"] == "verde"
    finally:
        _cleanup(db); db.close()


def test_alerta_usage_drop_dispara_y_volumen_chico_no():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        # Semana previa 6 jobs, actual 1 → caída 83% con volumen suficiente.
        for i in range(6):
            _seed_job(db, f"al-prev-{i}", days_ago=8 + i * 0.1)
        _seed_job(db, "al-cur-1", days_ago=1)
        fake = mock.MagicMock()
        scope = mock.MagicMock()
        fake.push_scope.return_value.__enter__ = mock.Mock(return_value=scope)
        fake.push_scope.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.dict(sys.modules, {"sentry_sdk": fake}):
            fired = run_business_alerts(db)
        kinds = {(f["tenant"], f["kind"]) for f in fired}
        assert (_T, "usage-drop") in kinds
        assert fake.capture_message.called
    finally:
        _cleanup(db); db.close()


def test_alerta_no_dispara_con_salud_normal():
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        _seed_job(db, "ok-1", days_ago=1, approved_days_ago=0.5)
        _seed_job(db, "ok-2", days_ago=8, approved_days_ago=7)
        with mock.patch.dict(sys.modules, {"sentry_sdk": mock.MagicMock()}):
            fired = run_business_alerts(db)
        assert [f for f in fired if f["tenant"] == _T] == []
    finally:
        _cleanup(db); db.close()
