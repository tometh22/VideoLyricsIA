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
                        is_estimate=True, breakdown=[{"runs": 20}]))
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
        body = client.get(
            "/admin/cost/unit-economics?period=2020-06",
            headers=auth(admin_token),
        ).json()
        assert body["videos_delivered"] >= 1
    finally:
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
