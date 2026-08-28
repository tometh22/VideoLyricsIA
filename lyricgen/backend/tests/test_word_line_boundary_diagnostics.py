from word_line_boundary_diagnostics import analyze_word_line_boundaries


def test_detects_upstream_shared_word_and_line_boundary_without_mutation():
    segments = [
        {
            "start": 1.0, "end": 3.0, "text": "uno",
            "words": [{"word": "uno", "start": 1.0, "end": 3.0}],
        },
        {
            "start": 3.0, "end": 5.0, "text": "dos",
            "words": [{"word": "dos", "start": 3.0, "end": 4.8}],
        },
    ]
    report = analyze_word_line_boundaries(segments)
    assert report["counts"]["coupled_word_line_boundary"] == 1
    assert report["rows"][0]["diagnosis"] == "upstream_shared_word_line_boundary"
    assert report["rows"][0]["automatic_timing_change_allowed"] is False
    assert segments[0]["end"] == 3.0


def test_distinguishes_fixed_padding_from_next_line_clamp():
    report = analyze_word_line_boundaries([
        {
            "start": 1.0, "end": 2.25, "text": "uno",
            "words": [{"word": "uno", "start": 1.0, "end": 2.0}],
        },
        {"start": 4.0, "end": 5.0, "text": "dos"},
    ])
    assert report["counts"]["fixed_250ms_padding"] == 1
    assert report["counts"]["coupled_word_line_boundary"] == 0
    assert report["rows"][0]["diagnosis"] == "fixed_wrapper_padding"
