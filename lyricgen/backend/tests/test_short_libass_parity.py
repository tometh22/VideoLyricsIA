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
    """El recableado: pasada libass primero, moviepy solo como fallback."""
    import inspect
    src = inspect.getsource(pipeline.generate_short)
    assert "_burn_short_text_ass(" in src
    assert "_make_short_text_clip(" in src  # fallback presente
    assert src.index("_burn_short_text_ass(") < src.index("_make_short_text_clip(")


def test_fallback_alerts_sentry():
    """El fallback moviepy es el ÚNICO camino que puede reproducir la
    divergencia tipográfica — debe alertar en Sentry, no degradar en
    silencio (regresión textual)."""
    import inspect
    import pipeline as _p
    src = inspect.getsource(_p.generate_short)
    assert "short-libass-fallback" in src
    assert "capture_message" in src
