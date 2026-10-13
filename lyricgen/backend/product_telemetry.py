"""Privacy-safe scalar validation for editor product analytics."""
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timedelta, timezone

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
    "total": 10_000,
    "seconds": 60,
    "timing_count": 10_000,
    "text_count": 10_000,
    "vocalization_count": 10_000,
    "impact_ms": 3_600_000,
    "words_count": 10_000,
    "stale_words_count": 10_000,
    "one_side_count": 10_000,
    "no_words_count": 10_000,
    "played_ms": 86_400_000,
    "audio_duration_ms": 86_400_000,
}
# `editor_audio_played`: velocidad del <audio> (el navegador acepta 1/16..16).
BOUNDED_FLOATS = {"playback_rate": (0.0625, 16.0)}
# Tramos reproducidos: lista acotada de pares [inicio_ms, fin_ms]. 64 pares
# de 8 cifras entran en el tope de 2.000 caracteres por evento.
MAX_PLAYED_RANGES = 64
MAX_MEDIA_MS = 86_400_000
SIGNED_NUMBERS = {"delta_ms", "proposed_delta_ms", "chosen_delta_ms", "distance_to_proposal_ms"}
BOOLEANS = {"quality_acknowledged", "automatic_recovery_available"}
ENUMS = {
    "kind": {"generated", "shown", "examined", "active_seconds"},
    "decision": {"accepted", "edited", "rejected", "applied", "manual_edit", "manual_override", "edited_after_accept"},
    "suggestion_type": {"text", "timing", "vocalization", "unknown"},
    "confidence": {"high", "medium", "low", "unknown"},
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
    "structure_op": {"split", "merge"},
    "trigger": {"caret", "button", "bulk", "wrap_dialog", "reference", "backspace_empty"},
    "flush_reason": {"interval", "pause", "ended", "full", "approve", "hidden", "unmount"},
}
SLUG_CATEGORIES = {"reason", "resolution", "context"}


def _strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_played_ranges(value) -> bool:
    """Pares [inicio_ms, fin_ms] enteros dentro del audio; nunca texto."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PLAYED_RANGES:
        return False
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            return False
        start, end = pair
        if not (_strict_int(start) and _strict_int(end)):
            return False
        if not 0 <= start < end <= MAX_MEDIA_MS:
            return False
    return True


# El cliente manda la hora del evento; el reloj del navegador puede estar
# corrido o ser falso. Un evento no puede ocurrir después de llegar ni mucho
# antes (salen en el momento, o con keepalive al cerrar la pestaña): fuera de
# esa ventana se usa la hora de llegada del servidor.
OCCURRED_AT_MAX_PAST = timedelta(hours=24)


def resolve_occurred_at(raw, received_at: datetime) -> datetime:
    """Hora del evento: la del cliente si es plausible, si no la de llegada."""
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if received_at - OCCURRED_AT_MAX_PAST <= parsed <= received_at:
                return parsed
    return received_at


def valid_property(key: str, value) -> bool:
    """Reject lyric strings, non-finite values and metric poisoning."""
    if key == "ranges":
        return valid_played_ranges(value)
    if key in BOUNDED_FLOATS:
        low, high = BOUNDED_FLOATS[key]
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and low <= float(value) <= high
        )
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
    if key in {"proposal_id", "window_id", "candidate_id", "event_id"}:
        return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", value))
    if key == "pipeline_release":
        return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9_.+-]{1,64}", value))
    return False
