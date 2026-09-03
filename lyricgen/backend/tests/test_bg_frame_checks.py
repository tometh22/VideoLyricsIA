"""Chequeos de geometría y luz del fondo (puros, sin ffmpeg ni GPU).

Cubren dos reglas de aceptación que hasta ahora se pedían SÓLO en el prompt y
no se verificaban nunca sobre el archivo resultante:
  · 16:9 completo, sin franjas negras
  · iluminación estable durante toda la canción
"""

import ast
import io
import os

import bg_frame_checks as fc

_PIPELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "pipeline.py")
_SRC = io.open(_PIPELINE, encoding="utf-8").read()


def _body(name):
    for node in ast.walk(ast.parse(_SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"{name} no existe en pipeline.py")


# ── cropdetect ─────────────────────────────────────────────────────────────
def test_toma_la_ultima_estimacion_de_cropdetect():
    """cropdetect refina frame a frame; la primera suele venir de un fundido de
    entrada negro y reportaría barras donde no las hay."""
    salida = (
        "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:200 y2:879 crop=1920:680:0:200\n"
        "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:0 y2:1079 crop=1920:1080:0:0\n"
    )
    assert fc.parse_cropdetect(salida) == (1920, 1080, 0, 0)


def test_sin_cropdetect_no_hay_medicion():
    assert fc.parse_cropdetect("") is None
    assert fc.parse_cropdetect(None) is None
    assert fc.parse_cropdetect("ffmpeg version 7.1") is None


# ── letterbox ──────────────────────────────────────────────────────────────
def test_frame_completo_no_tiene_barras():
    r = fc.letterbox_report((1920, 1080, 0, 0), 1920, 1080)
    assert r["has_bars"] is False
    assert r["reason"] == "frame completo"


def test_detecta_letterbox_239():
    """El caso del incidente Spinetta: Veo hornea barras 2.39:1."""
    r = fc.letterbox_report((1920, 804, 0, 138), 1920, 1080)
    assert r["has_bars"] is True
    assert "horizontales" in r["reason"]
    assert r["top_bottom"] > 0.25


def test_detecta_pillarbox():
    r = fc.letterbox_report((1440, 1080, 240, 0), 1920, 1080)
    assert r["has_bars"] is True
    assert "verticales" in r["reason"]


def test_un_borde_de_dos_pixeles_no_es_letterbox():
    """Veo deja 1-2 px por redondeo de escalado; eso no se ve como barra y
    re-rollear por eso sería quemar un Veo al pedo."""
    r = fc.letterbox_report((1920, 1078, 0, 1), 1920, 1080)
    assert r["has_bars"] is False


def test_sin_medicion_falla_abierto():
    """Un bug midiendo no puede tirar un fondo bueno."""
    r = fc.letterbox_report(None, 1920, 1080)
    assert r["has_bars"] is False
    r = fc.letterbox_report((1920, 1080, 0, 0), 0, 0)
    assert r["has_bars"] is False


# ── luz ────────────────────────────────────────────────────────────────────
def test_luminancia_y_calidez():
    blanco = [(255, 255, 255)] * 4
    negro = [(0, 0, 0)] * 4
    assert fc.luminance(blanco) > 250
    assert fc.luminance(negro) == 0
    # Ámbar es cálido, azul es frío.
    assert fc.warmth([(230, 150, 60)] * 4) > 100
    assert fc.warmth([(40, 60, 200)] * 4) < -100


def test_una_sola_escena_siempre_es_coherente():
    assert fc.lighting_consistency([{"key": "a", "luminance": 100, "warmth": 0}])["consistent"]
    assert fc.lighting_consistency([])["consistent"]


def test_detecta_el_salto_de_dia_a_noche():
    sigs = [
        {"key": "verso_1", "luminance": 150, "warmth": 20},
        {"key": "coro_1", "luminance": 25, "warmth": -30},
    ]
    r = fc.lighting_consistency(sigs)
    assert r["consistent"] is False
    assert r["worst_delta"] == 125.0
    assert r["worst_pair"] == ("verso_1", "coro_1")
    assert r["offenders"][0]["pair"] == ("verso_1", "coro_1")


def test_variacion_normal_entre_planos_no_alerta():
    """Un plano abierto y uno cerrado del mismo momento difieren, pero no es un
    cambio de hora del día."""
    sigs = [
        {"key": "verso_1", "luminance": 120, "warmth": 30},
        {"key": "coro_1", "luminance": 100, "warmth": 25},
        {"key": "verso_2", "luminance": 130, "warmth": 35},
    ]
    assert fc.lighting_consistency(sigs)["consistent"] is True


def test_la_calidez_atrapa_el_atardecer_que_se_vuelve_mediodia():
    """Misma luminancia, distinto momento: un atardecer ámbar y un mediodía
    nublado pueden medir parecido en brillo y verse de horas distintas."""
    sigs = [
        {"key": "verso_1", "luminance": 110, "warmth": 70},
        {"key": "coro_1", "luminance": 108, "warmth": -5},
    ]
    r = fc.lighting_consistency(sigs)
    assert r["consistent"] is False
    assert r["offenders"][0]["warmth_delta"] == 75.0


def test_compara_contiguas_no_contra_el_promedio():
    """Oscurecerse de a poco de principio a fin no rompe; el salto en el corte
    sí. Extremos lejanos pero cada paso chico → coherente."""
    sigs = [{"key": f"s{i}", "luminance": 150 - i * 20, "warmth": 10}
            for i in range(6)]          # 150 -> 50, de a 20
    assert fc.lighting_consistency(sigs)["consistent"] is True


# ── directiva compartida ───────────────────────────────────────────────────
def test_la_directiva_de_luz_prohibe_cambiar_el_momento():
    d = fc.shared_light_directive("atardecer bajo y cálido desde el oeste")
    assert "atardecer bajo y cálido" in d
    assert "MISMO momento del día" in d
    assert "No pases de día a noche" in d


def test_sin_luz_no_hay_directiva():
    assert fc.shared_light_directive(None) == ""
    assert fc.shared_light_directive("  ") == ""


# ── cableado ───────────────────────────────────────────────────────────────
def test_las_franjas_negras_disparan_el_reroll():
    body = _body("_ensure_background")
    assert "_measure_letterbox(bg_path)" in body
    assert '_bars.get("has_bars")' in body
    assert "or bool(_bars.get(\"has_bars\"))" in body


def test_el_letterbox_persistente_avisa_por_sentry():
    """Fail-open igual que el corte de escena: se acepta el clip, pero el
    operador se entera antes que el cliente."""
    body = _body("_ensure_background")
    assert '_scope.fingerprint = ["bg-letterbox"]' in body


def test_la_biblia_define_una_luz_compartida_obligatoria():
    """`palette` describe colores, no un momento del día: dos escenas podían
    compartir paleta y estar una al mediodía y otra de noche."""
    body = _body("_build_visual_bible")
    # Sin "scene" al final: en el fuente esa frase cruza un salto de línea.
    assert "the TIME OF DAY and light state shared by every" in body
    assert "HARD CONSTRAINT" in body
    # También en el fallback sin LLM.
    assert '"light": "one single consistent time of day' in body


def test_todas_las_escenas_heredan_la_misma_luz():
    body = _body("_make_scene_prompt_fn")
    assert "shared_light" in body
    assert "shared_light_directive" in body
    assert "scene_context=_ctx" in body


def test_el_plan_registra_la_coherencia_de_luz():
    body = _body("_generate_scene_background")
    assert 'shared_light=(bible or {}).get("light")' in body
    assert 'plan["light_consistency"]' in body
    assert '_scope.fingerprint = ["scenes-light-jump"]' in body


def test_el_coro_repetido_no_cuenta_como_corte_de_luz():
    """Los coros comparten EL MISMO clip: compararlo consigo mismo daría 0 y
    ensuciaría la señal."""
    body = _body("_generate_scene_background")
    assert 'if _sigs and _sigs[-1].get("key") == _sec.recurrence_key:' in body


def test_medir_nunca_tumba_un_render():
    for fn in ("_measure_letterbox", "_scene_light_signature"):
        body = _body(fn)
        assert "except Exception" in body, fn
        assert "_raise_if_job_timeout(e)" in body, fn


# ── Simetría: el falso positivo que quema un Veo ───────────────────────────
def test_una_escena_nocturna_no_es_letterbox():
    """cropdetect recorta lo que ve negro (limit=24), así que un cielo oscuro
    arriba lo dispara igual que un letterbox. Re-rollear ahí quema un Veo por
    una escena que está bien. Un letterbox real es simétrico; un cielo no."""
    # 300 px oscuros arriba, nada abajo → recorte pegado a un borde.
    r = fc.letterbox_report((1920, 780, 0, 300), 1920, 1080)
    assert r["has_bars"] is False
    assert "asimétrico" in r["reason"]


def test_el_letterbox_real_es_simetrico():
    """2.39:1 sobre 16:9: 138 px arriba y 138 abajo."""
    r = fc.letterbox_report((1920, 804, 0, 138), 1920, 1080)
    assert r["has_bars"] is True
    assert r["symmetric"] is True


def test_pillarbox_simetrico_si_cuenta():
    r = fc.letterbox_report((1440, 1080, 240, 0), 1920, 1080)
    assert r["has_bars"] is True


def test_sombra_lateral_en_un_solo_borde_no_cuenta():
    r = fc.letterbox_report((1440, 1080, 480, 0), 1920, 1080)
    assert r["has_bars"] is False
