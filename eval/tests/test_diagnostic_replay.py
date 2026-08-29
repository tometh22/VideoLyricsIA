from eval.train_timing import _collapse_endpoint_events, _feature_names


def test_t4_collapses_intermediate_mouse_drags_to_one_net_delta():
    collapsed = _collapse_endpoint_events([
        {"seq": 1, "before": 10.0, "after": 10.2},
        {"seq": 2, "before": 10.2, "after": 10.4},
        {"seq": 3, "before": 10.4, "after": 10.5},
    ])
    assert collapsed["event_count"] == 3
    assert collapsed["intermediate_drag_events"] == 2
    assert collapsed["target_delta_ms"] == 500.0
    assert collapsed["continuity_errors"] == 0


def test_t4_regressor_never_uses_start_delta_as_a_feature():
    row = {
        "song_id": "song", "line_idx": 1, "timing_touched": 1,
        "identity_score": 1.0, "target_delta_ms": 200.0,
        "event_count": 2, "first_before_s": 10.0, "last_after_s": 10.2,
        "final_snapshot_mismatch_ms": 0.0, "start_delta_ms": 450.0,
        "safe_feature": 1.0,
    }
    assert _feature_names([row], set()) == ["safe_feature"]
