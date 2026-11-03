"""Clasificación de fines de línea y simulación de la regla de fin."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "diagnose_line_ends", BACKEND / "scripts" / "diagnose_line_ends.py",
)
ends = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ends)


def _row(start, end, last_word, approved_end=None, *, words=True, origin="ctc"):
    return {
        "job_id": "j", "origin": origin, "has_words": words, "matched": True,
        "text_changed": False, "alignment_complete": True,
        "machine_start": start, "machine_end": end,
        "raw_end": last_word if words else None,
        "approved_start": start, "approved_end": approved_end if approved_end is not None else end,
    }


def test_end_categories():
    assert ends.end_category(_row(0, 2.5, 2.0), 5.0) == "last_word_plus_hold"
    assert ends.end_category(_row(0, 2.29, 2.0), 2.3) == "stretched_to_next_hold_clamped"
    assert ends.end_category(_row(0, 2.0, 2.0), 5.0) == "at_last_word"
    assert ends.end_category(_row(0, 4.99, None, words=False), 5.0) == "stretched_to_next"
    assert ends.end_category(_row(0, 2.0, 2.0), None) == "last_line"


def test_rule_end_caps_at_next_start_minus_gap():
    row = _row(0, 2.5, 2.0)
    assert ends.rule_end(row, 5.0, hold=0.15, gap=0.3) == pytest.approx(2.15)
    assert ends.rule_end(row, 2.3, hold=0.15, gap=0.3) == pytest.approx(2.0)
    assert ends.rule_end(_row(0, 2.5, None, words=False), 5.0, hold=0.15, gap=0.3) == 2.5


def test_simulate_rule_counts_removed_and_created():
    grouped = {"j": [
        _row(0.0, 2.5, 2.0, approved_end=2.15),   # editada hoy; la regla la acierta
        _row(3.0, 5.5, 5.0, approved_end=5.5),    # aceptada hoy; la regla la cambia
        _row(9.0, 10.0, 10.0),
    ]}

    result = ends.simulate_rule(grouped, hold=0.15, gap=0.3)

    assert (result["end_edits_now"], result["removed"], result["created"]) == (1, 1, 1)
    assert result["net"] == 0
