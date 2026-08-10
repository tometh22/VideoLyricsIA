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


# ---------------------------------------------------------------------------
# 5. Un job viejo que gasta DENTRO del mes tiene que entrar
# ---------------------------------------------------------------------------

def test_un_job_de_junio_que_gasta_en_julio_cuenta_en_julio(db):
    """Simétrico al de arriba, y el que faltaba.

    `run_edit_pipeline` re-genera el fondo sobre la MISMA fila de job. Un job
    creado en junio y editado en julio quedaba excluido antes de que corriera
    la query de provenance: el proveedor facturaba esa llamada en julio y el
    informe la omitía por completo — justo el agujero que la reconciliación
    existe para detectar.
    """
    import cost_attribution as ca
    from database import AIProvenance, Job

    db.query(Job).filter(Job.tenant_id == "edit-tardio").delete()
    junio = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    julio = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
    db.add(Job(job_id="lt1", user_id=1, tenant_id="edit-tardio",
               artist="A", song_title="B", filename="a.mp3",
               status="done", created_at=junio))
    db.flush()
    db.add(AIProvenance(job_id="lt1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=julio))
    db.commit()

    try:
        jobs = ca.collect_jobs(db, "test", period="2026-07")
        assert "lt1" in jobs, (
            "el job de junio editado en julio quedaba fuera del informe de "
            "julio, pero su llamada sí estaba en la factura de julio")
        assert jobs["lt1"].billable_calls == 1
        # Y NO aparece en junio, donde no gastó nada.
        assert ca.collect_jobs(db, "test", period="2026-06")["lt1"].cost == 0
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "lt1").delete()
        db.query(Job).filter(Job.tenant_id == "edit-tardio").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 6. Una sola calibración para los dos entornos
# ---------------------------------------------------------------------------

def test_collect_jobs_usa_las_tarifas_que_le_pasan(db, monkeypatch):
    """La calibración vive en la base donde se corrió /cost/calibrate-rates.
    Si cada sesión lee la suya, la peer (staging, de sólo lectura) cae a
    precio de lista y el MISMO Veo sale valuado a dos tarifas distintas
    dentro de un solo informe."""
    import cost_attribution as ca
    from database import AIProvenance, Job

    leidas = []

    def _no_deberia_leer(db_, period):
        leidas.append(period)
        return {}

    monkeypatch.setattr("rate_calibration.load_applied_rates", _no_deberia_leer)

    db.query(Job).filter(Job.tenant_id == "rates-inyectadas").delete()
    julio = datetime(2026, 7, 5, tzinfo=timezone.utc)
    db.add(Job(job_id="ri1", user_id=1, tenant_id="rates-inyectadas",
               artist="A", song_title="B", filename="a.mp3",
               status="done", created_at=julio))
    db.flush()
    db.add(AIProvenance(job_id="ri1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=julio))
    db.commit()

    try:
        jobs = ca.collect_jobs(db, "test", period="2026-07",
                               rates={"veo": 0.62})
        assert abs(jobs["ri1"].cost - 0.62) < 1e-6, (
            "no aplicó las tarifas que le pasó el caller")
        assert leidas == [], (
            "con `rates` explícitas no puede volver a leer la calibración de "
            "su propia sesión")
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "ri1").delete()
        db.query(Job).filter(Job.tenant_id == "rates-inyectadas").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 7. La calibración conserva los precios relativos dentro del grupo
# ---------------------------------------------------------------------------

def test_la_calibracion_no_aplana_veo_fast_contra_standard():
    """El SKU de la factura agrega la familia entera. Devolver la tarifa
    mezclada plana valuaba igual un Veo Fast ($0,80 de lista) y un Veo
    Standard ($3,20): el total del mes cuadraba, pero la atribución movía
    gasto de los usuarios de Standard a los de Fast."""
    from rate_calibration import EST_KEY_SUFFIX, rate_for_tool

    # Mes con mezcla: la estimada ponderada dio $1,00 y la factura $0,80 →
    # todo el grupo está 20% por debajo de lista.
    cal = {"veo": 0.80, f"veo{EST_KEY_SUFFIX}": 1.00}
    fast = rate_for_tool("veo-3.1-fast-generate-001", cal)
    std = rate_for_tool("veo-3.1-generate-001", cal)
    assert fast is not None and std is not None
    assert fast < std, "Standard tiene que seguir costando más que Fast"
    assert abs(std / fast - 4.0) < 0.01, "la razón de lista (4x) se conserva"
    assert abs(fast - 0.64) < 1e-6   # 0.80 de lista × 0.8


def test_sin_estimada_la_calibracion_sigue_siendo_plana():
    """Compatibilidad: una calibración vieja (sin la estimada guardada) tiene
    que seguir devolviendo la tarifa derivada tal cual."""
    from rate_calibration import rate_for_tool
    assert rate_for_tool("veo-3.1-fast-generate-001", {"veo": 0.62}) == 0.62


# ---------------------------------------------------------------------------
# 8. Cada factura directa se prorratea con SU proporción de uso
# ---------------------------------------------------------------------------

def test_cada_proveedor_directo_usa_su_propia_proporcion():
    """UMG y el trabajo interno usan GCP, OpenAI y Replicate en proporciones
    muy distintas. Con una sola proporción mezclada, trabajo interno pesado en
    Replicate movía la porción aplicada a la factura de GCP aunque el uso de
    GCP de UMG no hubiera cambiado — y el error salía como costo de UMG."""
    import cost_attribution as ca

    attribution = {
        "umg": {"songs": 10, "direct_cost": 90.0},
        "business": {
            "umg_share_of_cost": 0.5,      # mezcla
            "umg_share_of_jobs": 0.5,
            # UMG es casi todo GCP; Replicate es casi todo interno.
            "umg_share_by_source": {"gcp": 0.9, "replicate": 0.1},
        },
    }
    out = ca.add_total_cost(attribution,
                            {"gcp": 100.0, "replicate": 100.0},
                            basis="cost")["umg_total"]
    # 100×0,9 + 100×0,1 = 100, no 100×0,5 + 100×0,5 = 100... acá coinciden
    # por simetría, así que lo que se afirma es el DESGLOSE por proveedor.
    assert out["umg_direct_cost_by_source"] == {"gcp": 90.0, "replicate": 10.0}


def test_un_proveedor_sin_uso_medido_cae_a_la_proporcion_mezclada():
    """No hay nada mejor que la mezcla si no se midió uso de ese proveedor;
    lo que no puede es desaparecer de la cuenta."""
    import cost_attribution as ca

    attribution = {
        "umg": {"songs": 1, "direct_cost": 1.0},
        "business": {"umg_share_of_cost": 0.4, "umg_share_of_jobs": 0.4,
                     "umg_share_by_source": {}},
    }
    out = ca.add_total_cost(attribution, {"openai": 10.0},
                            basis="cost")["umg_total"]
    assert out["umg_direct_cost_by_source"] == {"openai": 4.0}


# ---------------------------------------------------------------------------
# 9. El desperdicio se suma de los dos entornos
# ---------------------------------------------------------------------------

def test_el_desperdicio_se_suma_entre_entornos():
    """La producción gestionada de UMG corre en staging. Un subárbol de
    desperdicio de un solo entorno, al lado de totales de dos, describía una
    porción del negocio con cara de describirlo entero."""
    from provenance import merge_waste_breakdowns

    prod = {"total_cost": 10.0, "delivered_cost": 6.0, "delivered_videos": 2,
            "by_destination": [
                {"destination": "entregado", "status": "done",
                 "delivered": True, "jobs": 2, "calls": 6, "cost": 6.0},
                {"destination": "rechazado", "status": "rejected",
                 "delivered": False, "jobs": 1, "calls": 4, "cost": 4.0},
            ], "environments": 1}
    staging = {"total_cost": 90.0, "delivered_cost": 30.0,
               "delivered_videos": 8,
               "by_destination": [
                   {"destination": "entregado", "status": "done",
                    "delivered": True, "jobs": 8, "calls": 30, "cost": 30.0},
                   {"destination": "preview_descartado",
                    "status": "bg_preview_done", "delivered": False,
                    "jobs": 20, "calls": 60, "cost": 60.0},
               ], "environments": 1}

    m = merge_waste_breakdowns(prod, staging)
    assert m["total_cost"] == 100.0
    assert m["delivered_videos"] == 10
    assert m["wasted_cost"] == 64.0
    assert m["waste_ratio"] == 0.64
    assert m["environments"] == 2
    # Los mismos destinos se funden en una fila, no se duplican.
    done = [r for r in m["by_destination"] if r["status"] == "done"]
    assert len(done) == 1 and done[0]["cost"] == 36.0 and done[0]["jobs"] == 10


def test_merge_de_uno_solo_devuelve_lo_mismo():
    from provenance import merge_waste_breakdowns
    uno = {"total_cost": 1.0, "by_destination": [], "environments": 1}
    assert merge_waste_breakdowns(uno) is uno
    assert merge_waste_breakdowns() == {}
