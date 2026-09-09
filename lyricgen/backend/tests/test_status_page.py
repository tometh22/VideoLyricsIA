"""Tests de la página de status pública y del banner de incidente.

El riesgo específico de esta feature no es que se caiga: es que MIENTA. Una
página de status en la que no se puede confiar es peor que no tenerla,
porque el cliente que la mira en verde durante un outage deja de mirarla
para siempre. Casi todos los tests de acá pinean exactamente eso:

  * un skew de release entre API y workers (que pasa en CADA deploy) no
    puede pintar la página de amarillo;
  * una sonda que no pudo correr no puede publicar un outage;
  * la falta de observaciones no puede fabricar 100% de uptime;
  * la sonda y un incidente declarado a mano no pueden sumarse y dar
    50% de uptime en un día que estuvo entero caído.
"""

from datetime import datetime, timedelta, timezone

import pytest

import status_page
from database import StatusComponentEvent, StatusIncident, StatusIncidentUpdate
from tests.conftest import auth


UTC = timezone.utc


def _healthy_snapshot(**over):
    """Snapshot mínimo de `health_snapshot()` con todo sano."""
    snap = {
        "status": "ok",
        "db": "up",
        "db_pool": {"in_use": 2, "total": 16, "utilization": 0.13},
        "redis": "up",
        "workers_alive": 4,
        "queue_depth": {
            "transcription": 0, "transcription_batch": 0, "bg_preview": 0,
            "enterprise": 0, "default": 0, "batch_render": 0,
            "campaign_control": 0,
        },
        "fleet_missing_queues": [],
        "r2": "ready",
        "r2_probe_ms": 120,
        "r2_circuit_breaker": {"open": False},
        "veo_breaker": {"enabled": True, "open": False},
        "disk_free_gb": 40.0,
    }
    snap.update(over)
    return snap


def _by_id(components):
    return {c["id"]: c for c in components}


@pytest.fixture(autouse=True)
def _reset_status_module_state():
    """El módulo cachea el snapshot de /health 20 s, el payload de la página
    15 s, y throttlea la escritura de tramos.

    Sin resetear, el segundo test del archivo lee el estado del primero.
    """
    status_page._cached.update({"at": 0.0, "components": None, "snapshot": None})
    status_page._page_cache.clear()
    status_page._last_observed_at = 0.0
    yield
    status_page._cached.update({"at": 0.0, "components": None, "snapshot": None})
    status_page._page_cache.clear()
    status_page._last_observed_at = 0.0


@pytest.fixture
def clean_status_tables():
    from database import SessionLocal
    db = SessionLocal()
    try:
        db.query(StatusIncidentUpdate).delete(synchronize_session=False)
        db.query(StatusIncident).delete(synchronize_session=False)
        db.query(StatusComponentEvent).delete(synchronize_session=False)
        db.commit()
        yield db
        db.query(StatusIncidentUpdate).delete(synchronize_session=False)
        db.query(StatusIncident).delete(synchronize_session=False)
        db.query(StatusComponentEvent).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# derive_components — la función pura donde se decide qué ve el cliente
# ---------------------------------------------------------------------------

def test_snapshot_sano_da_todo_operativo():
    comps = _by_id(status_page.derive_components(_healthy_snapshot()))
    assert set(comps) == set(status_page.COMPONENT_IDS)
    assert all(c["status"] == "operational" for c in comps.values()), comps


def test_skew_de_release_entre_api_y_workers_no_ensucia_la_pagina():
    """EL test que justifica no derivar del `status` agregado de /health.

    En cada deploy la API nueva arranca mientras la flota vieja todavía
    sirve, así que `health_snapshot` devuelve status=degraded con
    degraded_reason=mixed_worker_releases / worker_fleet_incoherent por
    diseño. Si eso pintara la página, quedaría amarilla después de cada
    merge con todo funcionando — y una alarma que grita en falso de rutina
    enseña a ignorar la alarma.
    """
    snap = _healthy_snapshot(
        status="degraded",
        degraded_reason="mixed_worker_releases",
        fleet_coherent=False,
        fleet_release_match=False,
        worker_releases=[{"release": "abc", "queues": ["default"]}],
    )
    comps = _by_id(status_page.derive_components(snap))
    assert all(c["status"] == "operational" for c in comps.values()), comps


def test_disco_bajo_no_es_un_incidente_para_el_cliente():
    """`disk_low` degrada /health pero el usuario no ve nada: los archivos
    van a R2, no al disco del contenedor."""
    snap = _healthy_snapshot(status="degraded", degraded_reason="disk_low",
                             disk_free_gb=4.0)
    comps = _by_id(status_page.derive_components(snap))
    assert all(c["status"] == "operational" for c in comps.values()), comps


def test_postgres_caido_tira_abajo_el_portal():
    snap = _healthy_snapshot(status="down", db="down", db_error="boom",
                             down_reason="db_down")
    comps = _by_id(status_page.derive_components(snap))
    assert comps["api"]["status"] == "major_outage"
    assert comps["api"]["reason"] == "db_down"


def test_redis_caido_tira_abajo_las_colas_pero_no_el_portal():
    """Sin Redis no se encola nada: transcribir y renderizar no funcionan.
    El portal sí: se puede entrar, ver el historial y descargar."""
    snap = _healthy_snapshot(status="down", redis="down", down_reason="redis_down")
    comps = _by_id(status_page.derive_components(snap))
    assert comps["api"]["status"] == "operational"
    assert comps["transcription"]["status"] == "major_outage"
    assert comps["render"]["status"] == "major_outage"
    assert comps["storage"]["status"] == "operational"


def test_redis_no_configurado_fuera_de_prod_no_es_outage():
    """El fallback por threads es intencional en dev y la app anda."""
    snap = _healthy_snapshot(redis="not_configured")
    del snap["queue_depth"]
    del snap["workers_alive"]
    comps = _by_id(status_page.derive_components(snap))
    assert comps["transcription"]["status"] == status_page.STATUS_UNKNOWN
    assert comps["render"]["status"] == status_page.STATUS_UNKNOWN


def test_sin_senal_de_cola_dice_no_se_y_no_inventa_un_outage():
    """Cuando `rq` no es importable en el proceso, health_snapshot omite
    `queue_depth` y `workers_alive`. Sin distinguir ese caso, "no pudimos
    preguntar" se publicaría igual que "no hay workers" y la página
    anunciaría un outage total que no existe."""
    snap = _healthy_snapshot()
    del snap["queue_depth"]
    del snap["workers_alive"]
    comps = _by_id(status_page.derive_components(snap))
    for cid in ("transcription", "render", "backgrounds"):
        assert comps[cid]["status"] == status_page.STATUS_UNKNOWN, cid
        assert comps[cid]["reason"] == "no_queue_signal"
    # Lo que SÍ se pudo medir se sigue publicando.
    assert comps["api"]["status"] == "operational"
    assert comps["storage"]["status"] == "operational"


def test_workers_alive_menos_uno_es_no_se_no_es_cero():
    """-1 = `Worker.all()` tiró excepción. Tratarlo como 0 publicaría un
    outage total cada vez que Redis contesta lento."""
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(workers_alive=-1)))
    assert comps["render"]["status"] == status_page.STATUS_UNKNOWN


def test_cola_sin_consumidor_es_outage_del_componente_de_esa_cola():
    """La flota está segmentada (ShortWorker en transcription/bg_preview,
    Worker en enterprise/default). Un rollout mal hecho deja UNA cola sin
    nadie escuchando y `workers_alive` sigue >0."""
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(fleet_missing_queues=["transcription"])))
    assert comps["transcription"]["status"] == "major_outage"
    assert comps["render"]["status"] == "operational"


def test_backlog_de_cola_degrada_pero_no_es_caida():
    """Una cola profunda significa "vas a esperar más", no "no funciona".
    Un lote de UMG mete 30-60 jobs de golpe y eso NO es un incidente."""
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(queue_depth={"transcription": 60, "transcription_batch": 0,
                                       "enterprise": 0, "default": 0,
                                       "batch_render": 0, "bg_preview": 0})))
    assert comps["transcription"]["status"] == "operational", "60 es un lote normal"

    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(queue_depth={"transcription": 200, "transcription_batch": 0,
                                       "enterprise": 0, "default": 0,
                                       "batch_render": 0, "bg_preview": 0})))
    assert comps["transcription"]["status"] == "degraded"
    assert comps["transcription"]["reason"] == "backlog_200"


def test_cola_que_no_se_pudo_contar_no_resta_del_backlog():
    """rq devuelve -1 cuando no puede contar una cola. Sumado crudo, ese -1
    baja el total y puede tapar otra cola realmente profunda."""
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(queue_depth={"transcription": 100, "transcription_batch": -1,
                                       "enterprise": 0, "default": 0,
                                       "batch_render": 0, "bg_preview": 0})))
    assert comps["transcription"]["reason"] == "backlog_100"


def test_breaker_de_veo_abierto_es_degradacion_parcial_visible():
    """Con el breaker abierto los renders salen con degradé en vez de fondo
    IA: el entregable es peor y el cliente lo ve, así que se publica."""
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(veo_breaker={"enabled": True, "open": True, "ttl_s": 300})))
    assert comps["backgrounds"]["status"] == "partial_outage"
    assert comps["render"]["status"] == "operational", "el render sigue saliendo"


def test_r2_lento_degrada_y_r2_inalcanzable_es_outage():
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(r2_probe_ms=2400)))
    assert comps["storage"]["status"] == "degraded"
    assert comps["storage"]["reason"] == "storage_slow_2400ms"

    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(r2_probe_error="connect timeout")))
    assert comps["storage"]["status"] == "major_outage"


def test_pool_de_db_saturado_degrada_el_portal():
    comps = _by_id(status_page.derive_components(
        _healthy_snapshot(db_pool={"in_use": 16, "total": 16, "utilization": 1.0})))
    assert comps["api"]["status"] == "degraded"


def test_sin_snapshot_todo_es_desconocido_no_todo_caido():
    """Si `health_snapshot()` tira excepción no sabemos nada. Publicar
    "todo caído" en ese caso convierte un bug de la sonda en un incidente
    de plataforma frente al cliente."""
    for empty in (None, {}, "no-soy-un-dict"):
        comps = _by_id(status_page.derive_components(empty))
        assert all(c["status"] == status_page.STATUS_UNKNOWN
                   for c in comps.values()), empty


def test_unknown_pierde_contra_cualquier_dato_real():
    assert status_page._worse("unknown", "operational") == "operational"
    assert status_page._worse("operational", "unknown") == "operational"
    assert status_page._worse("degraded", "major_outage") == "major_outage"
    assert status_page._worse("unknown", "unknown") == "unknown"


# ---------------------------------------------------------------------------
# Endpoints públicos
# ---------------------------------------------------------------------------

def test_summary_es_publico_y_sin_incidentes_no_muestra_banner(
    client, clean_status_tables, monkeypatch,
):
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(
                            _healthy_snapshot()), _healthy_snapshot()))
    res = client.get("/service-status/summary")  # sin Authorization a propósito
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["indicator"] == "operational"
    assert data["banner"] is False
    assert data["incident"] is None
    assert data["auto_affected"] == []


def test_pagina_publica_es_accesible_sin_token(client, clean_status_tables):
    res = client.get("/service-status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert [c["id"] for c in data["components"]] == list(status_page.COMPONENT_IDS)
    assert "active_incidents" in data and "past_incidents" in data
    # La jerga interna de la sonda NO se publica.
    assert all("reason" not in c for c in data["components"])


def test_admin_ve_la_razon_cruda_que_el_publico_no(client, admin_token,
                                                   clean_status_tables):
    res = client.get("/admin/status/components", headers=auth(admin_token))
    assert res.status_code == 200, res.text
    assert all("reason" in c for c in res.json()["components"])


def test_endpoints_de_admin_rechazan_a_un_usuario_comun(client, user_token):
    assert client.get("/admin/status/incidents",
                      headers=auth(user_token)).status_code == 403
    assert client.post("/admin/status/incidents", headers=auth(user_token), json={
        "title": "no deberia entrar", "body": "x",
    }).status_code == 403


# ---------------------------------------------------------------------------
# Ciclo de vida de un incidente
# ---------------------------------------------------------------------------

def test_ciclo_completo_del_incidente_prende_y_apaga_el_banner(
    client, admin_token, clean_status_tables, monkeypatch,
):
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(
                            _healthy_snapshot()), _healthy_snapshot()))

    created = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Transcripciones demoradas",
        "body": "Estamos viendo demoras en las transcripciones nuevas.",
        "impact": "major",
        "components": ["transcription"],
    })
    assert created.status_code == 200, created.text
    inc = created.json()
    assert inc["status"] == "investigating"
    # Un incidente sin ninguna entrada de timeline sería un título rojo sin
    # explicación: la creación publica las dos cosas juntas.
    assert len(inc["updates"]) == 1

    summary = client.get("/service-status/summary").json()
    assert summary["banner"] is True
    assert summary["severity"] == "critical"
    assert summary["incident"]["id"] == inc["id"]
    assert summary["indicator"] == "partial_outage", "impact major → parcial"

    upd = client.post(f"/admin/status/incidents/{inc['id']}/updates",
                      headers=auth(admin_token),
                      json={"body": "Identificamos la causa.",
                            "status": "identified"})
    assert upd.status_code == 200, upd.text
    assert upd.json()["status"] == "identified"
    assert len(upd.json()["updates"]) == 2

    done = client.post(f"/admin/status/incidents/{inc['id']}/updates",
                       headers=auth(admin_token),
                       json={"body": "Resuelto.", "status": "resolved"})
    assert done.status_code == 200, done.text
    assert done.json()["resolved"] is True
    assert done.json()["banner"] is False, "resolver apaga el banner"

    summary = client.get("/service-status/summary").json()
    assert summary["banner"] is False
    assert summary["indicator"] == "operational"

    page = client.get("/service-status").json()
    assert page["active_incidents"] == []
    assert [i["id"] for i in page["past_incidents"]] == [inc["id"]]
    # El timeline se publica del más nuevo al más viejo.
    bodies = [u["body"] for u in page["past_incidents"][0]["updates"]]
    assert bodies[0] == "Resuelto."


def test_resolver_dos_veces_no_mueve_la_marca_original(
    client, admin_token, clean_status_tables,
):
    """`resolved_at` es la ventana que usa el historial de uptime. Si cada
    re-resolución la corriera, un incidente tocado al día siguiente sumaría
    24 h de downtime que no existieron."""
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Corto", "body": "arranca", "impact": "minor",
    }).json()
    first = client.post(f"/admin/status/incidents/{inc['id']}/updates",
                        headers=auth(admin_token),
                        json={"body": "listo", "status": "resolved"}).json()
    again = client.post(f"/admin/status/incidents/{inc['id']}/updates",
                        headers=auth(admin_token),
                        json={"body": "de nuevo listo", "status": "resolved"}).json()
    assert again["resolved_at"] == first["resolved_at"]


def test_reabrir_un_incidente_limpia_resolved_at(client, admin_token,
                                                 clean_status_tables):
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Volvio", "body": "arranca", "impact": "major",
    }).json()
    client.post(f"/admin/status/incidents/{inc['id']}/updates",
                headers=auth(admin_token),
                json={"body": "listo", "status": "resolved"})
    reopened = client.post(f"/admin/status/incidents/{inc['id']}/updates",
                           headers=auth(admin_token),
                           json={"body": "volvio a pasar",
                                 "status": "investigating"}).json()
    assert reopened["resolved_at"] is None
    assert reopened["resolved"] is False


def test_incidente_no_publico_no_aparece_en_la_pagina(client, admin_token,
                                                      clean_status_tables):
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Interno", "body": "nadie lo vio", "impact": "major",
        "public": False,
    }).json()
    page = client.get("/service-status").json()
    assert all(i["id"] != inc["id"] for i in page["active_incidents"])
    assert client.get(f"/service-status/incidents/{inc['id']}").status_code == 404
    # Pero sí en la lista de admin.
    listed = client.get("/admin/status/incidents", headers=auth(admin_token)).json()
    assert any(i["id"] == inc["id"] for i in listed["incidents"])


def test_incidente_abierto_sin_banner_se_ve_en_la_pagina_y_no_en_la_home(
    client, admin_token, clean_status_tables, monkeypatch,
):
    """Caso real: mantenimiento programado anunciado con anticipación. Tiene
    que estar publicado sin ponerle una barra a todo el mundo."""
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(
                            _healthy_snapshot()), _healthy_snapshot()))
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Mantenimiento programado", "body": "Domingo 3 AM.",
        "impact": "none", "banner": False,
    }).json()
    assert client.get("/service-status/summary").json()["banner"] is False
    page = client.get("/service-status").json()
    assert any(i["id"] == inc["id"] for i in page["active_incidents"])


def test_incidente_creado_ya_resuelto_no_muestra_banner(
    client, admin_token, clean_status_tables, monkeypatch,
):
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(
                            _healthy_snapshot()), _healthy_snapshot()))
    client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Postmortem cargado tarde", "body": "ya paso",
        "impact": "critical", "status": "resolved",
    })
    assert client.get("/service-status/summary").json()["banner"] is False


def test_patch_edita_metadata_y_no_toca_el_timeline(client, admin_token,
                                                    clean_status_tables):
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Titulo viejo", "body": "primera entrada", "impact": "minor",
    }).json()
    started = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    patched = client.patch(f"/admin/status/incidents/{inc['id']}",
                           headers=auth(admin_token),
                           json={"title": "Titulo nuevo", "impact": "critical",
                                 "components": ["render", "render", "storage"],
                                 "started_at": started}).json()
    assert patched["title"] == "Titulo nuevo"
    assert patched["impact"] == "critical"
    assert patched["components"] == ["render", "storage"], "deduplica"
    assert [u["body"] for u in patched["updates"]] == ["primera entrada"]


def test_no_se_puede_declarar_un_incidente_que_arranca_en_el_futuro(
    client, admin_token, clean_status_tables,
):
    """`started_at` alimenta el historial de uptime. Una fecha futura le
    resta downtime a días que todavía no existen."""
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    res = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Del futuro", "body": "x", "started_at": future,
    })
    assert res.status_code == 422


def test_vocabulario_invalido_se_rechaza_con_422(client, admin_token,
                                                 clean_status_tables):
    base = {"title": "Prueba de vocabulario", "body": "x"}
    assert client.post("/admin/status/incidents", headers=auth(admin_token),
                       json={**base, "impact": "apocalipsis"}).status_code == 422
    assert client.post("/admin/status/incidents", headers=auth(admin_token),
                       json={**base, "status": "pensandolo"}).status_code == 422
    assert client.post("/admin/status/incidents", headers=auth(admin_token),
                       json={**base, "components": ["no-existe"]}).status_code == 422


def test_borrar_un_incidente_se_lleva_su_timeline(client, admin_token,
                                                  clean_status_tables):
    """SQLite no aplica ON DELETE CASCADE sin PRAGMA, así que el borrado de
    los hijos es explícito. Un update huérfano rompería la página."""
    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Falso positivo", "body": "publicado por error",
    }).json()
    assert client.delete(f"/admin/status/incidents/{inc['id']}",
                         headers=auth(admin_token)).status_code == 200
    db = clean_status_tables
    db.expire_all()
    assert db.query(StatusIncidentUpdate).filter(
        StatusIncidentUpdate.incident_id == inc["id"]).count() == 0
    assert client.get(f"/service-status/incidents/{inc['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Banner automático (sin nadie despierto para redactar nada)
# ---------------------------------------------------------------------------

def test_una_caida_detectada_por_la_sonda_prende_el_banner_sola(
    client, clean_status_tables, monkeypatch,
):
    """A las 4 AM no hay nadie para redactar un incidente. La barra tiene
    que aparecer igual."""
    snap = _healthy_snapshot(redis="down")
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(snap), snap))
    data = client.get("/service-status/summary").json()
    assert data["banner"] is True
    assert data["severity"] == "critical"
    assert data["incident"] is None
    assert set(data["auto_affected"]) == {"transcription", "render", "backgrounds"}


def test_un_backlog_de_cola_no_le_pone_una_barra_a_todo_el_mundo(
    client, clean_status_tables, monkeypatch,
):
    """Decisión de producto: `degraded` se ve en /status y NO en la home. Si
    una cola profunda pintara la home de amarillo, la barra dejaría de
    significar algo para cuando de verdad haya un outage."""
    snap = _healthy_snapshot(queue_depth={"transcription": 500,
                                          "transcription_batch": 0,
                                          "enterprise": 0, "default": 0,
                                          "batch_render": 0, "bg_preview": 0})
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(snap), snap))
    data = client.get("/service-status/summary").json()
    assert data["indicator"] == "degraded"
    assert data["banner"] is False


def test_el_relato_humano_le_gana_al_banner_automatico(
    client, admin_token, clean_status_tables, monkeypatch,
):
    """Con un incidente redactado, la barra muestra ESE texto y no el copy
    genérico: el operador ya explicó qué pasa mejor que la sonda."""
    snap = _healthy_snapshot(redis="down")
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(snap), snap))
    client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Estamos con una caída de la cola de trabajos",
        "body": "Detectado y trabajando.", "impact": "critical",
        "components": ["transcription", "render"],
    })
    data = client.get("/service-status/summary").json()
    assert data["incident"]["title"].startswith("Estamos con una caída")
    assert data["auto_affected"] == []


# ---------------------------------------------------------------------------
# Historial de 90 días — donde una página de status miente
# ---------------------------------------------------------------------------

def test_sin_observaciones_el_uptime_es_none_y_los_dias_grises(
    clean_status_tables,
):
    """EL test de honestidad. Sin datos, `uptime_pct` NO puede ser 100: un
    100% fabricado por falta de observaciones vuelve la página peor que no
    tenerla."""
    hist = status_page.uptime_history(clean_status_tables, days=90)
    for comp in hist["components"]:
        assert comp["uptime_pct"] is None, comp["id"]
        assert comp["coverage_pct"] == 0.0
        assert {d["status"] for d in comp["days"]} == {"no_data"}


def test_un_tramo_observado_pinta_su_dia(clean_status_tables):
    db = clean_status_tables
    ayer = datetime.now(UTC) - timedelta(days=1)
    inicio = ayer.replace(hour=0, minute=5, second=0, microsecond=0)
    db.add(StatusComponentEvent(
        component="api", status="operational",
        started_at=inicio, last_seen_at=inicio + timedelta(hours=23),
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=3)["components"]}
    dias = {d["day"]: d for d in hist["api"]["days"]}
    assert dias[ayer.date().isoformat()]["status"] == "operational"
    assert hist["api"]["uptime_pct"] == 100.0
    assert hist["render"]["uptime_pct"] is None, "otro componente sigue sin datos"


def test_un_hueco_largo_entre_observaciones_no_se_rellena(clean_status_tables):
    """La sonda externa corre cada 5 min. Un hueco de horas significa que
    nadie miró, no que todo estuvo bien."""
    db = clean_status_tables
    hace_tres = datetime.now(UTC) - timedelta(days=3)
    momento = hace_tres.replace(hour=12, minute=0, second=0, microsecond=0)
    db.add(StatusComponentEvent(
        component="api", status="operational",
        started_at=momento, last_seen_at=momento + timedelta(minutes=5),
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=5)["components"]}
    dias = {d["day"]: d for d in hist["api"]["days"]}
    dia = dias[hace_tres.date().isoformat()]
    assert dia["status"] == "operational"
    assert dia["low_coverage"] is True, "5 minutos no certifican un día entero"


def test_sonda_verde_e_incidente_declarado_no_se_suman(clean_status_tables):
    """El bug que este test pinea: si la ventana de la sonda y la del
    incidente se sumaran, un día entero caído (86.400 s de incidente) más
    un día entero de sonda verde darían 172.800 s "observados" y 50% de
    uptime en un día que estuvo 100% mal."""
    db = clean_status_tables
    ayer = (datetime.now(UTC) - timedelta(days=1)).date()
    inicio = status_page._day_start(ayer)
    fin = inicio + timedelta(days=1)
    db.add(StatusComponentEvent(
        component="render", status="operational",
        started_at=inicio, last_seen_at=fin,
    ))
    db.add(StatusIncident(
        title="Render caído todo el día", status="resolved", impact="critical",
        components=["render"], started_at=inicio, resolved_at=fin,
        banner=False, public=True,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=2)["components"]}
    dias = {d["day"]: d for d in hist["render"]["days"]}
    assert dias[ayer.isoformat()]["status"] == "major_outage"
    assert hist["render"]["uptime_pct"] == 0.0, "el día entero cuenta como caído"


def test_un_incidente_sin_componentes_afecta_a_toda_la_plataforma(
    clean_status_tables,
):
    db = clean_status_tables
    ayer = (datetime.now(UTC) - timedelta(days=1)).date()
    inicio = status_page._day_start(ayer)
    db.add(StatusIncident(
        title="Todo caído", status="resolved", impact="critical",
        components=[], started_at=inicio, resolved_at=inicio + timedelta(days=1),
        banner=False, public=True,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=2)["components"]}
    for cid in status_page.COMPONENT_IDS:
        dias = {d["day"]: d for d in hist[cid]["days"]}
        assert dias[ayer.isoformat()]["status"] == "major_outage", cid


def test_mantenimiento_programado_no_baja_el_uptime(clean_status_tables):
    """Estaba anunciado: descontarlo del SLA castiga el haber avisado."""
    db = clean_status_tables
    ayer = (datetime.now(UTC) - timedelta(days=1)).date()
    inicio = status_page._day_start(ayer)
    fin = inicio + timedelta(days=1)
    db.add(StatusComponentEvent(
        component="api", status="operational", started_at=inicio, last_seen_at=fin,
    ))
    db.add(StatusIncident(
        title="Mantenimiento", status="resolved", impact="none",
        components=["api"], started_at=inicio, resolved_at=fin,
        banner=False, public=True,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=2)["components"]}
    assert hist["api"]["uptime_pct"] == 100.0


def test_la_cobertura_de_hoy_puede_llegar_a_cien(clean_status_tables):
    """`days * 86400` como denominador dejaría la cobertura de hoy clavada
    en una fracción para siempre y el número sería ilegible."""
    db = clean_status_tables
    ahora = datetime.now(UTC)
    inicio_hoy = status_page._day_start(ahora.date())
    db.add(StatusComponentEvent(
        component="api", status="operational",
        started_at=inicio_hoy, last_seen_at=ahora,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=1)["components"]}
    assert hist["api"]["coverage_pct"] > 95.0


# ---------------------------------------------------------------------------
# Registro de observaciones
# ---------------------------------------------------------------------------

def test_observar_el_mismo_estado_extiende_el_tramo_en_vez_de_crear_filas(
    clean_status_tables,
):
    """Una fila por muestra serían ~8.600 filas por día por componente. El
    tramo escribe una sola y bumpea `last_seen_at`."""
    db = clean_status_tables
    comps = [{"id": "api", "status": "operational", "reason": None}]
    status_page.observe_components(db, comps)
    status_page._last_observed_at = 0.0
    status_page.observe_components(db, comps)
    filas = db.query(StatusComponentEvent).filter(
        StatusComponentEvent.component == "api").all()
    assert len(filas) == 1


def test_cambiar_de_estado_abre_un_tramo_nuevo(clean_status_tables):
    db = clean_status_tables
    status_page.observe_components(
        db, [{"id": "api", "status": "operational", "reason": None}])
    status_page._last_observed_at = 0.0
    status_page.observe_components(
        db, [{"id": "api", "status": "major_outage", "reason": "db_down"}])
    filas = db.query(StatusComponentEvent).filter(
        StatusComponentEvent.component == "api"
    ).order_by(StatusComponentEvent.id).all()
    assert [f.status for f in filas] == ["operational", "major_outage"]
    assert filas[1].reason == "db_down"


def test_un_hueco_mayor_al_maximo_abre_un_tramo_nuevo(clean_status_tables):
    """Si extendiéramos por encima del hueco, un tramo abierto en marzo y
    re-visto en junio afirmaría tres meses verdes que nadie observó."""
    db = clean_status_tables
    viejo = datetime.now(UTC) - timedelta(hours=6)
    db.add(StatusComponentEvent(
        component="api", status="operational", started_at=viejo, last_seen_at=viejo,
    ))
    db.commit()
    status_page.observe_components(
        db, [{"id": "api", "status": "operational", "reason": None}])
    assert db.query(StatusComponentEvent).filter(
        StatusComponentEvent.component == "api").count() == 2


def test_un_estado_desconocido_no_se_registra(clean_status_tables):
    """Dejar el hueco es la única forma de que el día salga gris."""
    db = clean_status_tables
    status_page.observe_components(
        db, [{"id": "api", "status": status_page.STATUS_UNKNOWN,
              "reason": "no_snapshot"}])
    assert db.query(StatusComponentEvent).count() == 0


def test_observar_nunca_puede_hacer_fallar_la_respuesta(monkeypatch,
                                                        clean_status_tables):
    """La página tiene que seguir en pie exactamente cuando la DB está mal."""
    class BrokenSession:
        def query(self, *a, **kw):
            raise RuntimeError("la DB se cayó")

        def rollback(self):
            pass

    status_page.observe_components(
        BrokenSession(), [{"id": "api", "status": "operational", "reason": None}])


def test_una_sola_observacion_mala_no_desaparece_del_grafico(clean_status_tables):
    """Un tramo recién abierto tiene started_at == last_seen_at hasta el
    latido siguiente. Si un span de duración cero no aportara nada, un
    outage visto UNA sola vez sería invisible en las barras — justo el dato
    que el visitante vino a buscar."""
    db = clean_status_tables
    ahora = datetime.now(UTC)
    db.add(StatusComponentEvent(
        component="render", status="major_outage", reason="no_workers",
        started_at=ahora, last_seen_at=ahora,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=1)["components"]}
    hoy = {d["day"]: d for d in hist["render"]["days"]}[ahora.date().isoformat()]
    assert hoy["status"] == "major_outage"
    assert hoy["low_coverage"] is True, "cero segundos de cobertura, pero se publica"


def test_un_incidente_instantaneo_tambien_se_registra(clean_status_tables):
    db = clean_status_tables
    ahora = datetime.now(UTC)
    db.add(StatusIncident(
        title="Corte de un minuto", status="resolved", impact="critical",
        components=["api"], started_at=ahora, resolved_at=ahora,
        banner=False, public=True,
    ))
    db.commit()
    hist = {c["id"]: c for c in
            status_page.uptime_history(db, days=1)["components"]}
    hoy = {d["day"]: d for d in hist["api"]["days"]}[ahora.date().isoformat()]
    assert hoy["status"] == "major_outage"


# ---------------------------------------------------------------------------
# Resiliencia: la página tiene que servir cuando la DB no
# ---------------------------------------------------------------------------

class _DeadSession:
    """Session que revienta en cada query, como una DB inalcanzable."""

    def query(self, *a, **kw):
        raise RuntimeError("could not connect to server: Connection refused")

    def rollback(self):
        pass

    def commit(self):
        raise RuntimeError("could not connect to server: Connection refused")

    def add(self, *a, **kw):
        pass


def test_la_pagina_no_tira_500_con_la_db_caida(client, monkeypatch):
    """EL caso que justifica toda la resiliencia del módulo.

    Un 500 acá durante una caída de Postgres es el peor resultado posible:
    el visitante viene JUSTO por eso y se encuentra con que ni la página de
    estado anda. Sin DB se pierde el relato humano, pero la sonda (Redis,
    R2, colas) sigue reportando y `api` sale en `major_outage` por el
    propio `db: down` — o sea, la respuesta sigue siendo correcta y más
    informativa que un error.
    """
    from database import get_db as real_get_db
    from main import app

    snap = _healthy_snapshot(status="down", db="down", down_reason="db_down")
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(snap), snap))
    app.dependency_overrides[real_get_db] = lambda: _DeadSession()
    try:
        page = client.get("/service-status")
        assert page.status_code == 200, page.text
        data = page.json()
        assert data["indicator"] == "major_outage"
        assert data["active_incidents"] == []
        assert data["past_incidents"] == []
        api = next(c for c in data["components"] if c["id"] == "api")
        assert api["status"] == "major_outage"
        # El historial se degrada a "sin datos", no a 100%.
        assert all(d["status"] == "no_data" for d in api["days"])
        assert api["uptime_pct"] is None

        summary = client.get("/service-status/summary")
        assert summary.status_code == 200, summary.text
        assert summary.json()["indicator"] == "major_outage"
        assert summary.json()["banner"] is True
    finally:
        app.dependency_overrides.pop(real_get_db, None)


def test_publicar_un_incidente_invalida_el_cache_de_la_pagina(
    client, admin_token, clean_status_tables, monkeypatch,
):
    """El cache de 15 s absorbe tráfico de lectura durante un outage, pero
    no puede agregar latencia a la publicación: un operador que publica y
    no lo ve aparecer se convierte en su propio bug report y publica de
    nuevo."""
    monkeypatch.setattr(status_page, "current_components",
                        lambda **kw: (status_page.derive_components(
                            _healthy_snapshot()), _healthy_snapshot()))
    # Primer GET: llena el cache con "sin incidentes".
    assert client.get("/service-status").json()["active_incidents"] == []

    inc = client.post("/admin/status/incidents", headers=auth(admin_token), json={
        "title": "Recien publicado", "body": "arranca", "impact": "major",
    }).json()

    page = client.get("/service-status").json()
    assert [i["id"] for i in page["active_incidents"]] == [inc["id"]]

    # Y una actualización posterior también se ve enseguida.
    client.post(f"/admin/status/incidents/{inc['id']}/updates",
                headers=auth(admin_token),
                json={"body": "novedad", "status": "identified"})
    page = client.get("/service-status").json()
    assert page["active_incidents"][0]["status"] == "identified"
