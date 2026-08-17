"""Guardrail "nunca degradar" — el video final tiene que salir tal cual lo
pidió el operador; NUNCA se entrega un fondo degradado (gradiente / Ken Burns).

Decisión de producto: aplica a TODOS los tenants. Cuando Veo no puede producir
el fondo pedido, el pipeline reintenta la generación en vez de entregar un
gradiente; al agotar los reintentos escala a error (nunca degrada en silencio).

Estos tests cubren las funciones puras del guardrail. La integración con el
Retry de RQ (re-lanzar BackgroundDegraded → reintento con backoff → error al
agotar) se apoya en la maquinaria ya probada de enqueue_pipeline.
"""

import pytest

import pipeline as p


# ---------------------------------------------------------------------------
# Kill-switch: OFF por default (deploy inerte)
# ---------------------------------------------------------------------------

def test_guard_apagado_por_default(monkeypatch):
    monkeypatch.delenv("VEO_NO_DEGRADE_GUARD", raising=False)
    assert p._veo_no_degrade_guard_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_guard_se_prende_con_valores_truthy(monkeypatch, val):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", val)
    assert p._veo_no_degrade_guard_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no", ""])
def test_guard_apagado_con_valores_falsy(monkeypatch, val):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", val)
    assert p._veo_no_degrade_guard_enabled() is False


# ---------------------------------------------------------------------------
# Detección por nombre de archivo — cubre los 15 caminos de degradación
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "bg_gradient_fallback.mp4",
    "bg_policy_safe_fallback.mp4",
    "bg_scene_ambiguous_fallback.mp4",
    "bg_scene_cancelled_fallback.mp4",
    "bg_initial_policy_fallback.mp4",
    "bg_edit_policy_fallback.mp4",
])
def test_detecta_todos_los_gradientes_de_fallback(name):
    assert p._is_deterministic_fallback_bg(f"/tmp/job123/{name}") is True


@pytest.mark.parametrize("name", [
    "bg_generated.mp4",           # Veo real
    "bg_scene_timeline.mp4",      # multi-escena real
    "user_uploaded_photo.jpg",    # foto del operador
    "bg_looped.mp4",
])
def test_no_marca_los_fondos_reales(name):
    assert p._is_deterministic_fallback_bg(f"/tmp/job123/{name}") is False


def test_none_no_es_fallback():
    assert p._is_deterministic_fallback_bg(None) is False
    assert p._is_deterministic_fallback_bg("") is False


# ---------------------------------------------------------------------------
# El guard: no-op cuando corresponde, levanta cuando degrada
# ---------------------------------------------------------------------------

def test_guard_off_no_hace_nada_aunque_haya_gradiente(monkeypatch):
    monkeypatch.delenv("VEO_NO_DEGRADE_GUARD", raising=False)
    # No levanta: con el guard apagado se conserva el comportamiento histórico.
    p._guard_against_degraded_delivery(
        "job1",
        bg_image_path="/tmp/job1/bg_gradient_fallback.mp4",
        is_deterministic_fallback=True,
        animation_degraded=False,
    )


def test_guard_on_deja_pasar_el_fondo_real(monkeypatch):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    # No levanta: el fondo es el Veo real pedido.
    p._guard_against_degraded_delivery(
        "job1",
        bg_image_path="/tmp/job1/bg_generated.mp4",
        is_deterministic_fallback=False,
        animation_degraded=False,
    )


def test_guard_on_corta_ante_gradiente_por_flag(monkeypatch):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    with pytest.raises(p.BackgroundDegraded):
        p._guard_against_degraded_delivery(
            "job1",
            bg_image_path="/tmp/job1/bg_generated.mp4",  # flag manda igual
            is_deterministic_fallback=True,
            animation_degraded=False,
        )


def test_guard_on_corta_ante_gradiente_por_nombre_de_archivo(monkeypatch):
    # El fallback profundo de _generate_veo_video no propaga flag al llamador;
    # el chequeo por nombre lo caza igual.
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    with pytest.raises(p.BackgroundDegraded):
        p._guard_against_degraded_delivery(
            "job1",
            bg_image_path="/tmp/job1/bg_gradient_fallback.mp4",
            is_deterministic_fallback=False,
            animation_degraded=False,
        )


def test_guard_on_corta_ante_animacion_degradada_a_ken_burns(monkeypatch):
    # Ken Burns usa la imagen del operador (no un archivo *fallback*), así que
    # sólo el flag animation_degraded lo señala.
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    with pytest.raises(p.BackgroundDegraded):
        p._guard_against_degraded_delivery(
            "job1",
            bg_image_path="/tmp/job1/user_uploaded_photo.jpg",
            is_deterministic_fallback=False,
            animation_degraded=True,
        )


# ---------------------------------------------------------------------------
# Clasificación por familia + mapeo a estado accionable (UX "el fondo necesita
# tu atención" en vez de "error"). provider = reintentar; content = ajustar.
# ---------------------------------------------------------------------------

def test_guard_clasifica_content_por_filename_de_politica(monkeypatch):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    with pytest.raises(p.BackgroundDegraded) as exc:
        p._guard_against_degraded_delivery(
            "job1",
            bg_image_path="/tmp/job1/bg_policy_safe_fallback.mp4",
            is_deterministic_fallback=True,
            animation_degraded=False,
        )
    assert exc.value.family == p.BG_DEGRADE_FAMILY_CONTENT


@pytest.mark.parametrize("name", [
    "bg_gradient_fallback.mp4",
    "bg_scene_ambiguous_fallback.mp4",
    "bg_initial_policy_fallback.mp4",
    "bg_edit_policy_fallback.mp4",
])
def test_guard_clasifica_provider_para_fallas_de_veo(monkeypatch, name):
    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    with pytest.raises(p.BackgroundDegraded) as exc:
        p._guard_against_degraded_delivery(
            "job1",
            bg_image_path=f"/tmp/job1/{name}",
            is_deterministic_fallback=False,
            animation_degraded=False,
        )
    assert exc.value.family == p.BG_DEGRADE_FAMILY_PROVIDER


def test_callback_mapea_background_degraded_a_estado_accionable():
    import queue_jobs

    class _Fake(p.BackgroundDegraded):
        pass

    # provider
    exc_prov = p.BackgroundDegraded("x", family="provider")
    cat, msg = queue_jobs._background_attention_from_exc(type(exc_prov), exc_prov)
    assert cat == "background_attention:provider"
    assert "error" not in msg.lower()      # nunca dice "error"
    assert "reintent" in msg.lower()       # accionable
    assert len(cat) <= 32                   # cabe en Job.error_category (VARCHAR(32))

    # content
    exc_cont = p.BackgroundDegraded("x", family="content")
    cat2, msg2 = queue_jobs._background_attention_from_exc(type(exc_cont), exc_cont)
    assert cat2 == "background_attention:content"
    assert "error" not in msg2.lower()
    assert len(cat2) <= 32


def test_callback_no_toca_errores_comunes():
    import queue_jobs
    assert queue_jobs._background_attention_from_exc(RuntimeError, RuntimeError("boom")) is None
    assert queue_jobs._background_attention_from_exc(None, None) is None
