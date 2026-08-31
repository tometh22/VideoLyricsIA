"""Privacy-safe scalar validation for editor product analytics."""
from __future__ import annotations

import math
import re
import uuid

MAX_BY_NUMBER = {
    "duration_ms": 86_400_000,
    "active_edit_ms": 14_400_000,
    "position_ms": 86_400_000,
    "line_count": 10_000,
    "count": 10_000,
    "text_changes": 10_000,
    "timing_changes": 10_000,
    "lines_added": 10_000,
    "lines_removed": 10_000,
    "lines_reordered": 10_000,
    "revision": 1_000_000_000,
    "server_revision": 1_000_000_000,
    "local_revision": 1_000_000_000,
    "from_revision": 1_000_000_000,
    "to_revision": 1_000_000_000,
    "retry_count": 100,
    "media_error_code": 4,
    "status": 599,
}
SIGNED_NUMBERS = {"delta_ms"}
BOOLEANS = {"quality_acknowledged", "automatic_recovery_available"}
ENUMS = {
    "view": {"basic", "advanced"},
    "from": {"basic", "advanced"},
    "to": {"basic", "advanced"},
    "source": {"editor", "editor_v2", "legacy"},
    "method": {"modifier", "range", "paint"},
    "operation": {"edit", "resize_or_move", "delete"},
    # El cliente emite tres checkpoints: "draft" (debounce 800 ms), "autosave"
    # (checkpoint 5 s) y "manual" (flush al aprobar / botón Guardar). El enum
    # sólo aceptaba "draft", así que los otros dos hacían FALLAR la validación
    # y el evento entero se descartaba: `autosave_failures` venía contando un
    # subconjunto arbitrario y `avg_time_to_first_edit_ms`, que depende de
    # `editor_autosave_success`, quedaba sesgado.
    "checkpoint": {"draft", "autosave", "manual"},
}
SLUG_CATEGORIES = {"reason", "resolution", "context"}


def valid_property(key: str, value) -> bool:
    """Reject lyric strings, non-finite values and metric poisoning."""
    if key in MAX_BY_NUMBER:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))
            and 0 <= float(value) <= MAX_BY_NUMBER[key]
        )
    if key in SIGNED_NUMBERS:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and abs(float(value)) <= 86_400_000
        )
    if key in BOOLEANS:
        return isinstance(value, bool)
    if key == "session_id":
        if not isinstance(value, str):
            return False
        try:
            return str(uuid.UUID(value)) == value.lower()
        except ValueError:
            return bool(re.fullmatch(r"editor-[0-9]{10,16}-[a-z0-9]{4,20}", value))
    if key in ENUMS:
        return isinstance(value, str) and value in ENUMS[key]
    if key in SLUG_CATEGORIES:
        return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9_.:-]{1,40}", value))
    return False
