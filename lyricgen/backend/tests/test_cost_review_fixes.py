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
import json

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


# ---------------------------------------------------------------------------
# 10. Un refresh fallido no puede pisar un snapshot sano
# ---------------------------------------------------------------------------

def test_un_refresh_fallido_conserva_el_valor_anterior(client, admin_token, db,
                                                       monkeypatch):
    """Las fuentes son ventanas móviles: GitHub sólo puede consultar el ciclo
    vigente. Re-refrescar julio en agosto devuelve su error deliberado y
    borraba el valor bueno capturado cuando julio ERA el mes actual — un dato
    que ya no se puede volver a pedir."""
    import billing_sources
    from database import CostSnapshot

    db.query(CostSnapshot).filter(CostSnapshot.period == "2099-01").delete()
    db.add(CostSnapshot(period="2099-01", source="github", amount_usd=41.0,
                        status="ok", detail="capturado cuando era el mes actual",
                        is_estimate=False, breakdown=[],
                        fetched_at=datetime.now(timezone.utc)))
    db.commit()

    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": "2099-01",
        "sources": [{"source": "github", "amount_usd": None, "status": "error",
                     "detail": "sólo se puede consultar el ciclo vigente",
                     "is_estimate": False, "breakdown": []}],
    })

    try:
        r = client.post("/admin/cost/refresh?period=2099-01&only=github",
                        headers=auth(admin_token))
        assert r.status_code == 200
        entry = r.json()["sources"][0]
        assert entry.get("kept_previous") is True
        assert entry.get("previous_amount_usd") == 41.0
        assert r.json()["configured"] == ["github"]
        assert r.json()["errored"] == []
        assert r.json()["complete"] is False
        assert r.json()["partial"] is True
        assert "github" not in r.json()["not_requested"]
        assert set(r.json()["not_requested"]) == set(billing_sources.SOURCES) - {"github"}

        db.expire_all()
        row = (db.query(CostSnapshot)
                 .filter(CostSnapshot.period == "2099-01",
                         CostSnapshot.source == "github").one())
        assert row.amount_usd == 41.0, "el refresh fallido borró el dato bueno"
        assert row.status == "ok"
        assert "se conservó el valor anterior" in (row.detail or "")
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == "2099-01").delete()
        db.commit()


def test_un_refresh_ok_si_pisa(client, admin_token, db, monkeypatch):
    """Contraprueba: conservar el valor viejo no puede convertirse en no
    actualizar nunca."""
    import billing_sources
    from database import CostSnapshot

    db.query(CostSnapshot).filter(CostSnapshot.period == "2099-02").delete()
    db.add(CostSnapshot(period="2099-02", source="github", amount_usd=41.0,
                        status="ok", detail="viejo", is_estimate=False,
                        breakdown=[], fetched_at=datetime.now(timezone.utc)))
    db.commit()

    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": "2099-02",
        "sources": [{"source": "github", "amount_usd": 44.0, "status": "ok",
                     "detail": "nuevo", "is_estimate": False, "breakdown": []}],
    })
    try:
        client.post("/admin/cost/refresh?period=2099-02&only=github",
                    headers=auth(admin_token))
        db.expire_all()
        row = (db.query(CostSnapshot)
                 .filter(CostSnapshot.period == "2099-02",
                         CostSnapshot.source == "github").one())
        assert row.amount_usd == 44.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == "2099-02").delete()
        db.commit()


def test_un_refresh_historico_ok_pero_vacio_no_pisa(client, admin_token, db,
                                                    monkeypatch):
    """Replicate envejece predicciones y devuelve una página vacía como $0 ok.
    Un mes cerrado ya capturado no puede convertirse retroactivamente en cero.
    """
    import billing_sources
    from database import CostSnapshot

    period = "2020-07"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.add(CostSnapshot(period=period, source="replicate", amount_usd=19.25,
                        status="ok", detail="captura original",
                        is_estimate=True, breakdown=[{"runs": 20}],
                        fetched_at=datetime(2020, 7, 20,
                                            tzinfo=timezone.utc)))
    db.commit()
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": period, "total_usd": 0.0,
        "sources": [{"source": "replicate", "amount_usd": 0.0,
                     "status": "ok", "detail": "0s de compute",
                     "is_estimate": True, "breakdown": []}],
    })
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}&only=replicate",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_usd"] == 19.25
        assert body["sources"][0]["kept_previous"] is True
        assert body["sources"][0]["discarded_refresh_amount_usd"] == 0.0
        db.expire_all()
        row = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "replicate",
        ).one()
        assert row.amount_usd == 19.25
        assert row.breakdown == [{"runs": 20}]
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


def test_railway_vacio_tampoco_borra_snapshot_provisional(
    client, admin_token, db, monkeypatch,
):
    import billing_sources
    from database import CostSnapshot

    period = "2020-08"
    db.query(CostSnapshot).filter(
        CostSnapshot.period == period,
        CostSnapshot.source == "railway",
    ).delete()
    db.add(CostSnapshot(
        period=period, source="railway", amount_usd=88.0, status="ok",
        detail="captura intrames", breakdown=[{"usage": 88.0}],
        fetched_at=datetime(2020, 8, 20, tzinfo=timezone.utc),
    ))
    db.commit()
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": period,
        "sources": [{"source": "railway", "amount_usd": 0.0,
                     "status": "ok", "detail": "sin usage devuelto",
                     "is_estimate": False, "breakdown": []}],
    })
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}&only=railway",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_usd"] == 88.0
        assert body["configured"] == ["railway"]
        assert body["errored"] == []
        assert body["complete"] is False
        assert body["partial"] is True
        assert set(body["not_requested"]) == set(billing_sources.SOURCES) - {"railway"}
        assert body["sources"][0]["kept_previous"] is True
        db.expire_all()
        row = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "railway",
        ).one()
        assert row.amount_usd == 88.0
        assert row.breakdown == [{"usage": 88.0}]
    finally:
        db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "railway",
        ).delete()
        db.commit()


def test_refresh_fallido_del_mes_abierto_conserva_snapshot_pero_no_completa(
    client, admin_token, db, monkeypatch,
):
    import billing_sources
    from database import CostSnapshot

    period = billing_sources.current_period()
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.add(CostSnapshot(
        period=period, source="gcp", amount_usd=10.0, status="ok",
        detail="captura intrames", breakdown=[{"cost": 10.0}],
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    db.commit()
    entries = [
        {"source": source, "amount_usd": (None if source == "gcp" else 1.0),
         "status": ("error" if source == "gcp" else "ok"),
         "detail": ("timeout" if source == "gcp" else "ok"),
         "is_estimate": False, "breakdown": []}
        for source in billing_sources.SOURCES
    ]
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": period, "sources": entries,
    })
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        gcp = next(row for row in body["sources"] if row["source"] == "gcp")
        assert gcp["status"] == "error"
        assert gcp["amount_usd"] is None
        assert gcp["retained_amount_usd"] == 10.0
        assert gcp["stale"] is True
        assert body["complete"] is False
        assert body["errored"] == ["gcp"]
        db.expire_all()
        stored = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "gcp",
        ).one()
        assert stored.status == "ok"
        assert stored.amount_usd == 10.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


def test_refresh_final_fallido_no_convierte_checkpoint_en_factura(
    client, admin_token, db, monkeypatch,
):
    """Un snapshot intrames sigue incompleto hasta un refresh maduro exitoso."""
    import billing_sources
    from database import CostSnapshot

    period = "2020-10"
    db.query(CostSnapshot).filter(
        CostSnapshot.period == period,
        CostSnapshot.source == "railway",
    ).delete()
    db.add(CostSnapshot(
        period=period, source="railway", amount_usd=12.0, status="ok",
        detail="checkpoint intrames", breakdown=[{"usage": 12.0}],
        fetched_at=datetime(2020, 10, 15, tzinfo=timezone.utc),
    ))
    db.commit()
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": period,
        "sources": [{
            "source": "railway", "amount_usd": None,
            "status": "error", "detail": "timeout de cierre",
            "is_estimate": False, "breakdown": [],
        }],
    })
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}&only=railway",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        entry = body["sources"][0]
        assert entry["status"] == "error"
        assert entry["amount_usd"] is None
        assert entry["retained_amount_usd"] == 12.0
        assert entry["stale"] is True
        assert body["configured"] == []
        assert body["errored"] == ["railway"]
        assert body["complete"] is False

        db.expire_all()
        stored = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "railway",
        ).one()
        assert stored.status == "ok"
        assert stored.amount_usd == 12.0
        assert stored.breakdown == [{"usage": 12.0}]
    finally:
        db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "railway",
        ).delete()
        db.commit()


def test_lecturas_no_reviven_checkpoint_intrames_como_factura(
    client, admin_token, db,
):
    """El row durable conserva el piso, pero ningún panel puede cotizarlo."""
    from database import CostSnapshot

    period = "2020-10"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.add(CostSnapshot(
        period=period, source="gcp", amount_usd=12.0, status="ok",
        detail="checkpoint intrames", breakdown=[{"cost": 12.0}],
        fetched_at=datetime(2020, 10, 15, tzinfo=timezone.utc),
    ))
    db.commit()
    try:
        real = client.get(
            f"/admin/cost/real?period={period}",
            headers=auth(admin_token),
        )
        assert real.status_code == 200, real.text
        real_body = real.json()
        gcp = next(s for s in real_body["sources"] if s["source"] == "gcp")
        assert gcp["status"] == "provisional"
        assert gcp["amount_usd"] is None
        assert gcp["retained_amount_usd"] == 12.0
        assert gcp["stale"] is True
        assert real_body["total_usd"] == 0.0
        assert real_body["complete"] is False
        assert "gcp" in real_body["errored"]

        unit = client.get(
            f"/admin/cost/unit-economics?period={period}",
            headers=auth(admin_token),
        )
        assert unit.status_code == 200, unit.text
        assert unit.json()["real_cost_usd"] == 0.0
        assert unit.json()["cost_complete"] is False
        assert "gcp" in unit.json()["missing_sources"]

        reconcile = client.get(
            f"/admin/cost/reconcile?period={period}",
            headers=auth(admin_token),
        )
        assert reconcile.status_code == 200, reconcile.text
        assert reconcile.json()["invoiced_usd"] == 0.0
        assert "gcp" in reconcile.json()["invoiced_sources_missing"]

        umg = client.get(
            f"/admin/cost/umg?period={period}",
            headers=auth(admin_token),
        )
        assert umg.status_code == 200, umg.text
        assert "umg_total" not in umg.json()
        assert "refresh" in umg.json()["umg_total_unavailable"]
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


def test_primer_refresh_maduro_finaliza_snapshot_provisional(
    client, admin_token, db, monkeypatch,
):
    """Una captura GCP hecha durante el mes no puede congelar gasto parcial."""
    import billing_sources
    from database import CostSnapshot

    period = "2020-09"
    db.query(CostSnapshot).filter(
        CostSnapshot.period == period, CostSnapshot.source == "gcp").delete()
    db.add(CostSnapshot(
        period=period, source="gcp", amount_usd=40.0, status="ok",
        detail="captura parcial", breakdown=[],
        fetched_at=datetime(2020, 9, 15, tzinfo=timezone.utc),
    ))
    db.commit()
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kw: {
        "period": period, "total_usd": 100.0,
        "sources": [{"source": "gcp", "amount_usd": 100.0,
                     "status": "ok", "detail": "factura madura",
                     "is_estimate": False,
                     "breakdown": [{"service": "Vertex AI", "cost": 100.0}]}],
    })
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}&only=gcp",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        entry = response.json()["sources"][0]
        assert entry.get("kept_previous") is not True
        assert entry["amount_usd"] == 100.0
        db.expire_all()
        row = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "gcp",
        ).one()
        assert row.amount_usd == 100.0
        assert row.detail == "factura madura"
    finally:
        db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "gcp",
        ).delete()
        db.commit()


# ---------------------------------------------------------------------------
# 11. El export de facturación es de la CUENTA, no del proyecto
# ---------------------------------------------------------------------------

def test_el_query_de_bigquery_se_acota_a_los_proyectos_configurados(monkeypatch):
    """La tabla de export cubre la cuenta de facturación entera. Si esa cuenta
    tiene otros proyectos, su gasto infla /cost/real, entra en la calibración
    y termina atribuido a clientes de GenLy."""
    import billing_sources

    consultas = []

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"jobComplete": True, "totalRows": "1", "rows": [
                {"f": [{"v": "Vertex AI"}, {"v": "Veo"}, {"v": "10"},
                       {"v": "0"}]}]}

    def _post(url, **kw):
        consultas.append(kw["json"]["query"])
        return _Resp()

    monkeypatch.setenv("GCP_BILLING_BQ_PROJECT", "proj")
    monkeypatch.setenv("GCP_BILLING_BQ_DATASET", "ds")
    monkeypatch.setenv("GCP_BILLING_BQ_TABLE", "tbl")
    monkeypatch.setattr(billing_sources, "_gcp_credentials", lambda: "tok")
    monkeypatch.setattr(billing_sources.requests, "post", _post)

    monkeypatch.setenv("GCP_BILLING_PROJECT_IDS", "genly-prod,genly-staging")
    out = billing_sources.fetch_gcp("2026-07")
    assert "project.id IN ('genly-prod', 'genly-staging')" in consultas[-1]
    assert "invoice.month = '202607'" in consultas[-1]
    assert "usage_start_time" not in consultas[-1]
    assert out.raw["invoice_month"] == "202607"
    assert out.raw["project_scope"] == ["genly-prod", "genly-staging"]

    # Sin configurar NO filtra (filtrar por el proyecto del dataset daría $0
    # si el export vive aparte, que es peor que sobrecontar) pero lo DECLARA.
    monkeypatch.delenv("GCP_BILLING_PROJECT_IDS", raising=False)
    out2 = billing_sources.fetch_gcp("2026-07")
    assert "project.id IN" not in consultas[-1]
    assert out2.raw["project_scope"] == "billing_account"
    assert "cuenta de facturación" in (out2.detail or "")


# ---------------------------------------------------------------------------
# 12. Una sola base de valuación para el desperdicio de los dos entornos
# ---------------------------------------------------------------------------

def test_el_waste_usa_las_tarifas_que_le_pasan(db, monkeypatch):
    """Si la peer carga su propia calibración (que no tiene), su mitad sale a
    precio de lista y el waste_ratio mezclado sale de dos tarifas para el
    mismo Veo."""
    from provenance import cost_waste_breakdown

    leidas = []
    monkeypatch.setattr("provenance.rates_for_window",
                        lambda d, s: leidas.append(s) or {})
    cost_waste_breakdown(
        db,
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        rates={"veo": 0.62},
    )
    assert leidas == [], "con `rates` explícitas no puede leer la suya"


# ---------------------------------------------------------------------------
# 13. Los cache hits también se acotan al período
# ---------------------------------------------------------------------------

def test_los_cache_hits_no_arrastran_la_historia_del_job(db):
    """Desde que un job viejo con gasto adentro entra al informe, contar sus
    cache hits sin acotar traía la vida entera del job al mes reportado."""
    import cost_attribution as ca
    from database import AIProvenance, Job

    db.query(Job).filter(Job.tenant_id == "hits-historicos").delete()
    junio = datetime(2026, 6, 10, tzinfo=timezone.utc)
    julio = datetime(2026, 7, 10, tzinfo=timezone.utc)
    db.add(Job(job_id="ch1", user_id=1, tenant_id="hits-historicos",
               artist="A", song_title="B", filename="a.mp3",
               status="done", created_at=junio))
    db.flush()
    # 2 cache hits en junio, 1 en julio, y una llamada paga en julio (que es
    # lo que hace entrar el job al informe de julio).
    for cuando, n in ((junio, 2), (julio, 1)):
        for _ in range(n):
            db.add(AIProvenance(job_id="ch1", step="video_bg",
                                tool_name="veo-3.1-fast-generate-001",
                                tool_provider="google_vertex", prompt_sent="p",
                                response_summary="cache_hit: 1MB",
                                created_at=cuando))
    db.add(AIProvenance(job_id="ch1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=julio))
    # También son no facturables, pero NO ahorraron una generación.
    for summary in ("cache_only_miss: key=x", "budget_exceeded: no se generó"):
        db.add(AIProvenance(job_id="ch1", step="video_bg",
                            tool_name="veo-3.1-fast-generate-001",
                            tool_provider="google_vertex", prompt_sent="p",
                            response_summary=summary, created_at=julio))
    db.commit()

    try:
        jobs = ca.collect_jobs(db, "test", period="2026-07")
        assert jobs["ch1"].cache_hits == 1, (
            "los cache hits de junio no son ahorro de julio")
        assert ca.collect_jobs(db, "test", period="2026-06")["ch1"].cache_hits == 2
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "ch1").delete()
        db.query(Job).filter(Job.tenant_id == "hits-historicos").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 14. El denominador mensual usa finalización, no creación
# ---------------------------------------------------------------------------

def test_collect_jobs_asigna_la_entrega_al_mes_de_finalizacion(db):
    """Un upload de junio terminado en julio pertenece al denominador de
    julio. Si además se edita en agosto, su costo entra en agosto pero no se
    vuelve a contar como otra entrega."""
    import cost_attribution as ca
    from database import AIProvenance, Job

    db.query(Job).filter(Job.tenant_id == "delivery-month").delete()
    created = datetime(2026, 6, 30, 23, tzinfo=timezone.utc)
    completed = datetime(2026, 7, 2, 1, tzinfo=timezone.utc)
    august_edit = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    db.add(Job(job_id="dm1", user_id=1, tenant_id="delivery-month",
               artist="A", song_title="B", filename="a.mp3", status="done",
               created_at=created, completed_at=completed))
    db.flush()
    db.add(AIProvenance(job_id="dm1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=august_edit))
    db.commit()

    try:
        assert "dm1" not in ca.collect_jobs(db, "test", period="2026-06")

        july = ca.collect_jobs(db, "test", period="2026-07")
        assert july["dm1"].delivered is True
        assert july["dm1"].billable_calls == 0

        august = ca.collect_jobs(db, "test", period="2026-08")
        assert august["dm1"].billable_calls == 1
        assert august["dm1"].delivered is False
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "dm1").delete()
        db.query(Job).filter(Job.tenant_id == "delivery-month").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 15. Sólo las fuentes registradas pueden persistirse o leerse
# ---------------------------------------------------------------------------

def test_refresh_rechaza_fuentes_desconocidas(client, admin_token, db):
    import billing_sources
    from database import CostSnapshot

    with pytest.raises(ValueError, match="fuentes desconocidas"):
        billing_sources.fetch_all("2026-07", only=["gcpp"])

    with pytest.raises(ValueError, match="YYYY-MM"):
        billing_sources.fetch_all("2026-7", only=["gcp"])

    response = client.post(
        "/admin/cost/refresh?period=2026-07&only=gcpp",
        headers=auth(admin_token),
    )
    assert response.status_code == 400
    assert not db.query(CostSnapshot).filter(
        CostSnapshot.period == "2026-07", CostSnapshot.source == "gcpp"
    ).count()


def test_cost_real_ignora_snapshot_desconocido(client, admin_token, db):
    import billing_sources
    from database import CostSnapshot

    period = "2098-11"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    for source in billing_sources.SOURCES:
        db.add(CostSnapshot(period=period, source=source, amount_usd=1.0,
                            status="ok"))
    db.add(CostSnapshot(period=period, source="gcpp", amount_usd=None,
                        status="error", detail="legacy typo"))
    db.commit()
    try:
        body = client.get(f"/admin/cost/real?period={period}",
                          headers=auth(admin_token)).json()
        assert body["complete"] is True
        assert body["total_usd"] == pytest.approx(len(billing_sources.SOURCES))
        assert "gcpp" not in body["errored"]
        assert all(row["source"] != "gcpp" for row in body["sources"])
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


def test_cost_umg_ignora_snapshot_legacy_desconocido(
    client, admin_token, db,
):
    from database import CostSnapshot

    period = "2020-10"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.add_all([
        CostSnapshot(period=period, source="fixed", amount_usd=10.0,
                     status="ok", breakdown=[]),
        CostSnapshot(period=period, source="gcpp", amount_usd=999.0,
                     status="ok", breakdown=[]),
    ])
    db.commit()
    try:
        response = client.get(
            f"/admin/cost/umg?period={period}",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        total = response.json()["umg_total"]
        assert total["invoices_total"] == 10.0
        assert "gcpp" not in total["invoiced_shared_cost_by_source"]
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


def test_portal_sin_metadata_usa_el_job_id_como_identidad(db):
    import cost_attribution as ca
    from database import Delivery, Job

    job_ids = ("portalblank1", "portalblank2")
    db.query(Delivery).filter(Delivery.job_id.in_(job_ids)).delete(
        synchronize_session=False)
    db.query(Job).filter(Job.job_id.in_(job_ids)).delete(
        synchronize_session=False)
    for job_id in job_ids:
        db.add(Job(
            job_id=job_id, user_id=1, tenant_id="default", artist="",
            song_title="", filename="a.mp3", status="done",
        ))
        db.add(Delivery(
            job_id=job_id, label="Renderizado", file_types=[],
            artist_snapshot="", song_title_snapshot="",
            tenant_snapshot="default", added_by_user_id=1,
        ))
    db.commit()
    try:
        portal = ca.collect_portal_songs(db)
        expected = {ca.song_key("", "", job_id) for job_id in job_ids}
        assert set(portal["songs"]) >= expected
        assert len(expected) == 2
        assert ca.collect_song_keys(db) >= expected
        jobs = ca.collect_jobs(db, "prod")
        assert {jobs[job_id].key for job_id in job_ids} == expected
    finally:
        db.query(Delivery).filter(Delivery.job_id.in_(job_ids)).delete(
            synchronize_session=False)
        db.query(Job).filter(Job.job_id.in_(job_ids)).delete(
            synchronize_session=False)
        db.commit()


# ---------------------------------------------------------------------------
# 16. El mes es [inicio, inicio del mes siguiente)
# ---------------------------------------------------------------------------

def test_unit_economics_incluye_el_ultimo_microsegundo(client, admin_token, db):
    from database import Job

    tenant = "last-microsecond"
    db.query(Job).filter(Job.tenant_id == tenant).delete()
    instant = datetime(2020, 6, 30, 23, 59, 59, 500000,
                       tzinfo=timezone.utc)
    db.add(Job(job_id="lm1", user_id=1, tenant_id=tenant, artist="A",
               filename="a.mp3", status="done", created_at=instant,
               completed_at=instant))
    db.commit()
    try:
        body = client.get("/admin/cost/unit-economics?period=2020-06",
                          headers=auth(admin_token)).json()
        assert body["videos_delivered"] >= 1
        assert body["videos_created"] >= 1
    finally:
        db.query(Job).filter(Job.tenant_id == tenant).delete()
        db.commit()


def test_unit_economics_conserva_una_entrega_mientras_se_edita(
    client, admin_token, db,
):
    import cost_attribution as ca
    from database import Job

    tenant = "reopened-delivery"
    delivered_at = datetime(2020, 6, 15, tzinfo=timezone.utc)
    editing_at = datetime(2020, 7, 1, tzinfo=timezone.utc)
    db.query(Job).filter(Job.tenant_id == tenant).delete()
    db.add(Job(job_id="rd1", user_id=1, tenant_id=tenant, artist="A",
               filename="a.mp3", status="editing", created_at=delivered_at,
               completed_at=delivered_at, editing_started_at=editing_at))
    db.commit()
    try:
        monthly = ca.collect_jobs(db, "test", period="2020-06")
        assert monthly["rd1"].delivered is True
        all_time = ca.collect_jobs(db, "test")
        assert all_time["rd1"].delivered is True
        body = client.get(
            "/admin/cost/unit-economics?period=2020-06",
            headers=auth(admin_token),
        ).json()
        assert body["videos_delivered"] >= 1
    finally:
        db.query(Job).filter(Job.tenant_id == tenant).delete()
        db.commit()


def test_waste_conserva_el_costo_de_una_entrega_reabierta(db):
    from database import AIProvenance, Job
    from provenance import cost_waste_breakdown

    tenant = "reopened-waste"
    delivered_at = datetime.now(timezone.utc) - timedelta(days=5)
    editing_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.query(Job).filter(Job.tenant_id == tenant).delete()
    db.add(Job(job_id="rw1", user_id=1, tenant_id=tenant, artist="A",
               filename="a.mp3", status="error", created_at=delivered_at,
               completed_at=delivered_at, editing_started_at=editing_at))
    db.flush()
    db.add(AIProvenance(job_id="rw1", step="video_bg",
                        tool_name="veo-3.1-fast-generate-001",
                        tool_provider="google_vertex", prompt_sent="p",
                        created_at=delivered_at))
    db.commit()
    try:
        out = cost_waste_breakdown(db, since_days=30, tenant_id=tenant)
        assert out["delivered_videos"] == 1
        assert out["delivered_cost"] == pytest.approx(0.8)
        assert out["wasted_cost"] == 0.0
        assert out["by_destination"][0]["status"] == "delivered_reopened"
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == "rw1").delete()
        db.query(Job).filter(Job.tenant_id == tenant).delete()
        db.commit()


def test_reconcile_excluye_storage_de_la_factura_gcp(client, admin_token, db):
    from database import CostSnapshot

    period = "2020-08"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.add(CostSnapshot(
        period=period, source="gcp", amount_usd=100.0, status="ok",
        breakdown=[
            {"service": "Vertex AI", "sku": "Veo", "cost": 70.0},
            {"service": "Cloud Storage", "sku": "Storage", "cost": 30.0},
        ],
    ))
    db.commit()
    try:
        body = client.get(
            f"/admin/cost/reconcile?period={period}",
            headers=auth(admin_token),
        ).json()
        assert body["invoiced_usd"] == 70.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


# ---------------------------------------------------------------------------
# 17. Los cargos GCP no-Vertex son infraestructura compartida
# ---------------------------------------------------------------------------

def test_gcp_separa_vertex_de_storage_y_red():
    import cost_attribution as ca

    attribution = {
        "business": {
            "umg_share_of_cost": 0.75,
            "umg_share_of_jobs": 0.25,
            "umg_share_by_source": {"gcp": 0.8},
        },
        "umg": {"songs": 2, "direct_cost": 10.0},
    }
    ca.add_total_cost(
        attribution,
        {"gcp": 100.0},
        basis="jobs",
        invoice_breakdowns={"gcp": [
            {"service": "Vertex AI", "sku": "Veo", "cost": 70.0},
            {"service": "Cloud Storage", "sku": "Storage", "cost": 20.0},
            {"service": "Networking", "sku": "Egress", "cost": 10.0},
        ]},
    )
    out = attribution["umg_total"]
    assert out["umg_direct_cost"] == pytest.approx(56.0)  # 70 * 80%
    assert out["umg_shared_cost"] == pytest.approx(7.5)   # 30 * 25%
    assert out["invoiced_shared_cost_by_source"] == {
        "gcp_infrastructure": 30.0,
    }
    assert out["invoices_total"] == 100.0


def test_cli_transporta_el_breakdown_de_gcp():
    from scripts.umg_cost_report import parse_invoices

    invoices, breakdowns = parse_invoices(json.dumps({
        "gcp": {
            "amount_usd": 100,
            "breakdown": [
                {"service": "Vertex AI", "sku": "Veo", "cost": 70},
                {"service": "Cloud Storage", "sku": "Storage", "cost": 30},
            ],
        },
        "railway": 20,
    }))
    assert invoices == {"gcp": 100.0, "railway": 20.0}
    assert breakdowns["gcp"][1]["service"] == "Cloud Storage"


def test_cli_rechaza_fuentes_de_factura_desconocidas():
    from scripts.umg_cost_report import parse_invoices

    with pytest.raises(ValueError, match="fuentes desconocidas: gcpp"):
        parse_invoices(json.dumps({"gcp": 100, "gcpp": 100}))


def test_lifecycle_solo_expira_prores_generado():
    from scripts.r2_lifecycle_report import _classify

    assert _classify("tenant/job/umg_master.mov").startswith("master ProRes")
    assert _classify("tenant/job/umg_short.mov.v2").startswith("master ProRes")
    assert not _classify("tenant/job/inputs/bg_custom.mov").startswith("master ProRes")
    assert not _classify("tenant/job/otro.mov").startswith("master ProRes")


def test_promedio_cotizable_incluye_gasto_abandonado():
    from scripts.umg_cost_report import _delivered_cost_stats

    songs = [
        {"delivered": True, "cost": 4.0},
        {"delivered": True, "cost": 6.0},
        {"delivered": False, "cost": 10.0},
    ]
    # $20 de gasto total / 2 entregas = $10 cotizable. La distribución de
    # entregadas conserva mediana $5 y extremos $4-$6.
    stats = _delivered_cost_stats(songs, quoteable_average=10.0)
    assert stats == {
        "n": 2,
        "median": 5.0,
        "quoteable_average": 10.0,
        "max": 6.0,
        "min": 4.0,
    }


# ---------------------------------------------------------------------------
# 18. R2 descuenta las operaciones incluidas
# ---------------------------------------------------------------------------

def test_r2_descuenta_las_cuotas_gratuitas_de_operaciones(monkeypatch):
    import billing_sources

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"viewer": {"accounts": [{
                "r2StorageAdaptiveGroups": [],
                "r2OperationsAdaptiveGroups": [
                    {"sum": {"requests": 1_000_000},
                     "dimensions": {"actionType": "PutObject"}},
                    {"sum": {"requests": 10_000_000},
                     "dimensions": {"actionType": "GetObject"}},
                    {"sum": {"requests": 50_000_000},
                     "dimensions": {"actionType": "DeleteObject"}},
                ],
            }]}}}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    out = billing_sources.fetch_r2("2026-07")
    assert out.amount_usd == 0.0
    by_concept = {row["concepto"]: row for row in out.breakdown}
    assert by_concept["operaciones clase A"]["billable_requests"] == 0
    assert by_concept["operaciones clase B"]["billable_requests"] == 0


def test_r2_no_imputa_franquicia_account_wide_a_un_bucket(monkeypatch):
    import billing_sources

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"viewer": {"accounts": [{
                "r2StorageAdaptiveGroups": [],
                "r2OperationsAdaptiveGroups": [
                    {"sum": {"requests": 100},
                     "dimensions": {"actionType": "PutObject"}},
                    {"sum": {"requests": 200},
                     "dimensions": {"actionType": "GetObject"}},
                ],
            }]}}}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv("R2_BUCKET", "genly")
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    out = billing_sources.fetch_r2("2026-07")
    by_concept = {row["concepto"]: row for row in out.breakdown}
    assert by_concept["operaciones clase A"]["included_requests"] == 0
    assert by_concept["operaciones clase A"]["billable_requests"] == 100
    assert by_concept["operaciones clase B"]["included_requests"] == 0
    assert by_concept["operaciones clase B"]["billable_requests"] == 200
    assert out.raw["account_allowances_applied"] is False


def test_r2_prorratea_filas_parciales_sobre_todo_el_mes(monkeypatch):
    import billing_sources

    gib = 1024 ** 3

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"viewer": {"accounts": [{
                # Diez días observados a 100 GiB en un mes de 31 días.
                "r2StorageAdaptiveGroups": [
                    {"max": {"payloadSize": 100 * gib, "objectCount": 10},
                     "dimensions": {"date": f"2026-07-{day:02d}"}}
                    for day in range(1, 11)
                ],
                "r2OperationsAdaptiveGroups": [],
            }]}}}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(billing_sources, "R2_FREE_GB", 0.0)
    monkeypatch.setattr(billing_sources, "R2_USD_PER_GB_MONTH", 1.0)
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    out = billing_sources.fetch_r2("2026-07")
    storage = next(row for row in out.breakdown if row["concepto"] == "storage")
    assert storage["avg_gb"] == pytest.approx(1000 / 31, abs=0.01)
    assert out.amount_usd == pytest.approx(32.26, abs=0.01)


def test_r2_vacio_no_confirma_costo_cero(monkeypatch):
    import billing_sources

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"viewer": {"accounts": [{
                "r2StorageAdaptiveGroups": [],
                "r2OperationsAdaptiveGroups": [],
            }]}}}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    out = billing_sources.fetch_r2("2026-07")
    assert out.status == "error"
    assert out.amount_usd is None
    assert "R2_BUCKET" in out.detail


def test_relevance_gemini_queda_en_provenance(db, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from database import AIProvenance, Job
    import pipeline

    job_id = "relprov001"
    db.add(Job(job_id=job_id, user_id=1, tenant_id="rel-prov",
               artist="A", filename="a.mp3", status="processing"))
    db.commit()

    monkeypatch.setattr(
        pipeline, "_extract_frame_from_video",
        lambda _video, frame: Path(frame).write_bytes(b"jpeg"),
    )
    monkeypatch.setattr(
        pipeline, "_get_genai_client",
        lambda: SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **_kwargs: SimpleNamespace(text="8"),
        )),
    )
    monkeypatch.setattr(
        pipeline, "_call_with_timeout", lambda call, **_kwargs: call(),
    )

    assert pipeline._score_video_relevance("video.mp4", "una cancha", job_id) == 8
    db.expire_all()
    row = db.query(AIProvenance).filter(
        AIProvenance.job_id == job_id,
        AIProvenance.step == "video_relevance",
    ).one()
    assert row.tool_name == "gemini-2.5-flash"
    assert row.tool_provider == "google_vertex"
    assert row.response_summary == "score=8"


def test_cada_reroll_correctivo_gemini_tiene_provenance(db, monkeypatch):
    from types import SimpleNamespace
    from database import AIProvenance, Job
    import pipeline

    job_id = "rerollprov01"
    db.add(Job(job_id=job_id, user_id=1, tenant_id="reroll-prov",
               artist="A", filename="a.mp3", status="processing"))
    db.commit()
    responses = iter([
        SimpleNamespace(text=(
            '{"style":"video","prompt":"A rain-slicked narrow alley with '
            'graffiti walls and neon reflections, cinematic night scene"}'
        )),
        SimpleNamespace(text=(
            '{"style":"video","prompt":"A wide mountain valley at dawn with '
            'pine trees, shifting sunlight and distant birds, cinematic scene"}'
        )),
    ])
    monkeypatch.setattr(
        pipeline, "_get_genai_client",
        lambda: SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **_kwargs: next(responses),
        )),
    )
    monkeypatch.setattr(
        pipeline, "_generate_content_with_quota_retry",
        lambda call, **_kwargs: call(),
    )

    result = pipeline._analyze_lyrics_for_background(
        "Una canción sobre volver a casa", "Artista", job_id=job_id,
    )
    assert "mountain valley" in result["prompt"]
    db.expire_all()
    rows = (
        db.query(AIProvenance)
        .filter(AIProvenance.job_id == job_id,
                AIProvenance.step == "lyrics_analysis")
        .order_by(AIProvenance.id)
        .all()
    )
    assert len(rows) == 2
    assert "corrective_alley_retry" in rows[0].response_summary
    assert rows[1].response_summary.startswith("attempt=2")


# ---------------------------------------------------------------------------
# 19. Un snapshot fijo cerrado no cambia con la configuración de hoy
# ---------------------------------------------------------------------------

def test_refresh_no_reescribe_suscripciones_de_un_mes_cerrado(
    client, admin_token, db, monkeypatch,
):
    from database import CostSnapshot

    period = "2020-06"
    db.query(CostSnapshot).filter(
        CostSnapshot.period == period, CostSnapshot.source == "fixed").delete()
    db.add(CostSnapshot(period=period, source="fixed", amount_usd=44.0,
                        status="ok", breakdown=[{"concepto": "legacy"}]))
    db.commit()
    monkeypatch.setenv("FIXED_SUBSCRIPTIONS_JSON", '{"new_plan": 99}')
    try:
        response = client.post(
            f"/admin/cost/refresh?period={period}&only=fixed",
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text
        entry = response.json()["sources"][0]
        assert entry["kept_previous"] is True
        db.expire_all()
        row = db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
            CostSnapshot.source == "fixed",
        ).one()
        assert row.amount_usd == 44.0
        assert row.breakdown == [{"concepto": "legacy"}]
    finally:
        db.query(CostSnapshot).filter(
            CostSnapshot.period == period, CostSnapshot.source == "fixed").delete()
        db.commit()


# ---------------------------------------------------------------------------
# 20. Replicate no congela subtotales cuando agota el límite de páginas
# ---------------------------------------------------------------------------

def test_replicate_marca_error_si_queda_paginacion_pendiente(monkeypatch):
    import billing_sources

    class _Resp:
        def __init__(self, next_url):
            self._next_url = next_url

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [{
                    "created_at": "2026-07-15T12:00:00Z",
                    "model": "owner/model",
                    "metrics": {"predict_time": 10},
                }],
                "next": self._next_url,
            }

    calls = []

    def _get(url, **_kwargs):
        calls.append(url)
        return _Resp(f"https://replicate.test/page/{len(calls) + 1}")

    monkeypatch.setenv("REPLICATE_API_TOKEN", "token")
    monkeypatch.setattr(billing_sources, "REPLICATE_MAX_PAGES", 2)
    monkeypatch.setattr(billing_sources.requests, "get", _get)

    out = billing_sources.fetch_replicate("2026-07")

    assert len(calls) == 2
    assert out.status == "error"
    assert out.amount_usd is None
    assert out.raw["pages_fetched"] == 2
    assert "cursor pendiente" in out.detail


def test_replicate_acepta_ultima_pagina_dentro_del_limite(monkeypatch):
    import billing_sources

    class _Resp:
        def __init__(self, next_url):
            self._next_url = next_url

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [{
                    "created_at": "2026-07-15T12:00:00Z",
                    "model": "owner/model",
                    "metrics": {"predict_time": 10},
                }],
                "next": self._next_url,
            }

    responses = iter([_Resp("https://replicate.test/page/2"), _Resp(None)])
    monkeypatch.setenv("REPLICATE_API_TOKEN", "token")
    monkeypatch.setattr(billing_sources, "REPLICATE_MAX_PAGES", 2)
    monkeypatch.setattr(
        billing_sources.requests, "get", lambda *_args, **_kwargs: next(responses),
    )

    out = billing_sources.fetch_replicate("2026-07")

    assert out.status == "ok"
    assert out.amount_usd == pytest.approx(0.0)
    assert out.breakdown[0]["runs"] == 2


# ---------------------------------------------------------------------------
# 21. Cada intento Replicate queda atribuido al job activo
# ---------------------------------------------------------------------------

def test_replicate_registra_whisperx_demucs_y_forced_align_por_job(
    db, monkeypatch,
):
    from database import AIProvenance, Job
    from observability import clear_job_log_context, set_job_log_context
    from replicate_budget import call_with_budget
    import forced_align

    job_id = "replprov001"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(job_id=job_id, user_id=1, tenant_id="repl-prov",
               artist="A", filename="a.mp3", status="processing"))
    db.commit()
    monkeypatch.setattr("replicate.run", lambda *_args, **_kwargs: {"ok": True})

    try:
        set_job_log_context(job_id)
        assert call_with_budget(
            "victor-upmeet/whisperx:version-a",
            lambda: {}, total_budget_s=10, backoff=[0], call_label="WHISPERX",
        ) == {"ok": True}
        assert call_with_budget(
            "cjwbw/demucs:version-b",
            lambda: {}, total_budget_s=10, backoff=[0], call_label="VOCALSEP",
        ) == {"ok": True}
        assert forced_align._call_with_budget(
            "cureau/force-align-wordstamps:version-c",
            lambda: {}, total_budget_s=10, backoff=[0],
        ) == {"ok": True}

        db.expire_all()
        rows = (
            db.query(AIProvenance)
            .filter(AIProvenance.job_id == job_id)
            .order_by(AIProvenance.id)
            .all()
        )
        assert [row.tool_name for row in rows] == [
            "victor-upmeet/whisperx",
            "cjwbw/demucs",
            "cureau/force-align-wordstamps",
        ]
        assert {row.tool_provider for row in rows} == {"replicate"}
        assert [row.tool_version for row in rows] == [
            "version-a", "version-b", "version-c",
        ]
        assert all(row.response_summary == "succeeded" for row in rows)
        assert all(row.duration_ms is not None for row in rows)
    finally:
        clear_job_log_context()
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()
