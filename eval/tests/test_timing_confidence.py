from eval.timing_confidence import _display_edges, _operating_point


def test_display_edges_apply_loo_display_delta():
    line = {
        "predicted_start": 10.0, "predicted_end": 12.0,
        "start_display_delta_ms": -60, "end_display_delta_ms": 250,
    }
    assert _display_edges(line) == (9.94, 12.25)


def test_operating_point_never_approves_hierarchical_abstention():
    rows = [
        {"song_id": "a", "label_safe": 1, "hierarchical_abstained": 0, "oof_probability": .9},
        {"song_id": "b", "label_safe": 0, "hierarchical_abstained": 1, "oof_probability": 1.0},
    ]
    point = _operating_point(rows, .9)
    assert point["approved_lines"] == 1
    assert point["precision"] == 1.0
