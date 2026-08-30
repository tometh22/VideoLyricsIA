import json
from pathlib import Path

from eval.review_block_report import build


def test_incomplete_inputs_never_prepare_staging(tmp_path: Path):
    selector = tmp_path / "selector.json"
    pruning = tmp_path / "pruning.json"
    selector.write_text(json.dumps({
        "cohort_gate": {"status": "INCOMPLETE"}, "gate": {"status": "BLOCKED"},
        "operating_points": {}, "ztlr": {},
    }))
    pruning.write_text(json.dumps({
        "gate": {"status": "BLOCKED_INCOMPLETE_TIMING_SELECTOR"},
        "operating_points": {"recall_93": {}},
    }))
    result = build(selector, pruning, tmp_path / "missing.json", tmp_path / "out")
    assert result["gate"]["status"] == "BLOCKED_INCOMPLETE_REPLAY"
    assert result["staging_mutated"] is False
    assert "127.1 → PENDIENTE" in (tmp_path / "out" / "REPORT.md").read_text()


def test_conclusive_mss_no_go_closes_replay_without_downstream_propagation(tmp_path: Path):
    selector = tmp_path / "selector.json"
    pruning = tmp_path / "pruning.json"
    mss = tmp_path / "mss.json"
    selector.write_text(json.dumps({
        "cohort_gate": {"status": "COMPLETE"},
        "gate": {"status": "NO_GO_INSUFFICIENT_EVIDENCE"},
        "operating_points": {}, "ztlr": {},
    }))
    pruning.write_text(json.dumps({
        "gate": {"status": "NO_GO"},
        "operating_points": {"recall_93": {
            "queue_seconds_per_song": 100, "correction_recall": .93,
        }},
    }))
    mss.write_text(json.dumps({
        "comparison": {"gate": {"status": "NO_GO"}},
        "downstream_flag_replay_applied": False,
    }))
    result = build(selector, pruning, mss, tmp_path / "out")
    assert result["after_seconds_per_song"] == 100
    assert result["gate"]["status"] == "NO_GO"
