"""Los minutos de revisor se suman sobre todas las sesiones, no sobre una.

La derivación vieja (derive_server_active_edit_ms) filtra a una session_id y
exige activity_seq contiguos: en producción reportó 9,0 s para una canción con
183 latidos repartidos en 5 sesiones (~46 min reales de trabajo).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_reviewer_minutes", BACKEND / "scripts" / "report_reviewer_minutes.py",
)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

BASE = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)


def _beats(offsets_s: list[float]) -> list[datetime]:
    return [BASE + timedelta(seconds=value) for value in offsets_s]


def test_contiguous_beats_sum_the_whole_span():
    stamps = _beats([0, 15, 30, 45, 60])
    assert report.active_seconds(stamps, 25.0) == 60.0


def test_beats_from_several_sessions_still_count():
    # Dos tandas separadas por 10 minutos (el revisor recargó y volvió).
    stamps = _beats([0, 15, 30]) + _beats([600, 615, 630])
    # Cuenta ambas tandas (30 s + 30 s) y descarta el hueco largo.
    assert report.active_seconds(stamps, 25.0) == 60.0


def test_long_gaps_are_not_counted_as_work():
    stamps = _beats([0, 3600])
    assert report.active_seconds(stamps, 25.0) == report.HEARTBEAT_INTERVAL_S


def test_single_beat_credits_one_nominal_interval():
    assert report.active_seconds(_beats([0]), 25.0) == report.HEARTBEAT_INTERVAL_S
    assert report.active_seconds([], 25.0) == 0.0


def test_order_does_not_matter():
    assert report.active_seconds(_beats([30, 0, 15]), 25.0) == 30.0


def test_percentile_helper():
    assert report._percentile([], 0.9) is None
    assert report._percentile([1.0, 2.0, 3.0, 10.0], 0.9) == 10.0


def test_live_detection_falls_back_to_filename():
    assert report._LIVE_RE.search("Los Pericos_Boulevard (Live).wav")
    assert report._LIVE_RE.search("Bersuit - Un Pacto (En Vivo).mp3")
    assert not report._LIVE_RE.search("Gondwana - Felicidad.wav")


def test_task_seconds_splits_by_task_across_sessions():
    """El desglose por tarea usa el mismo criterio que el total."""
    beats = [
        (BASE + timedelta(seconds=0), "listen"),
        (BASE + timedelta(seconds=15), "listen"),
        (BASE + timedelta(seconds=30), "text"),
        (BASE + timedelta(seconds=45), "timing"),
    ]
    assert report.task_seconds(beats, 25.0) == {"listen": 15.0, "text": 15.0, "timing": 15.0}


def test_task_seconds_attributes_the_gap_to_the_newer_beat():
    beats = [(BASE, "listen"), (BASE + timedelta(seconds=15), "text")]
    assert report.task_seconds(beats, 25.0) == {"text": 15.0}


def test_task_seconds_drops_long_gaps_and_unknown_labels():
    beats = [
        (BASE, "text"),
        (BASE + timedelta(seconds=600), "text"),
        (BASE + timedelta(seconds=615), "inventada"),
    ]
    assert report.task_seconds(beats, 25.0) == {"unknown": 15.0}


def test_task_seconds_handles_unordered_and_empty_input():
    beats = [(BASE + timedelta(seconds=15), "text"), (BASE, "listen")]
    assert report.task_seconds(beats, 25.0) == {"text": 15.0}
    assert report.task_seconds([], 25.0) == {}
