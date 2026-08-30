from eval.mss_alt import _comparison, _offset_segments


def test_offset_segments_moves_segments_and_words():
    out = _offset_segments([{
        "start": 1, "end": 2, "text": "hola",
        "words": [{"word": "hola", "start": 1.1, "end": 1.8}],
    }], 10)
    assert out[0]["start"] == 11
    assert out[0]["end"] == 12
    assert out[0]["words"][0]["start"] == 11.1


def test_mss_gate_rejects_any_song_regressing_over_two_points():
    rows = []
    for index in range(41):
        native_wer, mss_wer = .20, .18
        if index == 3:
            mss_wer = .23
        rows.append({"song_id": str(index), "families": {
            "native": {"word_edits": 20, "wer": native_wer, "correct_reference_line_indices": [0]},
            "mss_rms_vad": {"word_edits": 18, "wer": mss_wer, "correct_reference_line_indices": [0]},
        }})
    result = _comparison(rows, 41)
    assert result["gate"]["status"] == "NO_GO"
    assert result["songs_regressing_more_than_2pct_absolute"][0]["song_id"] == "3"
