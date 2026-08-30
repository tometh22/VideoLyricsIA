from eval.prune_review_flags import _evaluate


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
