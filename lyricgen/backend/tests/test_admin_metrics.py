"""Tests de admin_metrics — Fases 1+2 del panel world-class (2026-06-11).

Series temporales, funnel con percentiles, unit economics por tenant,
health score y alertas de negocio — todo computado de Job + AuditLog +
AIProvenance sembrados en la DB de test.
"""
import pytest
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


def test_rework_alert_requiere_volumen_minimo():
    """100% de retrabajo sobre pocos jobs (ej. el smoke) NO debe alertar;
    con volumen suficiente sí. Regresión del falso positivo 2026-07-24."""
    db = SessionLocal()
    try:
        _cleanup(db); _seed_user(db)
        for i in range(2):  # 2 jobs editados → 100% pero volumen <5
            _seed_job(db, f"rw-{i}", days_ago=1 + i)
            _seed_audit(db, "job.edit_request", f"rw-{i}", days_ago=1 + i)
        with mock.patch.dict(sys.modules, {"sentry_sdk": mock.MagicMock()}):
            fired = run_business_alerts(db)
        assert (_T, "rework-spike") not in {(f["tenant"], f["kind"]) for f in fired}
        for i in range(2, 6):  # sube a 6 editados → ahora sí dispara
            _seed_job(db, f"rw-{i}", days_ago=1 + i)
            _seed_audit(db, "job.edit_request", f"rw-{i}", days_ago=1 + i)
        with mock.patch.dict(sys.modules, {"sentry_sdk": mock.MagicMock()}):
            fired2 = run_business_alerts(db)
        assert (_T, "rework-spike") in {(f["tenant"], f["kind"]) for f in fired2}
    finally:
        _cleanup(db); db.close()


def test_is_internal_tenant_excluye_ci_smoke():
    from admin_metrics import _is_internal_tenant
    assert _is_internal_tenant("genly_edit_smoke_ci") is True
    assert _is_internal_tenant("algo_smoke") is True
    assert _is_internal_tenant("foo_ci") is True
    assert _is_internal_tenant("umusic") is False
    assert _is_internal_tenant("universal_argentina") is False
    assert _is_internal_tenant("") is False
    assert _is_internal_tenant(None) is False


# ---------------------------------------------------------------------------
# "Costo IA" tiene que valer lo mismo en todo el admin
# ---------------------------------------------------------------------------
#
# Antes había dos caminos con reglas distintas para el MISMO concepto:
#
#   admin_metrics.py  → Rendimiento, Insights   sin billable_filter, tarifa de lista
#   provenance.py     → Gestión → Costos        con billable_filter y tarifa calibrada
#
# Medido en jul-2026 contra la factura real de GCP ($142,87, veo $140,80):
#
#   veo-3.1-fast   lista $0,80/llamada   real $0,292116   → lista 2,7x ARRIBA
#   gemini         lista $0,013492       real $0,001885   → lista 7,2x arriba
#
# Las 527 llamadas a Veo de julio valuadas a lista dan $421,60 contra los
# $140,80 que efectivamente facturó Google. O sea: Rendimiento e Insights
# mostraban ~3x MÁS costo del real, y Gestión → Costos el número correcto,
# sin que nada en pantalla dijera que eran métodos distintos.

def test_el_costo_ia_ignora_las_filas_no_facturables(db):
    """Un cache hit no gastó plata y no puede sumar al costo."""
    from datetime import datetime, timedelta, timezone
    from database import AIProvenance, Job
    import admin_metrics
    from provenance import CACHE_HIT_PREFIX

    tenant = "metrics-billable-test"
    ahora = datetime.now(timezone.utc) - timedelta(days=1)
    db.query(Job).filter(Job.tenant_id == tenant).delete(synchronize_session=False)
    db.add(Job(job_id="mb1", user_id=1, tenant_id=tenant, artist="A",
               filename="a.mp3", status="done", created_at=ahora))
    db.flush()
    for jid, resumen in (("p-real", "ok"), ("p-cache", f"{CACHE_HIT_PREFIX} reuse")):
        db.add(AIProvenance(job_id="mb1", step="video_bg",
                            tool_name="veo-3.1-fast-generate-001",
                            tool_provider="google_vertex", prompt_sent="x",
                            response_summary=resumen, created_at=ahora))
    db.commit()
    try:
        econ = admin_metrics.metrics_economics(db, days=7)
        fila = next((t for t in econ["tenants"] if t["tenant_id"] == tenant), None)
        assert fila is not None, "el tenant no aparece"
        una_llamada = fila["ai_cost_usd"]

        # Con el cache hit contado serían DOS llamadas. Que valga una sola
        # es la propiedad: el reuso es un ahorro, no un gasto.
        from provenance import cost_for_record
        assert una_llamada == pytest.approx(
            cost_for_record("veo-3.1-fast-generate-001", "google_vertex"), abs=0.01)
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "mb1").delete(
            synchronize_session=False)
        db.query(Job).filter(Job.tenant_id == tenant).delete(synchronize_session=False)
        db.commit()


def test_el_costo_ia_usa_la_tarifa_calibrada_no_la_de_lista(db):
    """Si hay calibración de la factura, manda ella.

    Sin esto, el módulo de calibración es decorativo: se guarda una tarifa
    "real" que ninguna pantalla usa. Es exactamente lo que pasaba —
    `cost_snapshots` en producción tiene 0 filas y todo el admin caía a
    lista.
    """
    from datetime import datetime, timedelta, timezone
    from database import AIProvenance, CostSnapshot, Job
    import admin_metrics
    import rate_calibration as rc

    tenant = "metrics-calib-test"
    ahora = datetime.now(timezone.utc) - timedelta(days=1)
    period = f"{ahora.year:04d}-{ahora.month:02d}"
    TARIFA = 0.292116          # la derivada de la factura de julio

    db.query(Job).filter(Job.tenant_id == tenant).delete(synchronize_session=False)
    db.query(CostSnapshot).filter(CostSnapshot.period == period,
                                  CostSnapshot.source == "rate_calibration"
                                  ).delete(synchronize_session=False)
    db.add(Job(job_id="mc1", user_id=1, tenant_id=tenant, artist="A",
               filename="a.mp3", status="done", created_at=ahora))
    db.flush()
    db.add(AIProvenance(job_id="mc1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="x",
                        response_summary="ok", created_at=ahora))
    db.commit()
    # Forma REAL de lo que produce `derive_rates`: `load_applied_rates` lee
    # de `breakdown`, no de `applied`, y exige status ok + derived_rate.
    rc.store_rates(db, period, {
        "applied": {"veo": TARIFA},
        "rates": [{"tool": "veo", "status": "ok", "derived_rate": TARIFA,
                   "calls": 527, "invoiced_usd": 140.80}],
    })
    db.commit()
    try:
        econ = admin_metrics.metrics_economics(db, days=7)
        fila = next(t for t in econ["tenants"] if t["tenant_id"] == tenant)
        # `metrics_economics` redondea a centavos, así que se compara contra
        # la calibrada redondeada. Lo que importa es que NO sea la de lista
        # ($0,80): esa diferencia es 2,7x y no se pierde en el redondeo.
        assert fila["ai_cost_usd"] == pytest.approx(round(TARIFA, 2), abs=0.005), (
            f"usó la tarifa de lista en vez de la calibrada: {fila['ai_cost_usd']}")
        assert fila["ai_cost_usd"] < 0.5, "sigue valuando a precio de lista"
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "mc1").delete(
            synchronize_session=False)
        db.query(Job).filter(Job.tenant_id == tenant).delete(synchronize_session=False)
        db.query(CostSnapshot).filter(CostSnapshot.period == period,
                                      CostSnapshot.source == "rate_calibration"
                                      ).delete(synchronize_session=False)
        db.commit()
