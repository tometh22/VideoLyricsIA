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
# Smoke de INTEGRACIÓN: una BackgroundDegraded levantada dentro de run_pipeline
# debe ESCAPAR (→ el Retry de RQ reintenta), NO ser tragada por el except
# genérico a status="error". Este es el contrato que hace posible "retener y
# reintentar" en vez de entregar el gradiente.
#
# Simulamos el fallo en una etapa temprana (transcribe) — así no hace falta
# mockear todo el pipeline pesado; lo que verificamos es el ruteo de la
# excepción a través del try/except externo, que es idéntico venga de la etapa
# que venga.
# ---------------------------------------------------------------------------

import os
from unittest.mock import patch


def test_background_degraded_escapa_de_run_pipeline(tmp_path, monkeypatch):
    import pipeline

    monkeypatch.setenv("VEO_NO_DEGRADE_GUARD", "1")
    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: None)
    monkeypatch.setattr(
        pipeline.storage, "download_object",
        lambda key, dest: (open(dest, "wb").write(b"x"), True)[1],
    )

    # transcribe está DENTRO del try externo de run_pipeline (mismo punto que
    # usan los tests de retry). Si en vez de un error genérico levanta
    # BackgroundDegraded, el except dedicado debe re-lanzarla; el except
    # genérico (que setea status=error y NO re-lanza) NO debe atraparla.
    def _raise_degraded(*a, **kw):
        raise pipeline.BackgroundDegraded("smoke: fondo degradado simulado")

    monkeypatch.setattr(pipeline, "transcribe", _raise_degraded)
    monkeypatch.setattr(pipeline, "_transcribe_via_openai_api", _raise_degraded)

    # Si el except genérico la tragara, run_pipeline retornaría sin levantar.
    # El contrato correcto: la excepción escapa → RQ la ve → reintenta.
    with pytest.raises(pipeline.BackgroundDegraded):
        pipeline.run_pipeline(
            job_id="smoke_degraded_1",
            mp3_path=None,
            artist="Test",
            style="oscuro",
            input_r2_key="inputs/x/y/track.wav",
        )


def test_error_generico_NO_escapa_de_run_pipeline(tmp_path, monkeypatch):
    # Contraprueba: un error común SÍ lo traga el except genérico (status=error,
    # sin re-lanzar). Esto confirma que el comportamiento del guardrail es
    # específico de BackgroundDegraded y no cambiamos el manejo del resto.
    import pipeline

    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(tmp_path))
    _status = {}
    monkeypatch.setattr(
        pipeline, "update_job",
        lambda *a, **kw: _status.update(kw) if kw else None,
    )
    monkeypatch.setattr(
        pipeline.storage, "download_object",
        lambda key, dest: (open(dest, "wb").write(b"x"), True)[1],
    )

    def _raise_generic(*a, **kw):
        raise RuntimeError("smoke: fallo transitorio común")

    monkeypatch.setattr(pipeline, "transcribe", _raise_generic)
    monkeypatch.setattr(pipeline, "_transcribe_via_openai_api", _raise_generic)

    # No levanta: el except genérico lo captura y marca error.
    pipeline.run_pipeline(
        job_id="smoke_generic_1",
        mp3_path=None,
        artist="Test",
        style="oscuro",
        input_r2_key="inputs/x/y/track.wav",
    )
    assert _status.get("status") == "error"
