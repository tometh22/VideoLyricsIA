"""Allowlisted visual profiles used by controlled Universal batches.

The public upload form exposes the individual render knobs.  Batch jobs use a
single JSON object instead so the exact visual contract can be audited and
replayed.  This module deliberately has no FastAPI or pipeline imports: it is
safe to use from the API, workers, tests and the local batch runner.
"""

from __future__ import annotations

import json
from typing import Any


ALLOWED_FONTS = frozenset({
    "poppins-bold", "montserrat-bold", "roboto-bold", "anton",
})
ALLOWED_STYLES = frozenset({"oscuro", "minimal", "calido", "neon"})
ALLOWED_MOVEMENTS = frozenset({"estatico", "foto-estatica"})
ALLOWED_EFFECTS = frozenset({"", "rain", "bokeh", "light", "fog"})
ALLOWED_TEXT_CASES = frozenset({"upper", "lower"})
ALLOWED_TRANSITIONS = frozenset({"cut", "fade"})
ALLOWED_BACKGROUND_TYPES = frozenset({"video", "photo"})
ALLOWED_KEYS = frozenset({
    "font", "font_scale", "text_case", "transition", "lyric_transition",
    "line_transition", "background_type", "movement", "movement_style",
    "effect", "style", "background_id", "genre", "concept",
})


class RenderProfileError(ValueError):
    """Raised when a batch profile contains an unsupported value."""


def normalize_render_profile(value: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse and validate a batch render profile.

    The returned object is canonical and JSON-safe.  ``transition`` remains
    the operator-facing ``cut``/``fade`` value for auditability; the pipeline
    value is derived separately by :func:`pipeline_fields`.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RenderProfileError("render_profile must be valid JSON") from exc
    if not isinstance(value, dict):
        raise RenderProfileError("render_profile must be an object")
    unknown = sorted(set(value) - ALLOWED_KEYS)
    if unknown:
        raise RenderProfileError(f"unsupported render_profile keys: {', '.join(unknown)}")

    def pick(*keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in value:
                return value[key]
        return default

    font = str(pick("font", default="")).strip().lower()
    if font not in ALLOWED_FONTS:
        raise RenderProfileError(f"font must be one of: {', '.join(sorted(ALLOWED_FONTS))}")

    try:
        font_scale = float(pick("font_scale", default=1.0))
    except (TypeError, ValueError) as exc:
        raise RenderProfileError("font_scale must be numeric") from exc
    if font_scale not in (1.0, 1.3):
        raise RenderProfileError("font_scale must be 1.0 or 1.3 for Universal batches")

    text_case = str(pick("text_case", default="upper")).strip().lower()
    if text_case not in ALLOWED_TEXT_CASES:
        raise RenderProfileError("text_case must be upper or lower")

    transition = str(pick("transition", "lyric_transition", default="cut")).strip().lower()
    if transition not in ALLOWED_TRANSITIONS:
        raise RenderProfileError("transition must be cut or fade")

    background_type = str(pick("background_type", default="video")).strip().lower()
    if background_type not in ALLOWED_BACKGROUND_TYPES:
        raise RenderProfileError("background_type must be video or photo")

    movement = str(pick("movement", "movement_style", default="estatico")).strip().lower()
    if movement not in ALLOWED_MOVEMENTS:
        raise RenderProfileError("movement must be estatico or foto-estatica")

    effect = str(pick("effect", default="")).strip().lower()
    if effect not in ALLOWED_EFFECTS:
        raise RenderProfileError(f"effect must be one of: {', '.join(sorted(ALLOWED_EFFECTS))}")

    style = str(pick("style", default="oscuro")).strip().lower()
    if style not in ALLOWED_STYLES:
        raise RenderProfileError(f"style must be one of: {', '.join(sorted(ALLOWED_STYLES))}")

    background_id = pick("background_id", default=None)
    if background_id is not None:
        try:
            background_id = int(background_id)
        except (TypeError, ValueError) as exc:
            raise RenderProfileError("background_id must be an integer") from exc
        if background_id <= 0:
            raise RenderProfileError("background_id must be positive")

    genre = str(pick("genre", default="")).strip()
    concept = str(pick("concept", default="")).strip()
    return {
        "font": font,
        "font_scale": font_scale,
        "text_case": text_case,
        "transition": transition,
        "background_type": background_type,
        "movement_style": movement,
        "effect": effect,
        "style": style,
        "background_id": background_id,
        "genre": genre,
        "concept": concept,
    }


def pipeline_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate the canonical batch contract to existing pipeline fields."""
    transition = profile["transition"]
    return {
        "font": profile["font"],
        "font_scale": profile["font_scale"],
        "text_case": profile["text_case"],
        "movement_style": profile["movement_style"],
        "effect": profile["effect"],
        "style": profile["style"],
        "genre": profile["genre"],
        "concept": profile["concept"],
        # line_transition is the non-deprecated renderer control.  Keep the
        # public profile's cut/fade terminology in render_params as well.
        "line_transition": "dissolve_blur" if transition == "fade" else "none",
        "lyric_transition": "cut",
    }
