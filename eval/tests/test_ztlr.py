from eval.ztlr import _song_row


def _line(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_ztlr_counts_unchanged_text_and_timing_only_once():
    row = _song_row(
        "song",
        [_line(0, 1, "igual"), _line(2, 3, "texto"), _line(4, 5, "tiempo")],
        [_line(0, 1, "igual"), _line(2, 3, "otro"), _line(4.2, 5, "tiempo")],
    )
    assert row["work_units"] == 3
    assert row["zero_touch_lines"] == 1
    assert row["text_only_touched"] == 1
    assert row["timing_only_touched"] == 1
    assert row["ztlr"] == 1 / 3


def test_ztlr_denominator_includes_added_and_deleted_lines():
    row = _song_row(
        "song",
        [_line(0, 1, "queda"), _line(10, 11, "borrada")],
        [_line(0, 1, "queda"), _line(20, 21, "agregada")],
    )
    assert row["work_units"] == 3
    assert row["zero_touch_lines"] == 1
    assert {unit["category"] for unit in row["units"]} == {
        "zero_touch", "added_line", "deleted_line",
    }
