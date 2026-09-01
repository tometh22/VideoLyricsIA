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


class RecognitionCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self._completed_attempt_count = 0
        self._hypotheses: list[dict] = []

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
            }


_CURRENT: ContextVar[RecognitionCollector | None] = ContextVar(
    "recognition_collector", default=None,
)


def begin_collection() -> tuple[RecognitionCollector, Token]:
    collector = RecognitionCollector()
    return collector, _CURRENT.set(collector)


def end_collection(token: Token) -> None:
    _CURRENT.reset(token)


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
