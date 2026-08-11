"""Tope de generaciones de Veo por canción — control de COSTO.

Medido en jul-2026: 12 jobs de prod (el 10%) consumieron el 42,8% del gasto de
Veo, y uno llegó a 26 llamadas — ~$16 en un video que se vende a $8. Un job
sano usa 1-3.

El presupuesto es por CANCIÓN, no por job: cada edición o re-render crea un
job nuevo, así que un techo por job se esquiva solo.
"""

import pytest

# Importado ACÁ a propósito, antes de cualquier monkeypatch. `pipeline` importa
# `provenance` de forma perezosa DENTRO de `_veo_budget_exceeded`; si ese primer
# import ocurre con `database.SessionLocal` parcheado por `_fake_counter`,
# `provenance.SessionLocal` queda ligado a la sesión falsa para siempre —
# monkeypatch restaura `database`, no el binding que `provenance` ya copió— y
# revienta cualquier test posterior que escriba provenance de verdad.
import provenance  # noqa: F401

# ---------------------------------------------------------------------------
# Tope de generaciones de Veo
# ---------------------------------------------------------------------------

def _fake_counter(monkeypatch, n, artist="Bersuit", title="La Argentinidad",
                  hermanos=None, tenant="universal_argentina", visto=None):
    """Sesión falsa: `n` llamadas pagas ya registradas, y un job con la
    identidad de canción indicada (o sin metadata si van vacías).

    `hermanos` son las filas (job_id, artista, título) que la query de
    candidatos devuelve — sirve para probar la normalización de identidad.
    `visto` es un dict opcional donde se anotan los job_id que la query final
    terminó filtrando, para poder afirmar QUIÉN entró al conteo.
    """
    import pipeline

    filas = hermanos if hermanos is not None else [
        ("job-hermano-1", artist, title),
        ("job-hermano-2", artist, title),
    ]

    class _Q:
        def __init__(self, *, ledger=False):
            # La única query que llama a distinct() es la de job_ids con gasto
            # de Veo en la ventana; es lo que las distingue en el fake.
            self._distinct = False
            self._ledger = ledger
        def filter(self, *a, **k):
            if visto is not None:
                visto.setdefault("filtros", []).extend(str(x) for x in a)
            return self
        def distinct(self):
            self._distinct = True
            return self
        def scalar(self): return 0 if self._ledger else n
        def one_or_none(self): return (artist, title, tenant)
        def all(self):
            if self._distinct:
                return [(jid,) for jid, _a, _t in filas]
            return filas

    class _S:
        def query(self, *a, **k):
            return _Q(ledger=any("veo_budget_ledger" in str(x) for x in a))
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    return pipeline


def test_bajo_el_tope_deja_pasar(monkeypatch):
    p = _fake_counter(monkeypatch, 3)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is False and spent == 3


def test_en_el_tope_corta(monkeypatch):
    p = _fake_counter(monkeypatch, 10)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is True and spent == 10


def test_el_job_de_26_llamadas_se_habria_cortado(monkeypatch):
    """El outlier real de jul-2026: 26 generaciones en una sola canción."""
    p = _fake_counter(monkeypatch, 26)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job123")[0] is True


def test_borrar_job_no_reinicia_el_presupuesto(db, monkeypatch):
    """El hard-delete conserva sólo un ledger hasheado de llamadas pagas."""
    import uuid
    from datetime import datetime, timezone

    import jobs
    import pipeline
    from database import AIProvenance, Job, User, VeoBudgetLedger
    from provenance import CACHE_HIT_PREFIX
    from veo_budget import scope_hash

    old_id = "veoledger001"
    new_id = "veoledger002"
    tenant = f"ledger-{uuid.uuid4().hex[:8]}"
    user = User(
        username=f"ledger-user-{uuid.uuid4().hex[:8]}",
        hashed_password="unused",
        tenant_id=tenant,
    )
    db.add(user)
    db.flush()
    db.add(Job(
        job_id=old_id, user_id=user.id, tenant_id=tenant,
        artist="Bersuit", song_title="La Argentinidad",
        filename="old.mp3", status="error",
    ))
    now = datetime.now(timezone.utc)
    for _ in range(10):
        db.add(AIProvenance(
            job_id=old_id, step="video_bg",
            tool_name="veo-3.1-fast-generate-001",
            tool_provider="google_vertex", prompt_sent="paid",
            response_summary="provider_ok", created_at=now,
        ))
    db.add(AIProvenance(
        job_id=old_id, step="video_bg",
        tool_name="veo-3.1-fast-generate-001",
        tool_provider="google_vertex", prompt_sent="cache",
        response_summary=f"{CACHE_HIT_PREFIX}: key=x", created_at=now,
    ))
    db.commit()

    monkeypatch.setattr(jobs, "_delete_r2_objects", lambda *_a, **_k: None)
    try:
        assert jobs.delete_job(db, old_id, tenant) == (True, "ok")
        expected_scope = scope_hash(
            tenant, pipeline._song_identity(
                "Bersuit", "La Argentinidad", old_id,
            ),
        )
        ledger = db.query(VeoBudgetLedger).filter(
            VeoBudgetLedger.scope_hash == expected_scope,
        ).all()
        assert len(ledger) == 10
        assert db.query(AIProvenance).filter(
            AIProvenance.job_id == old_id).count() == 0

        db.add(Job(
            job_id=new_id, user_id=user.id, tenant_id=tenant,
            artist="Bersuit", song_title="La Argentinidad",
            filename="new.mp3", status="processing",
        ))
        db.commit()
        monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
        assert pipeline._veo_budget_exceeded(new_id) == (True, 10)
    finally:
        db.query(AIProvenance).filter(
            AIProvenance.job_id.in_([old_id, new_id])).delete(
            synchronize_session=False)
        db.query(Job).filter(Job.job_id.in_([old_id, new_id])).delete(
            synchronize_session=False)
        db.query(VeoBudgetLedger).filter(
            VeoBudgetLedger.scope_hash == scope_hash(
                tenant, pipeline._song_identity(
                    "Bersuit", "La Argentinidad", old_id,
                ),
            )).delete(synchronize_session=False)
        db.query(User).filter(User.id == user.id).delete(
            synchronize_session=False)
        db.commit()


def test_el_presupuesto_es_POR_CANCION_no_por_job(monkeypatch):
    """El punto de la revisión: un tope por job se esquiva solo, porque cada
    edición o re-render crea un job NUEVO con presupuesto fresco. Una canción
    que pasa por 5 ediciones a 10 llamadas gastaría 50 sin que ningún tope se
    entere.

    Acá el job es nuevo (0 llamadas propias) pero su canción ya acumuló 12
    entre jobs hermanos → tiene que cortar igual.
    """
    p = _fake_counter(monkeypatch, 12, artist="Bersuit", title="La Argentinidad")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job-recien-creado")
    assert over is True and spent == 12


def test_sin_metadata_de_cancion_cae_a_contar_solo_el_job(monkeypatch):
    """Los previews sin artista/título no tienen identidad de canción; contar
    sólo ese job es lo más ajustado posible sin inventar una."""
    p = _fake_counter(monkeypatch, 11, artist="", title="")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job-sin-metadata")[0] is True


def test_tope_en_cero_lo_desactiva(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 0)
    assert p._veo_budget_exceeded("job123") == (False, 0)


def test_tope_desactivado_conserva_la_fila_facturable(db, monkeypatch):
    """Apagar el ceiling no apaga la contabilidad de la llamada a Vertex."""
    import pipeline
    from database import AIProvenance, Job
    from provenance import BUDGET_PENDING_PREFIX, BUDGET_RESERVED_PREFIX

    job_id = "buddisabled"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(job_id=job_id, user_id=1, tenant_id="budget-disabled",
               artist="A", song_title="S", filename="a.mp3",
               status="processing"))
    row = AIProvenance(
        job_id=job_id, step="video_bg",
        tool_name="veo-3.1-fast-generate-001",
        tool_provider="google_vertex", prompt_sent="p",
        response_summary=BUDGET_PENDING_PREFIX,
    )
    db.add(row)
    db.commit()
    row_id = row.id
    try:
        monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 0)
        assert pipeline._veo_budget_exceeded(job_id, row_id) == (False, 0)
        db.expire_all()
        stored = db.query(AIProvenance).filter(
            AIProvenance.id == row_id).one()
        assert stored.response_summary.startswith(BUDGET_RESERVED_PREFIX)
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()


def test_fallo_del_guard_reintenta_la_reserva_en_transaccion_nueva(
    db, monkeypatch,
):
    import database
    import pipeline
    from database import AIProvenance, Job
    from provenance import BUDGET_PENDING_PREFIX, BUDGET_RESERVED_PREFIX

    job_id = "budfailopen"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(job_id=job_id, user_id=1, tenant_id="budget-failopen",
               artist="A", song_title="S", filename="a.mp3",
               status="processing"))
    row = AIProvenance(
        job_id=job_id, step="video_bg",
        tool_name="veo-3.1-fast-generate-001",
        tool_provider="google_vertex", prompt_sent="p",
        response_summary=BUDGET_PENDING_PREFIX,
    )
    db.add(row)
    db.commit()
    row_id = row.id
    real_session_local = database.SessionLocal
    attempts = 0

    def flaky_session_local():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("guard DB unavailable")
        return real_session_local()

    try:
        monkeypatch.setattr(database, "SessionLocal", flaky_session_local)
        monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
        assert pipeline._veo_budget_exceeded(job_id, row_id) == (False, 0)
        assert attempts >= 2
        db.expire_all()
        stored = db.query(AIProvenance).filter(
            AIProvenance.id == row_id).one()
        assert stored.response_summary.startswith(BUDGET_RESERVED_PREFIX)
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()


def test_tracking_requerido_falla_antes_de_veo_si_no_puede_reservar(
    monkeypatch,
):
    import pipeline

    class BrokenSession:
        def __init__(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr("database.SessionLocal", BrokenSession)
    monkeypatch.setattr(
        pipeline, "_mark_veo_reservation_billable", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)

    with pytest.raises(pipeline.VeoTrackingUnavailable):
        pipeline._veo_budget_exceeded(
            "trackedjob01",
            own_row_id=123,
            require_persistent_tracking=True,
        )


def test_reserva_borrada_cancela_veo_incluso_en_fail_open(db, monkeypatch):
    """Borrar el job no puede dejar salir un POST pago sin contabilidad."""
    import pipeline
    from database import AIProvenance, Job
    from provenance import BUDGET_PENDING_PREFIX

    job_id = "buddeleted"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(
        job_id=job_id, user_id=1, tenant_id="budget-deleted",
        artist="A", song_title="S", filename="a.mp3", status="processing",
    ))
    row = AIProvenance(
        job_id=job_id, step="video_bg",
        tool_name="veo-3.1-fast-generate-001",
        tool_provider="google_vertex", prompt_sent="p",
        response_summary=BUDGET_PENDING_PREFIX,
    )
    db.add(row)
    db.commit()
    row_id = row.id

    # Mirrors jobs.delete_job(): both live rows disappear before the pending
    # reservation can transition to billable. This is cancellation, not a DB
    # outage, so the default production path must fail closed as well.
    db.query(AIProvenance).filter(AIProvenance.id == row_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.commit()

    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
    with pytest.raises(pipeline.VeoTrackingUnavailable, match="disappeared"):
        pipeline._veo_budget_exceeded(job_id, own_row_id=row_id)


@pytest.mark.parametrize("limit", [10, 0])
def test_job_borrado_antes_del_worker_cancela_veo(db, monkeypatch, limit):
    """Un SELECT sano sin parent es cancelación, con o sin ceiling activo."""
    import pipeline
    from database import AIProvenance, Job

    job_id = "budmissingparent"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.commit()

    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", limit)
    with pytest.raises(pipeline.VeoTrackingUnavailable, match="Job disappeared"):
        pipeline._veo_budget_exceeded(job_id)


def test_cancelacion_de_tracking_no_reintenta_ni_cae_a_otro_veo():
    import inspect
    import pipeline

    ensure = inspect.getsource(pipeline._ensure_background)
    retry_loop = ensure[ensure.index("for attempt in range(2):"):]
    tracking_catch = retry_loop.index("except VeoTrackingUnavailable as e:")
    generic_catch = retry_loop.index("except Exception as e:", tracking_catch)
    assert tracking_catch < generic_catch
    assert "veo_tracking_cancelled" in retry_loop[tracking_catch:generic_catch]

    scenes = inspect.getsource(pipeline._generate_scene_clips)
    assert "except (VeoAmbiguousSubmission, VeoTrackingUnavailable):" in scenes

    run = inspect.getsource(pipeline.run_pipeline)
    assert "except VeoTrackingUnavailable as e:" in run
    assert "bg_scene_cancelled_fallback.mp4" in run


def test_generador_propaga_el_contrato_de_tracking_a_la_reserva():
    import inspect
    import pipeline

    source = inspect.getsource(pipeline._generate_veo_video)
    reservation = source[source.index(
        "_over, _spent = _veo_budget_exceeded("
    ):source.index("if _over:")]
    assert "require_persistent_tracking=require_persistent_tracking" in reservation


def test_liberacion_verificada_persiste_estado_no_facturable(db):
    import pipeline
    from database import AIProvenance, Job
    from provenance import BUDGET_RELEASED_PREFIX, BUDGET_RESERVED_PREFIX

    job_id = "vrelease001"
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(
        job_id=job_id, user_id=1, tenant_id="release-test",
        artist="A", song_title="S", filename="a.mp3", status="processing",
    ))
    row = AIProvenance(
        job_id=job_id, step="video_bg",
        tool_name="veo-3.1-fast-generate-001",
        tool_provider="google_vertex", prompt_sent="p",
        response_summary=BUDGET_RESERVED_PREFIX,
    )
    db.add(row)
    db.commit()
    recorder = type("Recorder", (), {
        "_row_id": row.id,
        "_finished": False,
    })()
    try:
        assert pipeline._release_veo_reservation(
            recorder, "HTTP 429 rejected") is True
        db.expire_all()
        stored = db.query(AIProvenance).filter(
            AIProvenance.id == row.id).one()
        assert stored.response_summary.startswith(BUDGET_RELEASED_PREFIX)
        assert stored.duration_ms is not None
        assert recorder._finished is True
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()


def test_liberacion_borra_fila_libre_si_los_updates_fallan(monkeypatch):
    import pipeline

    updates = []
    deletes = []
    monkeypatch.setattr(
        pipeline,
        "_persist_veo_reservation_summary",
        lambda *_a, **_k: updates.append(True) and False,
    )
    monkeypatch.setattr(
        pipeline,
        "_delete_provenance_row_by_id",
        lambda row_id: deletes.append(row_id) or True,
    )
    recorder = type("Recorder", (), {
        "_row_id": 42,
        "_finished": False,
    })()

    assert pipeline._release_veo_reservation(recorder, "rejected") is True
    assert len(updates) == 3
    assert deletes == [42]
    assert recorder._finished is True


def test_liberacion_no_finge_exito_si_update_y_delete_fallan(monkeypatch):
    import pipeline

    monkeypatch.setattr(
        pipeline, "_persist_veo_reservation_summary", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        pipeline, "_delete_provenance_row_by_id", lambda *_a, **_k: False,
    )
    recorder = type("Recorder", (), {
        "_row_id": 42,
        "_finished": True,
    })()

    with pytest.raises(pipeline.VeoTrackingUnavailable):
        pipeline._release_veo_reservation(recorder, "rejected")
    assert recorder._finished is False


def test_sin_job_id_no_topea(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded(None) == (False, 0)
    assert p._veo_budget_exceeded("") == (False, 0)


def test_si_la_db_falla_deja_generar(monkeypatch):
    """Un tope de costo que rompe entregas cuando la DB hipa es peor que el
    gasto que evita."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
    assert pipeline._veo_budget_exceeded("job123") == (False, 0)


def test_la_excepcion_es_atrapable_por_los_llamadores():
    """Los tres call sites envuelven la generación en `except Exception`, así
    que el fallback a gradiente sigue andando."""
    import pipeline
    assert issubclass(pipeline.VeoBudgetExceeded, Exception)
    with pytest.raises(Exception):
        raise pipeline.VeoBudgetExceeded("test")


# ---------------------------------------------------------------------------
# Regresiones de la segunda revisión
# ---------------------------------------------------------------------------

def test_la_reserva_pendiente_no_se_cuenta_a_si_misma():
    """La fila se inserta antes del guard para no perder auditoría si muere el
    worker. Tiene que nacer explícitamente no facturable; excluirla por id no
    alcanza bajo concurrencia entre transacciones."""
    from provenance import BUDGET_PENDING_PREFIX, billable_filter

    sql = str(billable_filter().compile(
        compile_kwargs={"literal_binds": True}))
    assert BUDGET_PENDING_PREFIX in sql

    import inspect
    import pipeline
    source = inspect.getsource(pipeline._generate_veo_video)
    assert "initial_response_summary=BUDGET_PENDING_PREFIX" in source


def test_el_tope_de_veo_se_chequea_antes_de_pedir_credenciales():
    """Una canción bloqueada corta aun si las credenciales están rotas.

    El payload local se construye primero, luego la reserva atómica decide el
    techo y recién entonces se pide el token. Una falla de auth libera esa
    reserva porque todavía no hubo POST.

    Desde que se invoca ``post`` el resultado ya es ambiguo (un timeout puede
    llegar después de que Vertex aceptó el trabajo), por lo que esa reserva sí
    se conserva como facturable.
    """
    import inspect
    import pipeline

    source = inspect.getsource(pipeline._generate_veo_video)
    first_token = source.index("token = _veo_access_token()")
    recorder = source.index("recorder = record_ai_call(")
    reserve = source.index("_over, _spent = _veo_budget_exceeded(")
    first_post = source.index("r = _req.post(")
    request_body = source.index("request_body = {")

    assert request_body < recorder < reserve < first_token < first_post
    auth_failure = source[first_token:first_post]
    assert "_release_veo_reservation(" in auth_failure
    assert "pre-submit auth failed" in auth_failure


def test_un_job_ya_topeado_corta_antes_del_cooldown():
    import inspect
    import pipeline

    source = inspect.getsource(pipeline._generate_veo_video)
    precheck = source.index("_pre_over, _pre_spent = _veo_budget_exceeded(job_id)")
    cooldown = source.index("elapsed = _time.time() - _last_veo_request")
    recorder = source.index("recorder = record_ai_call(")
    atomic_reservation = source.index(
        "_over, _spent = _veo_budget_exceeded(", recorder)
    assert precheck < cooldown < recorder < atomic_reservation


def test_los_rechazos_confirmados_liberan_la_reserva():
    """Un HTTP de rechazo no creó una operación: debe quedar auditable pero
    fuera del gasto y del tope, incluso si fue 429 o credenciales inválidas.
    """
    from provenance import BUDGET_RELEASED_PREFIX, billable_filter

    sql = str(billable_filter().compile(
        compile_kwargs={"literal_binds": True}))
    assert BUDGET_RELEASED_PREFIX in sql

    import inspect
    import pipeline
    source = inspect.getsource(pipeline._generate_veo_video)
    assert "_release_veo_reservation(" in source
    assert "HTTP {r.status_code} rejected" in source
    assert "rate limited {rate_limit_hits} times" in source


def test_el_rechazo_legacy_confirmado_no_cuenta_como_gasto():
    """La implementación vieja dejó otro resumen después de cinco HTTP 429.
    Es un rechazo pre-operación confirmado, no un error ambiguo."""
    from provenance import (
        LEGACY_CONFIRMED_RATE_LIMIT_PREFIX,
        billable_filter,
    )

    sql = str(billable_filter().compile(
        compile_kwargs={"literal_binds": True}))
    assert LEGACY_CONFIRMED_RATE_LIMIT_PREFIX in sql
    assert LEGACY_CONFIRMED_RATE_LIMIT_PREFIX == (
        "error: rate_limited_after_5_retries")


def test_una_respuesta_ambigua_no_duplica_el_post():
    """Tras un timeout de post Vertex puede haber aceptado el render. Se cuenta
    una reserva conservadora y se corta: reintentar bajo la misma fila podría
    crear hasta cinco operaciones pagas contando sólo una.
    """
    import inspect
    import pipeline

    source = inspect.getsource(pipeline._generate_veo_video)
    ambiguous = source.index("ambiguous submission; not retrying")
    next_rate_limit = source.index("if r.status_code == 429", ambiguous)
    block = source[ambiguous:next_rate_limit]
    assert "raise VeoAmbiguousSubmission" in block
    assert "continue" not in block


def test_un_2xx_sin_operation_name_se_conserva_como_ambiguo():
    """Un 2xx anómalo puede haber creado una operación aunque cambie el JSON.

    No se libera el costo ni se repite el POST sin un identificador con el que
    consultar la primera operación.
    """
    import inspect
    import pipeline

    source = inspect.getsource(pipeline._generate_veo_video)
    start = source.index("if not operation_name:")
    end = source.index("veo_breaker.record_success()", start)
    block = source[start:end]
    assert "ambiguous success response missing operation name" in block
    assert "_release_veo_reservation" not in block
    assert "raise VeoAmbiguousSubmission" in block


def test_un_envio_ambiguo_tampoco_reintenta_en_el_bucle_exterior():
    """Cortar el retry HTTP no alcanza: `_ensure_background` tiene otro loop."""
    import inspect
    import pipeline

    assert issubclass(pipeline.VeoAmbiguousSubmission, RuntimeError)
    source = inspect.getsource(pipeline._ensure_background)
    ambiguous = source.index("except VeoAmbiguousSubmission")
    generic = source.index("except Exception", ambiguous)
    block = source[ambiguous:generic]
    assert "break" in block
    assert "continue" not in block


def test_un_envio_ambiguo_de_escena_no_cae_a_otro_veo():
    """La ruta multi-escena debe terminar localmente, no repagar fondo único."""
    import inspect
    import pipeline

    scene_source = inspect.getsource(pipeline._generate_scene_clips)
    specific = scene_source.index(
        "except (VeoAmbiguousSubmission, VeoTrackingUnavailable)"
    )
    generic = scene_source.index("except Exception as e", specific)
    assert specific < generic
    assert "raise" in scene_source[specific:generic]

    run_source = inspect.getsource(pipeline.run_pipeline)
    scene_call = run_source.index("_generate_scene_background(")
    single_bg = run_source.index("_ensure_background(", scene_call)
    handler = run_source[scene_call:single_bg]
    assert "except VeoAmbiguousSubmission" in handler
    ambiguous_block = handler[handler.index("except VeoAmbiguousSubmission"):]
    assert "_write_safe_gradient_background(" in ambiguous_block


def test_http_408_y_5xx_son_ambiguos_pero_4xx_rechaza():
    import inspect
    import pipeline

    assert pipeline._veo_http_failure_is_ambiguous(408) is True
    assert pipeline._veo_http_failure_is_ambiguous(500) is True
    assert pipeline._veo_http_failure_is_ambiguous(502) is True
    assert pipeline._veo_http_failure_is_ambiguous(599) is True
    assert pipeline._veo_http_failure_is_ambiguous(400) is False
    assert pipeline._veo_http_failure_is_ambiguous(401) is False
    assert pipeline._veo_http_failure_is_ambiguous(403) is False
    assert pipeline._veo_http_failure_is_ambiguous(422) is False

    source = inspect.getsource(pipeline._generate_veo_video)
    failure = source[source.index("if not r.ok:"):
                     source.index("try:\n            payload = r.json()")]
    ambiguous = failure.index("_veo_http_failure_is_ambiguous")
    release = failure.index("_release_veo_reservation")
    assert ambiguous < release
    assert "raise VeoAmbiguousSubmission" in failure[:release]


def test_la_identidad_de_cancion_colapsa_espacios(monkeypatch):
    """Comparando con `lower(trim(...))` en SQL, "La  Argentinidad" y
    "La Argentinidad" eran canciones distintas — corregir la metadata de un
    job le estrenaba presupuesto. La identidad tiene que ser la misma que usa
    `cost_attribution.song_key`."""
    import pipeline
    assert (pipeline._song_identity("Bersuit", "La  Argentinidad")
            == pipeline._song_identity(" bersuit ", "La Argentinidad"))


def test_los_hermanos_se_matchean_por_identidad_normalizada(monkeypatch):
    """Un job con el título mal tipeado sigue contando contra el mismo
    presupuesto."""
    p = _fake_counter(
        monkeypatch, 12, artist="Bersuit", title="La Argentinidad",
        hermanos=[
            ("j1", "Bersuit", "La Argentinidad"),
            ("j2", "bersuit", "La  Argentinidad"),   # espacios y mayúsculas
            ("j3", "Otra Banda", "Otro Tema"),        # no es hermana
        ])
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("j1")
    assert over is True and spent == 12


def test_la_fila_se_borra_cuando_corta(monkeypatch):
    """Cerrar la fila con `budget_exceeded` inventaba gasto que nunca ocurrió
    y contaminaba el conteo de la pasada siguiente. Se borra."""
    import pipeline

    borradas = []

    class _Del:
        def delete(self, **k): borradas.append(True)
    class _Q:
        def filter(self, *a, **k): return _Del()
    class _S:
        def query(self, *a, **k): return _Q()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    rec = type("R", (), {"_row_id": 42, "_finished": False,
                         "finish": lambda s, **k: None})()
    pipeline._discard_provenance_row(rec)
    assert borradas == [True]
    assert rec._finished is True


def test_si_no_puede_borrar_no_rompe(monkeypatch):
    """Un control de costo no puede tumbar la generación porque falló un
    DELETE."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    cerrada = []
    rec = type("R", (), {"_row_id": 42, "_finished": False,
                         "finish": lambda s, **k: cerrada.append(k)})()
    pipeline._discard_provenance_row(rec)   # no debe levantar
    assert cerrada, "cae a cerrar la fila si no puede borrarla"



# ---------------------------------------------------------------------------
# Regresiones de la tercera revisión
# ---------------------------------------------------------------------------

def test_el_presupuesto_no_cruza_tenants(monkeypatch):
    """Dos clientes que rinden la misma canción NO comparten un techo.

    Sin filtrar por tenant, el primero en llegar a 10 generaciones le corta el
    render pago al segundo, que no gastó nada. La canción es la unidad de
    presupuesto DENTRO de un cliente, no entre clientes.
    """
    visto = {}
    p = _fake_counter(monkeypatch, 3, tenant="universal_argentina", visto=visto)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    p._veo_budget_exceeded("job123")
    filtros = " | ".join(visto.get("filtros", []))
    assert "tenant_id" in filtros, (
        "los jobs hermanos se buscan sin acotar por tenant → un cliente le "
        f"come el presupuesto a otro. Filtros vistos: {filtros}")


def test_los_previews_sin_metadata_no_comparten_un_solo_tope(monkeypatch):
    """`/generate-preview` guarda el placeholder `preview|preview` cuando el
    caller no manda artista/título. `song_key` lo trata como identidad
    degenerada y la desambigua con el job_id — pero sólo si se lo pasan.

    Sin el job_id, TODOS esos jobs colapsan en `__sin_metadata__|desconocido`,
    comparten un único tope de 10 y se agotan globalmente: el preview 11 de
    cualquier canción del sistema queda cortado para siempre.
    """
    import pipeline

    a = pipeline._song_identity("preview", "preview", "job-A")
    b = pipeline._song_identity("preview", "preview", "job-B")
    assert a != b, "dos previews sin metadata no son la misma canción"
    assert a.startswith("__sin_metadata__|")

    # Y el guard tiene que caer al conteo por job, no al de hermanos.
    p = _fake_counter(monkeypatch, 11, artist="preview", title="preview")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job-preview")
    assert over is True and spent == 11


def test_una_cancion_de_verdad_sigue_agrupando(monkeypatch):
    """Contraprueba del anterior: la desambiguación por job_id NO puede romper
    el agrupamiento real, que es todo el punto del tope por canción."""
    import pipeline
    assert (pipeline._song_identity("Bersuit", "La  Argentinidad", "job-A")
            == pipeline._song_identity(" bersuit ", "La Argentinidad", "job-B"))


# ---------------------------------------------------------------------------
# Los lookups cache_only no gastan: no pueden consumir presupuesto
# ---------------------------------------------------------------------------

def test_un_cache_only_miss_no_cuenta_como_llamada_paga():
    """Un `cache_only` busca el clip en R2 y, si no está, NO genera — ese es
    todo el punto (regenerar una escena no puede re-cobrar las otras N).

    La fila quedaba contada como paga para siempre: inflaba el costo del panel
    y le comía el tope a la canción."""
    from provenance import billable_filter
    from database import AIProvenance
    sql = str(billable_filter().compile(
        compile_kwargs={"literal_binds": True}))
    assert "cache_only_miss" in sql, (
        "billable_filter no excluye los cache_only_miss")
    assert "cache_hit" in sql, "y tiene que seguir excluyendo los cache hits"
    assert AIProvenance is not None


def test_el_lookup_cache_only_se_registra_despues_de_saber_el_resultado():
    """La fila se insertaba ANTES del lookup, y mientras está en vuelo
    (`response_summary` NULL) `billable_filter()` la cuenta como paga. En un
    regen multi-escena esas consultas corren en paralelo con la ÚNICA escena
    que sí paga, así que le comían el tope con llamadas de $0.

    Se verifica sobre el código: dentro de `_generate_veo_video`, el recorder
    de la generación paga tiene que crearse DESPUÉS del bloque `cache_only`.
    """
    import inspect
    import pipeline

    src = inspect.getsource(pipeline._generate_veo_video)
    i_cache = src.index("if cache_only:")
    i_rec = src.index("recorder = record_ai_call(")
    assert i_rec > i_cache, (
        "el recorder pago se abre antes del bloque cache_only → los lookups "
        "gratis quedan en vuelo contando como pagos")
    bloque = src[src.index("def _registrar_cache_only"):i_cache]
    assert "initial_response_summary=summary" in bloque, (
        "el resultado cache_only se inserta primero como NULL y queda "
        "transitoriamente facturable")


def test_record_ai_call_inserta_el_estado_conocido_atomicamente(db):
    """No debe existir una transacción intermedia con response_summary=NULL."""
    from database import AIProvenance, Job
    from provenance import CACHE_ONLY_MISS_PREFIX, record_ai_call

    job_id = "atomiccache1"
    db.query(Job).filter(Job.job_id == job_id).delete()
    db.add(Job(job_id=job_id, user_id=1, tenant_id="atomic-cache",
               artist="A", filename="a.mp3", status="processing"))
    db.commit()
    try:
        recorder = record_ai_call(
            job_id, "video_bg", "veo-3.1-fast-generate-001",
            "google_vertex", "prompt",
            initial_response_summary=f"{CACHE_ONLY_MISS_PREFIX}: key=x",
        )
        db.expire_all()
        row = db.query(AIProvenance).filter(
            AIProvenance.id == recorder._row_id).one()
        assert row.response_summary.startswith(CACHE_ONLY_MISS_PREFIX)
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()


# ---------------------------------------------------------------------------
# Regresiones de la cuarta revisión
# ---------------------------------------------------------------------------

def test_la_ventana_tambien_aplica_a_los_jobs_sin_metadata(monkeypatch):
    """El fallback por job filtraba SÓLO por job_id. Un job_id estable y
    reutilizado (los `sample-{style}` del script de muestras) quedaba
    bloqueado para siempre al llegar a 10, aunque los VEO_BUDGET_WINDOW_DAYS
    hubieran pasado hace meses."""
    visto = {}
    p = _fake_counter(monkeypatch, 3, artist="", title="", visto=visto)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    p._veo_budget_exceeded("sample-estatico")
    filtros = " | ".join(visto.get("filtros", []))
    assert "created_at" in filtros, (
        "sin la ventana, un job_id reutilizado se bloquea de por vida. "
        f"Filtros vistos: {filtros}")


def test_muestras_crean_identidades_persistentes_para_presupuesto(db, monkeypatch):
    import uuid
    from database import Job, User
    from scripts import regenerate_movement_samples as samples

    owner = User(
        username=f"movement-samples-{uuid.uuid4().hex[:8]}",
        hashed_password="unused",
        role="user",
        tenant_id="test-internal",
    )
    db.add(owner)
    db.commit()
    monkeypatch.setenv("MOVEMENT_SAMPLES_USER_ID", str(owner.id))
    ids = []
    try:
        ids = [samples._ensure_tracking_job(entry["style"], db=db)
               for entry in samples.SAMPLES]
        assert len(set(ids)) == len(samples.SAMPLES)
        assert all(len(job_id) == 12 for job_id in ids)
        rows = db.query(Job).filter(Job.job_id.in_(ids)).all()
        assert len(rows) == len(ids)
        assert {row.tenant_id for row in rows} == {samples._TRACKING_TENANT}
        assert {row.status for row in rows} == {"internal_sample"}
    finally:
        if ids:
            db.query(Job).filter(Job.job_id.in_(ids)).delete()
        db.query(User).filter(User.id == owner.id).delete()
        db.commit()


def test_generadores_pagos_de_scripts_declaran_una_identidad_persistente():
    """Ningún one-off puede volver a llamar Vertex sin pasar ``job_id``."""
    import ast
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    missing = []
    for path in scripts_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else ""
            )
            if name not in {"_generate_veo_video", "_generate_imagen_image"}:
                continue
            if not any(kw.arg == "job_id" for kw in node.keywords):
                missing.append(f"{path.relative_to(scripts_dir)}:{node.lineno}")
            if name == "_generate_veo_video" and not any(
                kw.arg == "require_persistent_tracking"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                missing.append(
                    f"{path.relative_to(scripts_dir)}:{node.lineno} "
                    "(tracking is not fail-closed)"
                )
    assert missing == [], f"provider calls without job_id: {missing}"


def test_experimentos_aislan_tracking_por_job_de_benchmark():
    from pathlib import Path

    experiment_dir = (
        Path(__file__).resolve().parents[1] / "scripts" / "loop_experiments"
    )
    for option in "abcd":
        source = (experiment_dir / f"run_option_{option}.py").read_text()
        assert "benchmark_job_id" in source
        assert 'benchmark_job_id=job["job_id"]' in source
        assert f'f"loop-exp-{option}:{{benchmark_job_id}}' in source


def test_veo_fresco_sin_job_id_falla_antes_del_proveedor(monkeypatch):
    import pipeline

    provider_touched = []
    monkeypatch.setattr(
        pipeline, "_veo_access_token",
        lambda: provider_touched.append(True) or "token")
    with pytest.raises(RuntimeError, match="persistent job_id"):
        pipeline._generate_veo_video(
            "prompt", "/tmp/untracked-veo.mp4", job_id=None)
    assert provider_touched == []


def test_veo_fresco_rechaza_un_job_id_sin_fila_persistente(monkeypatch):
    import pipeline

    provider_touched = []
    recorder = type("Recorder", (), {"_row_id": None})()
    monkeypatch.setattr(
        provenance, "record_ai_call", lambda **kwargs: recorder)
    monkeypatch.setattr(
        pipeline, "_veo_access_token",
        lambda: provider_touched.append(True) or "token")
    monkeypatch.setattr(pipeline, "_last_veo_request", 0)

    with pytest.raises(
        pipeline.VeoTrackingUnavailable, match="Job disappeared"
    ):
        pipeline._generate_veo_video(
            "prompt", "/tmp/untracked-veo.mp4", job_id="not-a-realjob",
            require_persistent_tracking=True)
    assert provider_touched == []


def test_la_fila_de_tope_no_borrada_no_cuenta_como_gasto():
    """Cuando el DELETE falla (un rol con UPDATE pero sin DELETE, o un fallo
    transitorio) la fila se cierra con `budget_exceeded:`. No hubo llamada al
    proveedor: si esa fila se cuenta, cada rechazo del tope fabrica gasto y
    encima hace avanzar el propio tope."""
    from provenance import BUDGET_EXCEEDED_PREFIX, billable_filter
    sql = str(billable_filter().compile(
        compile_kwargs={"literal_binds": True}))
    assert BUDGET_EXCEEDED_PREFIX in sql
    # Y el resumen que escribe el fallback tiene que matchear ese prefijo.
    import inspect

    import pipeline
    src = inspect.getsource(pipeline._discard_provenance_row)
    assert f'response_summary="{BUDGET_EXCEEDED_PREFIX}' in src


def test_el_tope_no_se_reintenta():
    """El loop de 2 intentos de `_ensure_background` duerme 30s antes de
    reintentar. El tope no puede cambiar durante esa espera: reintentar sólo
    agrega latencia y otro ciclo de reserva+borrado de fila."""
    import inspect

    import pipeline
    src = inspect.getsource(pipeline._ensure_background)
    i_budget = src.index("except VeoBudgetExceeded")
    i_generic = src.index("except Exception as e:", i_budget - 4000)
    assert i_budget < i_generic, (
        "el handler específico tiene que ir ANTES del genérico o nunca corre")
    # Y tiene que cortar el loop, no dormir.
    bloque = src[i_budget:i_budget + 500]
    assert "break" in bloque and "sleep" not in bloque


def test_la_reserva_concurrente_usa_lock_atomico_no_orden_de_ids():
    import inspect
    """`_generate_scene_clips` manda las escenas por un ThreadPoolExecutor y
    cada proceso puede commitear en un orden distinto al de la secuencia. El
    count y la reserva deben vivir bajo un lock de base compartido por todos
    los workers, nunca bajo una comparación de ids.
    """
    import pipeline
    src = inspect.getsource(pipeline._veo_budget_exceeded)
    assert "pg_advisory_xact_lock" in src
    assert "AIProvenance.id < own_row_id" not in src


def test_seis_escenas_concurrentes_reservan_exactamente_cinco(db, monkeypatch):
    """Integración real en PostgreSQL: con cinco llamadas históricas y cinco
    lugares libres, seis transacciones simultáneas admiten exactamente cinco.
    SQLite local no implementa advisory locks; CI ejecuta este caso en PG."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone

    import pipeline
    from database import AIProvenance, Job, User, engine
    from provenance import BUDGET_PENDING_PREFIX, BUDGET_RESERVED_PREFIX

    if engine.dialect.name != "postgresql":
        pytest.skip("la carrera cross-process requiere PostgreSQL")

    job_id = "atomicbudget"
    db.query(Job).filter(Job.job_id == job_id).delete()
    user = User(username="atomic-budget-user", hashed_password="unused",
                tenant_id="atomic-tenant")
    db.add(user)
    db.flush()
    db.add(Job(job_id=job_id, user_id=user.id, tenant_id="atomic-tenant",
               artist="Bersuit", song_title="La Argentinidad",
               filename="a.mp3", status="processing"))
    db.flush()
    now = datetime.now(timezone.utc)
    for _ in range(5):
        db.add(AIProvenance(
            job_id=job_id, step="video_bg",
            tool_name="veo-3.1-fast-generate-001",
            tool_provider="google_vertex", prompt_sent="historical",
            response_summary="provider_ok", created_at=now,
        ))
    pending = []
    for _ in range(6):
        row = AIProvenance(
            job_id=job_id, step="video_bg",
            tool_name="veo-3.1-fast-generate-001",
            tool_provider="google_vertex", prompt_sent="pending",
            response_summary=BUDGET_PENDING_PREFIX, created_at=now,
        )
        db.add(row)
        pending.append(row)
    db.commit()

    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
    row_ids = [row.id for row in pending]
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda rid: pipeline._veo_budget_exceeded(job_id, rid),
                row_ids,
            ))
        assert sum(not over for over, _spent in results) == 5
        assert sum(over for over, _spent in results) == 1
        db.expire_all()
        reserved = db.query(AIProvenance).filter(
            AIProvenance.id.in_(row_ids),
            AIProvenance.response_summary.like(
                f"{BUDGET_RESERVED_PREFIX}%"),
        ).count()
        assert reserved == 5
    finally:
        db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.query(User).filter(User.username == "atomic-budget-user").delete()
        db.commit()
