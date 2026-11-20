"""Authenticated, stateless subtitle previews. Never render video or call AI."""
from pathlib import Path
import math
from pydantic import BaseModel, Field
from typing import Literal
from lyric_overlay import compose_lyric_overlay

FONTS = Path(__file__).parent / "fonts"


class PreviewSettings(BaseModel):
    font: str = Field(default="", max_length=64)
    text_case: Literal["upper", "lower", "title", "sentence", "original"] = "upper"
    font_scale: float = Field(default=1, ge=0.6, le=1.5, allow_inf_nan=False)
    text_contrast: Literal["subtle", "medium", "strong"] = "medium"
    lyrics_animation: Literal["none", "karaoke", "word_reveal", "pop", "glow"] = "none"
    line_transition: Literal["none", "slide_up", "slide_side", "wipe", "dissolve_blur"] = "none"
    lyric_color: str = Field(default="", max_length=7)
    lyric_sung_color: str = Field(default="", max_length=7)
    lyric_look: str = Field(default="", max_length=64)
    lyric_genre: str = Field(default="", max_length=64)
    lyric_variant: int | None = Field(default=None, ge=0, le=9999)
    lyric_font_main: str = Field(default="", max_length=64)
    lyric_font_key: str = Field(default="", max_length=64)
    lyric_alt_color: str = Field(default="", max_length=7)
    lyric_accent_color: str = Field(default="", max_length=7)


class TextPreviewRequest(BaseModel):
    segments: list[dict] = Field(max_length=500)
    settings: PreviewSettings = Field(default_factory=PreviewSettings)


def preview_font(filename):
    # Only bundled TTFs; no caller-provided filesystem paths.
    if Path(filename).name != filename or not filename.endswith(".ttf"):
        raise ValueError("Unknown font")
    path = FONTS / filename
    if not path.is_file():
        raise ValueError("Unknown font")
    return path


def build_text_preview(segments, settings, *, render_params=None, job_id="", spec=None):
    import pipeline
    import lyric_looks
    import lyric_look_runtime
    from editor import normalize_segments
    from render_spec import RenderSpec
    spec = spec or RenderSpec.youtube_default()
    segments = normalize_segments(segments)
    rp = dict(render_params or {})
    allowed = {
        "font", "text_case", "font_scale", "text_contrast", "lyrics_animation",
        "line_transition", "lyric_color", "lyric_sung_color", "lyric_look",
        "lyric_genre", "lyric_variant", "lyric_font_main", "lyric_font_key",
        "lyric_alt_color", "lyric_accent_color",
    }
    rp.update({key: value for key, value in settings.items() if key in allowed})
    scale = float(rp.get("font_scale") or 1)
    if not math.isfinite(scale):
        raise ValueError("Invalid font scale")
    look_id = rp.get("lyric_look") or ""
    if look_id and not lyric_looks.get_look(look_id):
        raise ValueError("Unknown look")
    opts, _ = lyric_look_runtime.prepare_opts(
        job_id=job_id, lyric_look=look_id, render_params=rp, detect_genre=False,
    ) if look_id else ({}, {})
    look = lyric_look_runtime.resolve_for_render(look_id, opts) if look_id else None
    # Resolve Auto to a concrete choice which the editor publishes back to
    # its parent's render settings. Preview and export then use that choice.
    font_id = rp.get("font") or "montserrat-bold"
    font_path = lyric_looks.font_path(look) if look else pipeline._resolve_font(font_id)
    if not font_path:
        raise ValueError("Unknown font")
    fonts = lyric_looks.font_paths(look) if look else [font_path]
    content = compose_lyric_overlay(
        segments, width=spec.width, height=spec.height, text_scale=spec.text_scale, font_path=font_path,
        case_fn=lambda text: pipeline._apply_case(text, rp.get("text_case") or "upper"),
        font_scale=scale, text_contrast=rp.get("text_contrast") or "medium",
        lyrics_animation=rp.get("lyrics_animation") or "none",
        line_transition=rp.get("line_transition") or "none",
        lyric_color=rp.get("lyric_color") or "",
        lyric_sung_color=rp.get("lyric_sung_color") or "",
        look=look, look_opts=opts,
    )
    return {"ass": content, "fonts": sorted({Path(path).name for path in fonts}),
            "width": spec.width, "height": spec.height, "resolved_font": font_id if not look else None}
