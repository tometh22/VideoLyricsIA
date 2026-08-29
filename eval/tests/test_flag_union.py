from eval.flag_union import _merge_duration, _stats


def test_merge_duration_uses_union_not_sum():
    assert _merge_duration([(0, 3), (2, 5), (7, 8)]) == 6


def test_flag_stats_count_real_and_false_flags():
    rows = [
        {"song_id": "a", "label": 1, "correction_events": 2, "predictor": .9, "timing": 0,
         "start_s": 0, "end_s": 3, "song_duration_s": 10},
        {"song_id": "a", "label": 0, "correction_events": 0, "predictor": .8, "timing": 0,
         "start_s": 2, "end_s": 5, "song_duration_s": 10},
        {"song_id": "a", "label": 1, "correction_events": 1, "predictor": .1, "timing": .1,
         "start_s": 8, "end_s": 10, "song_duration_s": 10},
    ]
    stats = _stats(rows, .5, .5)
    assert stats["corrected_line_recall"] == .5
    assert stats["correction_event_recall"] == 2 / 3
    assert stats["false_flags"] == 1
    assert stats["flagged_audio_seconds"] == 5
