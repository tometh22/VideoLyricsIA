from eval.mss_alt import _offset_segments


def test_offset_segments_moves_segments_and_words():
    out = _offset_segments([{
        "start": 1, "end": 2, "text": "hola",
        "words": [{"word": "hola", "start": 1.1, "end": 1.8}],
    }], 10)
    assert out[0]["start"] == 11
    assert out[0]["end"] == 12
    assert out[0]["words"][0]["start"] == 11.1
