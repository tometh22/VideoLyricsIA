from __future__ import annotations

from scripts.evaluate_t4_95 import evaluate


def test_t4_gate_separates_target_and_control():
    rows = [
        {"event_id": "t1", "population": "target", "baseline_error_ms": 1000, "proposed_error_ms": 500},
        {"event_id": "t2", "population": "target", "baseline_error_ms": 800, "proposed_error_ms": 400},
        {"event_id": "c1", "population": "control", "baseline_error_ms": 30, "proposed_error_ms": 80},
    ]
    report = evaluate(rows, min_improvement=0.2)
    assert report["gate"]["passed"] is True
    assert report["gate"]["mutation_allowed"] is False
    assert report["target_population"]["events"] == 2


def test_t4_rejects_control_damage():
    rows = [
        {"event_id": "t1", "population": "target", "baseline_error_ms": 1000, "proposed_error_ms": 500},
        {"event_id": "c1", "population": "control", "baseline_error_ms": 30, "proposed_error_ms": 250},
    ]
    report = evaluate(rows)
    assert report["gate"]["passed"] is False
    assert report["control_population"]["damage_event_ids"] == ["c1"]
