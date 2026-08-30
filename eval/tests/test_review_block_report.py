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
