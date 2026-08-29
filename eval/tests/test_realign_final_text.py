from eval.realign_final_text import (
    ESPEAK_LANGUAGES,
    _group_ranges,
    _lines_from_word_spans,
    _neutral_segments,
    _raw_occurrence_anchors,
    _score_prediction,
)


def _line(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_neutral_segments_depend_on_text_not_approved_timing():
    left = _neutral_segments([_line(50, 60, "a"), _line(100, 110, "bbb")], 40)
    right = _neutral_segments([_line(1, 2, "a"), _line(3, 4, "bbb")], 40)
    assert left == right
    assert left[0]["start"] == 0
    assert left[-1]["end"] == 40


def test_word_spans_reconstruct_lines_in_order():
    lines = _lines_from_word_spans(
        [_line(99, 100, "hola mundo"), _line(199, 200, "final")],
        ["hola", "mundo", "final"], [0, 0, 1],
        [(1.0, 1.2, .8), (1.3, 1.8, .9), (3.0, 3.5, .7)],
    )
    assert lines[0]["start"] == 1.0
    assert lines[0]["end"] == 1.8
    assert lines[1]["start"] == 3.0


def test_score_projects_zero_touch_only_when_raw_text_and_new_timing_are_good():
    approved = [_line(1, 2, "igual"), _line(3, 4, "corregida")]
    raw = [_line(1, 2, "igual"), _line(3, 4, "incorrecta")]
    prediction = [_line(1.05, 2.05, "igual"), _line(3.05, 4.05, "corregida")]
    row = _score_prediction("song", prediction, approved, raw)
    assert row["within_150ms_both"] == 1.0
    assert row["projected_zero_touch_lines"] == 1
    assert row["projected_ztlr"] == 0.5


def test_supported_catalog_languages_have_explicit_espeak_mapping():
    assert set(ESPEAK_LANGUAGES) == {"de", "en", "es", "fr", "it", "pt"}


def test_raw_occurrence_anchors_use_raw_not_approved_timing_and_fill_added_line():
    approved = [
        _line(100, 101, "primera"),
        _line(200, 201, "línea humana nueva"),
        _line(300, 301, "tercera"),
    ]
    raw = [_line(10, 11, "primera"), _line(20, 21, "tercera")]
    anchors = _raw_occurrence_anchors(approved, raw, 30)
    assert (anchors[0]["start"], anchors[0]["end"]) == (10, 11)
    assert anchors[0]["anchor_source"] == "exact_text_sequence_raw_line"
    assert anchors[1]["anchor_source"] == "interpolated_raw_gap"
    assert 11 <= anchors[1]["start"] < anchors[1]["end"] <= 20
    assert (anchors[2]["start"], anchors[2]["end"]) == (20, 21)


def test_group_ranges_never_leave_a_short_tail():
    assert _group_ranges(18) == [(0, 8), (8, 18)]
    assert _group_ranges(19) == [(0, 8), (8, 16), (16, 19)]
