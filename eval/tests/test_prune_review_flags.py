import csv
import json

from eval.prune_review_flags import _evaluate, _selector_rows


def test_timing_selector_removes_only_timing_source_and_safe_counts_as_found():
    rows = [{
        "song_id": "a", "line_idx": 0, "category": "timing_only",
        "label_any": 1, "label_timing": 1,
        "text_oof_probability": 0.1, "timing_oof_probability": 0.9,
        "timing_selector_approved": 1, "timing_selector_safe": 1,
    }]
    result = _evaluate(rows, {"a": 0}, .5, .5)
    assert result["selected_lines"] == 0
    assert result["auto_resolved_safe_timing_lines"] == 1
    assert result["correction_recall"] == 1.0


def test_text_flag_survives_timing_auto_resolution():
    rows = [{
        "song_id": "a", "line_idx": 0, "category": "text_and_timing",
        "label_any": 1, "label_timing": 1,
        "text_oof_probability": 0.9, "timing_oof_probability": 0.9,
        "timing_selector_approved": 1, "timing_selector_safe": 1,
    }]
    result = _evaluate(rows, {"a": 0}, .5, .5)
    assert result["selected_lines"] == 1


def test_selector_rows_never_enable_failed_selector_gate(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps({
        "cohort_gate": {"status": "COMPLETE"},
        "gate": {"status": "NO_GO_INSUFFICIENT_EVIDENCE"},
        "operating_points": {"0.9": {"threshold": 0.8}},
        "songs": 10,
    }))
    with (tmp_path / "oof_lines.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "song_id", "line_idx", "hierarchical_abstained", "oof_probability", "label_safe",
        ])
        writer.writeheader()
        writer.writerow({
            "song_id": "song", "line_idx": 1, "hierarchical_abstained": 0,
            "oof_probability": 0.99, "label_safe": 1,
        })
    rows, metadata = _selector_rows(tmp_path)
    assert rows[("song", 1)]["approved"] is False
    assert metadata["auto_resolution_enabled"] is False
