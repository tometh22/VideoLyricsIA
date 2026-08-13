"""Unit tests for chorus_trim — pure annotation helpers."""

import chorus_trim as ct


def _s(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_detect_intro_offset_skips_short_intros():
    # Songs that start within 2s have no banner-worthy intro.
    assert ct.detect_intro_offset([_s(0.5, 2.0, "primera")]) == 0.0
    assert ct.detect_intro_offset([_s(1.9, 4.0, "primera")]) == 0.0


def test_detect_intro_offset_returns_real_intro():
    # El Arbol case: instrumental intro 53.0s before first sung line.
    assert ct.detect_intro_offset([_s(53.0, 56.7, "Después de tanto vagar")]) == 53.0


def test_detect_intro_offset_handles_empty_and_malformed():
    assert ct.detect_intro_offset([]) == 0.0
    assert ct.detect_intro_offset([{"start": "x", "end": 1.0, "text": "y"}]) == 0.0


def test_mark_repetitions_groups_identical_text():
    # El Arbol case: 'Para mí.' appears 4 times spread across the song.
    # All four should share the same repetition_group id.
    segs = [
        _s(53, 57, "Después de tanto vagar"),
        _s(86, 87, "Para mí."),
        _s(90, 91, "Para mí."),
        _s(94, 95, "Para mí."),
        _s(100, 101, "Otra línea"),
        _s(104, 105, "Para mí."),
    ]
    out = ct.mark_repetitions(segs)
    repetition_ids = [s.get("repetition_group") for s in out]
    # First line unique → no group. 'Para mí.' lines all share group 1.
    # 'Otra línea' unique → no group.
    assert repetition_ids == [None, 1, 1, 1, None, 1]


def test_mark_repetitions_assigns_groups_by_first_appearance():
    segs = [
        _s(0, 1, "Coro A"),
        _s(2, 3, "Coro B"),
        _s(4, 5, "Coro A"),    # 2nd 'Coro A' → confirms group 1
        _s(6, 7, "Coro B"),    # 2nd 'Coro B' → confirms group 2
        _s(8, 9, "Verso único"),
    ]
    out = ct.mark_repetitions(segs)
    assert out[0]["repetition_group"] == 1
    assert out[1]["repetition_group"] == 2
    assert out[2]["repetition_group"] == 1
    assert out[3]["repetition_group"] == 2
    assert "repetition_group" not in out[4]


def test_mark_repetitions_normalizes_text():
    # Casing + punctuation differences shouldn't split a chorus group.
    segs = [
        _s(0, 1, "Para mí."),
        _s(2, 3, "PARA MI"),
        _s(4, 5, "para  mi!"),
    ]
    out = ct.mark_repetitions(segs)
    assert all(s.get("repetition_group") == 1 for s in out)


def test_mark_repetitions_does_not_mutate_input():
    segs = [_s(0, 1, "Coro"), _s(2, 3, "Coro")]
    original = [dict(s) for s in segs]
    out = ct.mark_repetitions(segs)
    assert segs == original   # input untouched
    assert out is not segs


def test_mark_repetitions_min_count_three():
    # 'Coro' appears 2x; with min_count=3 it shouldn't get marked.
    segs = [_s(0, 1, "Coro"), _s(2, 3, "Coro"), _s(4, 5, "Verso")]
    out = ct.mark_repetitions(segs, min_count=3)
    assert all("repetition_group" not in s for s in out)


def test_annotate_returns_segs_and_metadata():
    segs = [
        _s(53.0, 57.0, "Después de tanto vagar"),
        _s(86.0, 87.0, "Para mí."),
        _s(90.0, 91.0, "Para mí."),
    ]
    annotated, meta = ct.annotate(segs)
    assert meta["intro_offset_s"] == 53.0
    assert meta["n_repetition_groups"] == 1
    assert annotated[1]["repetition_group"] == 1


def test_annotate_empty():
    assert ct.annotate([]) == ([], {})
