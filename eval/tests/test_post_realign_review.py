from eval.post_realign_review import _merged_seconds
from eval.report_hierarchical_realign import _descriptive


def test_review_audio_uses_interval_union():
    assert _merged_seconds([(0, 3), (2, 5), (8, 9)]) == 6


def test_hierarchical_descriptive_counts_tail_errors():
    rows = [{"song_id": "s", "per_line": [
        {"start_error_ms": 10, "end_error_ms": -20},
        {"start_error_ms": 3000, "end_error_ms": 100},
    ]}]
    result = _descriptive(rows)
    assert result["aligned_lines"] == 2
    assert result["boundaries_over_2s"] == 1
