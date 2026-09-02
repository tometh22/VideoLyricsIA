"""Paridad REAL de tipografía video/short (incidente UMG Chile, 3ª vuelta
2026-06-12: misma TTF en dos motores ≠ misma letra en pantalla).

El short ahora quema el texto con LIBASS — el mismo motor del video. Este
test compara el Style line del documento ASS del short contra el que el
video deriva para los mismos parámetros: familia, bold, fontsize base,
outline, shadow y colores deben ser IDÉNTICOS. Si alguien vuelve a
divergir las derivaciones, esto rompe.
"""
import os
import re

import pipeline
from pipeline import _build_short_ass_doc, _CONTRAST_SETTINGS
import ass_render as _ass

FREDOKA = os.path.join(os.path.dirname(pipeline.__file__), "fonts", "Fredoka-SemiBold.ttf")
SEGS = [
    {"start": 0.5, "end": 3.0, "text": "Que late mi corazón"},
    {"start": 3.5, "end": 7.0, "text": "Tú controlas toda mi verdad"},
]


def _style_line(doc: str) -> str:
    return next(l for l in doc.splitlines() if l.startswith("Style: Lyric,"))


def _video_style_reference(font_path, text_contrast, font_scale,
                           lyric_color="", lyric_sung_color="", animation="none"):
    """Reconstruye el Style que _render_lyrics_ass deriva para el VIDEO
    1080p (scale=1.0) con los mismos params — la referencia de paridad."""
    scale = 1.0
    contrast = _CONTRAST_SETTINGS.get(text_contrast, _CONTRAST_SETTINGS["medium"])
    outline = max(1.0, contrast["stroke_mult"] * scale)
    shadow = max(1, int(round(3 * scale)))
    family, bold = _ass.font_family(font_path)
    font_factor = _ass.font_size_factor(family)
    base_fs = _ass.lyric_fontsize(40, scale, font_scale, font_factor=font_factor)
    if animation == "karaoke":
        primary, secondary = lyric_sung_color or "", lyric_color or ""
    else:
        primary, secondary = lyric_color or "", ""
    doc = _ass.build_ass(
        width=1920, height=1080, font_name=family, base_fontsize=base_fs,
        outline=outline, shadow=shadow, lines=[], bold=bold,
        primary_color=primary, secondary_color=secondary,
    )
    return _style_line(doc)


def test_short_style_identical_to_video_style():
    """Familia, bold, fs, outline, shadow y colores idénticos al video."""
    for contrast in ("subtle", "medium", "strong"):
        for animation in ("none", "karaoke", "pop"):
            short_doc = _build_short_ass_doc(
                SEGS, font_path=FREDOKA, text_case="original",
                font_scale=1.0, lyric_color="#FFD700",
                lyric_sung_color="#FF00FF", text_contrast=contrast,
                lyrics_animation=animation, line_transition="none",
            )
            video_style = _video_style_reference(
                FREDOKA, contrast, 1.0,
                lyric_color="#FFD700", lyric_sung_color="#FF00FF",
                animation=animation,
            )
            assert _style_line(short_doc) == video_style, (contrast, animation)


def test_short_doc_is_vertical_with_real_family():
    doc = _build_short_ass_doc(
        SEGS, font_path=FREDOKA, text_case="upper", font_scale=1.0,
        lyric_color="", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="none", line_transition="none",
    )
    assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc
    family, bold = _ass.font_family(FREDOKA)
    assert f"Style: Lyric,{family}," in doc
    # Las dos líneas de la ventana están como eventos, en MAYÚSCULAS
    assert "QUE LATE MI CORAZÓN" in doc
    assert doc.count("Dialogue:") >= 2


def test_short_karaoke_now_has_per_word_payload():
    """Antes el short NO replicaba karaoke (limitación del motor moviepy);
    con libass los payloads per-word (\\k) vienen gratis."""
    doc = _build_short_ass_doc(
        SEGS, font_path=FREDOKA, text_case="original", font_scale=1.0,
        lyric_color="", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="karaoke", line_transition="none",
    )
    assert re.search(r"\\k[f]?\d+", doc), "faltan tags de karaoke en el short"


def test_generate_short_uses_libass_with_moviepy_fallback():
    """El efecto toca el fondo antes de libass; moviepy queda como fallback."""
    import inspect
    src = inspect.getsource(pipeline.generate_short)
    assert "_apply_short_effect(" in src
    assert "_burn_short_text_ass(" in src
    assert "_make_short_text_clip(" in src  # fallback presente
    assert src.index("_apply_short_effect(") < src.index("_burn_short_text_ass(")
    assert src.index("_burn_short_text_ass(") < src.index("_make_short_text_clip(")


def test_fallback_alerts_sentry():
    """El fallback moviepy es el ÚNICO camino que puede reproducir la
    divergencia tipográfica — debe alertar en Sentry, no degradar en
    silencio (regresión textual)."""
    import inspect
    import pipeline as _p
    src = inspect.getsource(_p.generate_short)
    assert "short-libass-fallback" in src
    # El capture_message vive en el helper compartido _alert_sentry, no
    # inline: verificamos que el helper se use acá Y que realmente alerte.
    assert "_alert_sentry(" in src
    assert "capture_message" in inspect.getsource(_p._alert_sentry)


def test_burn_short_returns_reason_on_ffmpeg_failure(tmp_path, monkeypatch):
    """El fallo de libass debe propagar el stderr de ffmpeg al caller para
    adjuntarlo al evento de Sentry (diagnosticabilidad: la causa raíz viaja
    con la alerta, sin correlacionar logs de worker por timestamp)."""
    class _FakeProc:
        returncode = 1
        stderr = "Fontconfig error: Cannot load default config file\nboom libass"

    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: _FakeProc())
    path, reason = pipeline._burn_short_text_ass(
        "bg_short.mp4", SEGS, str(tmp_path), 30.0, 30.0,
        font_path=FREDOKA, text_case="original", font_scale=1.0,
        lyric_color="", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="none", line_transition="none",
    )
    assert path is None
    assert reason is not None
    assert "rc=1" in reason
    assert "boom libass" in reason


def test_burn_short_returns_reason_on_exception(tmp_path, monkeypatch):
    """Si la pasada lanza excepción, el repr viaja en el reason (no None a
    secas): así el evento de Sentry del fallback trae el tipo y el mensaje."""
    def _boom(*a, **k):
        raise RuntimeError("libass unavailable")

    monkeypatch.setattr(pipeline.subprocess, "run", _boom)
    path, reason = pipeline._burn_short_text_ass(
        "bg_short.mp4", SEGS, str(tmp_path), 30.0, 30.0,
        font_path=FREDOKA, text_case="original", font_scale=1.0,
        lyric_color="", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="none", line_transition="none",
    )
    assert path is None
    assert reason is not None
    assert "RuntimeError" in reason
    assert "libass unavailable" in reason


def test_fallback_attaches_libass_error_to_sentry():
    """La rama de fallback debe pasar el motivo del fallo de libass como
    `extra` a `_alert_sentry`, y el helper debe adjuntarlo al scope con
    set_extra ANTES de capturar el mensaje — es el propósito del fix."""
    import inspect
    src = inspect.getsource(pipeline.generate_short)
    assert "libass_error" in src
    assert "extra=" in src
    helper_src = inspect.getsource(pipeline._alert_sentry)
    assert "set_extra(" in helper_src
    assert helper_src.index("set_extra(") < helper_src.index("capture_message(")


def test_bg_ilegible_alerta_en_vez_de_degradar_en_silencio():
    """Si el fondo del short no se puede usar, el short sale con degradé
    mientras el MASTER conserva el fondo real. Eso es una divergencia
    visible para el cliente: pasó con UMG Chile el 21-08 y nadie se enteró
    porque el except era mudo. Tiene que alertar."""
    import inspect
    import pipeline as _p
    src = inspect.getsource(_p.generate_short)
    assert "short-bg-source-unreadable" in src
    # Y el degradé sigue existiendo: alertar no significa dejar de degradar.
    assert "_make_gradient_clip(" in src


def test_short_no_abre_n_lectores_del_mismo_archivo():
    """El fondo corto se loopea con ffmpeg, no concatenando N VideoFileClip.

    El patrón viejo abría `ceil(short_dur/clip_dur)+1` lectores sobre el
    MISMO archivo y no cerraba ninguno (concatenate_videoclips no cascadea
    close()). Con VEO_CLIP_SECONDS=4 son 9 clips, y como los clips de Veo
    traen audio moviepy abre reader de video y de audio por clip: ~20
    procesos ffmpeg filtrados por job en un worker de vida larga. El master
    ya lo había erradicado en _get_background_clip_from_path; el short era
    el último sobreviviente."""
    import inspect
    import pipeline as _p
    src = inspect.getsource(_p.generate_short)
    # Con paréntesis: los comentarios la nombran para explicar por qué se fue.
    assert "concatenate_videoclips(" not in src
    assert "_prepare_short_bg(" in src
    # La rama de subclip abre UN VideoFileClip y está bien. Lo prohibido es
    # abrirlo DENTRO de un bucle: ninguna apertura de archivo puede escalar
    # con la duración del clip de fondo. Se chequea sobre el AST y no por
    # texto, porque las comprehensions de al lado (`... for c in layers`)
    # hacen que cualquier heurístico de líneas dé falsos positivos.
    import ast
    import textwrap
    arbol = ast.parse(textwrap.dedent(src))

    def _aperturas_en_bucle(nodo, dentro_de_bucle=False):
        encontradas = []
        for hijo in ast.iter_child_nodes(nodo):
            _bucle = dentro_de_bucle or isinstance(
                hijo, (ast.For, ast.AsyncFor, ast.While,
                       ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            )
            if (dentro_de_bucle and isinstance(hijo, ast.Call)
                    and isinstance(hijo.func, ast.Name)
                    and hijo.func.id == "VideoFileClip"):
                encontradas.append(hijo.lineno)
            encontradas.extend(_aperturas_en_bucle(hijo, _bucle))
        return encontradas

    culpables = _aperturas_en_bucle(arbol)
    assert not culpables, (
        f"VideoFileClip abierto dentro de un bucle (líneas relativas {culpables}) — "
        "ese es exactamente el patrón que filtraba un proceso ffmpeg por vuelta"
    )
