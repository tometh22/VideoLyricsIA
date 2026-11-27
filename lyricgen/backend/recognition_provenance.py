"""Invocation-level collection of private recognition hypotheses.

Provider wrappers record completed outputs here, before any caller chooses a
winner.  The orchestrator owns one collector per transcription and freezes its
snapshot at the single output chokepoint.  Context variables propagate through
``asyncio.to_thread``; the shared collector itself is lock-protected because
intro/body recognizers may finish concurrently.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from copy import deepcopy
from threading import Lock
from typing import Any


def bounded_provider_string(
    value: Any,
    *,
    label: str = "opaque-provider-value",
    limit: int = 2000,
) -> str:
    """Stringify an opaque SDK value without ever breaking provider success."""
    try:
        return str(value)[:max(0, int(limit))]
    except Exception:
        return f"<{label}-{type(value).__name__}>"


def provider_text_completion(
    value: Any,
    *,
    label: str = "opaque-provider-text",
) -> tuple[str, list[dict]]:
    """Freeze a provider text value without trusting its SDK wrapper.

    Some SDK response objects defer conversion until ``str()``.  A completed
    recognition call must remain durable even when that conversion fails, so
    return a bounded marker event instead of letting the accessor erase the
    attempt from the training sample.
    """
    try:
        text = "" if value is None else str(value)
    except Exception as exc:
        return "", [{
            "raw": f"<{label}-{type(value).__name__}>",
            "serialization_error": type(exc).__name__,
        }]
    return text, [{"text": text}]


def response_text_completion(
    response: Any,
    *,
    attribute: str = "text",
    label: str = "opaque-provider-response",
) -> tuple[str, list[dict]]:
    """Read a text attribute while preserving an unreadable completion."""
    try:
        value = getattr(response, attribute)
    except Exception as exc:
        return "", [{
            "raw": f"<{label}-{type(response).__name__}>",
            "serialization_error": type(exc).__name__,
        }]
    return provider_text_completion(value, label=label)


class RecognitionCollector:
    def __init__(
        self,
        *,
        completed_attempt_count: int = 0,
        hypotheses: list[dict] | None = None,
        window_outcomes: list[dict] | None = None,
    ) -> None:
        self._lock = Lock()
        self._completed_attempt_count = max(0, int(completed_attempt_count))
        self._hypotheses: list[dict] = deepcopy(hypotheses or [])
        # What happened to each vocal recognition window (attempts, language,
        # confidence, accepted/discarded). Operational evidence, not lyrics.
        self._window_outcomes: list[dict] = deepcopy(window_outcomes or [])

    def record_window_outcome(self, outcome: dict) -> None:
        if not isinstance(outcome, dict):
            return
        with self._lock:
            self._window_outcomes.append(deepcopy(outcome))

    def record_completed(
        self,
        *,
        family: str,
        events: Any,
        kind: str = "segments",
        view: str = "provider_input",
        transformation: str = "direct",
    ) -> None:
        """Count first, then serialize; a serialization loss fails closed."""
        with self._lock:
            attempt_id = self._completed_attempt_count
            self._completed_attempt_count += 1

        try:
            rows = [
                deepcopy(row) for row in (events or [])
                if isinstance(row, dict)
            ]
            for row in rows:
                row.pop("_recognition_family", None)
            hypothesis = {
                "attempt_id": attempt_id,
                "family": str(family or "").strip(),
                "kind": str(kind or "segments"),
                "events": rows,
                "view": str(view or "provider_input")[:64],
                "transformation": str(transformation or "direct")[:160],
            }
        except Exception:
            # The independent counter remains incremented.  The v3 validator
            # will see count != durable hypotheses and block approval/export.
            return

        with self._lock:
            self._hypotheses.append(hypothesis)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "completed_attempt_count": self._completed_attempt_count,
                "hypotheses": deepcopy(sorted(
                    self._hypotheses,
                    key=lambda item: int(item.get("attempt_id", -1)),
                )),
                "window_outcomes": deepcopy(self._window_outcomes),
            }


_CURRENT: ContextVar[RecognitionCollector | None] = ContextVar(
    "recognition_collector", default=None,
)


def begin_collection() -> tuple[RecognitionCollector, Token]:
    collector = RecognitionCollector()
    return collector, _CURRENT.set(collector)


def end_collection(token: Token) -> None:
    _CURRENT.reset(token)


def resume_from_result(result: dict) -> RecognitionCollector:
    """Continue collection through recognizers in post-processing."""
    existing = _CURRENT.get()
    if existing is not None:
        return existing
    hypotheses = [
        row for row in (result.get("_recognition_hypotheses") or [])
        if isinstance(row, dict)
    ]
    count = result.get("_recognition_attempt_count")
    if type(count) is not int:
        count = len(hypotheses)
    outcomes = [
        row for row in (result.get("_vocal_window_outcomes") or [])
        if isinstance(row, dict)
    ]
    collector = RecognitionCollector(
        completed_attempt_count=count,
        hypotheses=hypotheses,
        window_outcomes=outcomes,
    )
    _CURRENT.set(collector)
    return collector


def snapshot_into_result(result: dict) -> dict:
    collector = _CURRENT.get()
    if collector is None:
        return result
    snapshot = collector.snapshot()
    result["_recognition_hypotheses"] = snapshot["hypotheses"]
    result["_recognition_attempt_count"] = snapshot[
        "completed_attempt_count"
    ]
    result["_vocal_window_outcomes"] = snapshot["window_outcomes"]
    return result


def clear_collection() -> None:
    _CURRENT.set(None)


def record_completed(
    *,
    family: str,
    events: Any,
    kind: str = "segments",
    view: str = "provider_input",
    transformation: str = "direct",
) -> None:
    collector = _CURRENT.get()
    if collector is None:
        return
    collector.record_completed(
        family=family,
        events=events,
        kind=kind,
        view=view,
        transformation=transformation,
    )


def record_window_outcome(outcome: dict) -> None:
    collector = _CURRENT.get()
    if collector is not None:
        collector.record_window_outcome(outcome)


def window_outcomes(result: dict | None = None) -> list[dict]:
    """Outcomes from the active collector, else from a serialized result."""
    collector = _CURRENT.get()
    if collector is not None:
        return collector.snapshot()["window_outcomes"]
    return [row for row in ((result or {}).get("_vocal_window_outcomes") or [])
            if isinstance(row, dict)]
