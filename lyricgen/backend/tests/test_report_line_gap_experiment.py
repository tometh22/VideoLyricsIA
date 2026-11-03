"""Métricas por brazo de la prueba del aire mínimo."""
from __future__ import annotations

import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_line_gap_experiment", BACKEND / "scripts" / "report_line_gap_experiment.py",
)
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)


def test_boundary_edits_separates_starts_and_ends():
    machine = [{"_id": "a", "text": "uno", "start": 1.0, "end": 2.5},
               {"_id": "b", "text": "dos", "start": 3.0, "end": 4.5}]
    approved = [{"_id": "a", "text": "uno", "start": 1.2, "end": 2.5},
                {"_id": "b", "text": "dos", "start": 3.0, "end": 4.1}]
    edits = ab.boundary_edits(machine, approved)
    assert edits["complete"] and edits["lines"] == 2
    assert [round(v, 3) for v in edits["starts"]] == [0.2]
    assert [round(v, 3) for v in edits["ends"]] == [0.4]


def test_summarize_arm_counts_only_approved_jobs():
    rows = [
        {"approved": True, "edits": {"lines": 10, "starts": [0.2], "ends": [0.4, 0.6]},
         "heartbeats": 5, "active_minutes": 4.0, "lines_trimmed": 3},
        {"approved": False, "edits": {"lines": 0, "starts": [], "ends": []},
         "heartbeats": 0, "active_minutes": 0.0, "lines_trimmed": 1},
    ]
    summary = ab.summarize_arm(rows)
    assert summary["assigned"] == 2 and summary["approved"] == 1
    assert summary["end_edits_per_line"] == 0.2
    assert summary["end_edit_magnitude_median_s"] == 0.5
    assert summary["minutes_per_job_median"] == 4.0
    assert summary["lines_trimmed_per_job_median"] == 2
