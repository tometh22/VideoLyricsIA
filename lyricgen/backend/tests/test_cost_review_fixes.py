"""Regresiones de la segunda revisión de #1084.

Cuatro defectos que hacían que el panel reportara números que no eran los que
usaba, o que mezclaran meses:

1. Las tarifas calibradas se guardaban pero NO se aplicaban — el módulo de
   calibración entero era decorativo.
2. La atribución mensual filtraba el job por fecha pero no la provenance, así
   que gasto de julio caía en junio.
3. El waste "de un mes" medía una ventana que terminaba HOY.
4. La fila `rate_calibration` dejaba `/cost/real` en `complete=false` para
   siempre.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth


# ---------------------------------------------------------------------------
# 1. Las tarifas calibradas se APLICAN, no sólo se guardan
# ---------------------------------------------------------------------------

def test_cost_for_record_usa_la_tarifa_calibrada():
    """Veo está en la tabla a $0,80 de lista; la factura de julio divide a
    ~$0,62. Sin esto, todo agregado corría ~25% alto y el panel mostraba una
    tarifa "real" que nunca usaba."""
    from provenance import cost_for_record

    sin_calibrar = cost_for_record("veo-3.1-fast-generate-001", "google_vertex")
    assert sin_calibrar == 0.80

    calibrado = cost_for_record("veo-3.1-fast-generate-001", "google_vertex",
                                {"veo": 0.62})
    assert calibrado == 0.62


def test_una_herramienta_sin_calibrar_cae_a_la_tabla():
    """La calibración es parcial por diseño: si un SKU no aparece en la
    factura o tiene muestra chica, esa herramienta sigue con precio de lista
    en vez de quedar en cero."""
    from provenance import cost_for_record
    assert cost_for_record("whisper-1", "openai", {"veo": 0.62}) == 0.021


def test_rates_vacio_no_cambia_nada():
    from provenance import cost_for_record
    assert cost_for_record("veo-3.1-fast-generate-001", "google_vertex", {}) == 0.80
    assert cost_for_record("veo-3.1-fast-generate-001", "google_vertex", None) == 0.80


def test_los_agregados_aplican_la_calibracion(db, monkeypatch):
    """El contrato de punta a punta: `tenant_cost_summary` tiene que devolver
    el costo a tarifa de factura, no de lista."""
    from database import AIProvenance, Job
    import provenance

    db.query(Job).filter(Job.tenant_id == "calib-test").delete()
    db.add(Job(job_id="calib1", user_id=1, tenant_id="calib-test",
               artist="A", filename="a.mp3", status="done"))
    db.flush()
    for _ in range(10):
        db.add(AIProvenance(job_id="calib1", step="video_bg",
                            tool_name="veo-3.1-fast-generate-001",
                            tool_provider="google_vertex", prompt_sent="p"))
    db.commit()

    try:
        monkeypatch.setattr(provenance, "rates_for_window",
                            lambda *a, **k: {"veo": 0.62})
        s = provenance.tenant_cost_summary(db, tenant_id="calib-test")
        assert s["total_calls"] == 10
        assert s["total_cost"] == pytest.approx(6.20)   # 10 × 0,62, no × 0,80
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "calib1").delete()
        db.query(Job).filter(Job.tenant_id == "calib-test").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 2. La atribución acota la provenance al período
# ---------------------------------------------------------------------------

def test_collect_jobs_no_cuenta_gasto_de_otro_mes(db):
    """Un job creado el 30 de junio que re-rollea escenas en julio ponía el
    gasto de julio en el balde de junio — que después se compara contra la
    factura de junio."""
    import cost_attribution as ca
    from database import AIProvenance, Job

    db.query(Job).filter(Job.tenant_id == "cross-month").delete()
    junio = datetime(2026, 6, 30, 12, tzinfo=timezone.utc)
    db.add(Job(job_id="xm1", user_id=1, tenant_id="cross-month",
               artist="A", song_title="B", filename="a.mp3",
               status="done", created_at=junio))
    db.flush()
    # Una llamada en junio y otra en julio, del MISMO job.
    db.add(AIProvenance(job_id="xm1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=junio))
    db.add(AIProvenance(job_id="xm1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=junio + timedelta(days=3)))
    db.commit()

    try:
        jobs = ca.collect_jobs(db, "test", period="2026-06")
        assert "xm1" in jobs
        # Sólo la llamada de junio.
        assert jobs["xm1"].billable_calls == 1, \
            "la llamada de julio no pertenece al costo de junio"
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "xm1").delete()
        db.query(Job).filter(Job.tenant_id == "cross-month").delete()
        db.commit()


def test_sin_periodo_toma_todo(db):
    """El modo histórico no debe acotar nada."""
    import cost_attribution as ca
    jobs = ca.collect_jobs(db, "test", period=None)
    assert isinstance(jobs, dict)


# ---------------------------------------------------------------------------
# 3. El waste acepta una ventana explícita
# ---------------------------------------------------------------------------

def test_waste_respeta_start_y_end(db):
    """Pedir un mes cerrado no puede medir hasta hoy."""
    from database import AIProvenance, Job
    from provenance import cost_waste_breakdown

    db.query(Job).filter(Job.tenant_id == "waste-window").delete()
    viejo = datetime(2019, 5, 10, tzinfo=timezone.utc)
    db.add(Job(job_id="ww1", user_id=1, tenant_id="waste-window",
               artist="A", filename="a.mp3", status="done",
               created_at=viejo, completed_at=viejo))
    db.flush()
    db.add(AIProvenance(job_id="ww1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=viejo))
    db.commit()

    try:
        dentro = cost_waste_breakdown(
            db, tenant_id="waste-window",
            start=datetime(2019, 5, 1, tzinfo=timezone.utc),
            end=datetime(2019, 6, 1, tzinfo=timezone.utc))
        assert dentro["total_cost"] > 0

        fuera = cost_waste_breakdown(
            db, tenant_id="waste-window",
            start=datetime(2019, 7, 1, tzinfo=timezone.utc),
            end=datetime(2019, 8, 1, tzinfo=timezone.utc))
        assert fuera["total_cost"] == 0.0, \
            "una ventana posterior no debe ver el gasto de mayo"
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "ww1").delete()
        db.query(Job).filter(Job.tenant_id == "waste-window").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 4. `rate_calibration` no rompe la completitud
# ---------------------------------------------------------------------------

def test_la_fila_de_calibracion_no_marca_incompleto(client, admin_token, db):
    """Guarda tarifas, no gasto: su `amount_usd` es NULL a propósito. Contarla
    como fuente de facturación dejaba `complete=false` para siempre y escondía
    si faltaba una factura de verdad."""
    import billing_sources
    from database import CostSnapshot

    period = "2020-07"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    for src in billing_sources.SOURCES:
        db.add(CostSnapshot(period=period, source=src, amount_usd=10.0,
                            status="ok"))
    db.add(CostSnapshot(period=period, source="rate_calibration",
                        amount_usd=None, status="ok",
                        breakdown=[{"tool": "veo", "derived_rate": 0.62,
                                    "status": "ok"}]))
    db.commit()

    try:
        body = client.get(f"/admin/cost/real?period={period}",
                          headers=auth(admin_token)).json()
        assert body["complete"] is True
        assert "rate_calibration" not in body["errored"]
        # Y no suma al total de gasto.
        assert body["total_usd"] == pytest.approx(10.0 * len(billing_sources.SOURCES))
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()
