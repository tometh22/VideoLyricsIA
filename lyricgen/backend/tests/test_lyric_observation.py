from copy import deepcopy

from delivery_preflight import build_delivery_preflight
from lyric_observation import build_lyric_observation
from transcription_quality import evaluate


def test_shared_word_clocks_with_non_overlapping_display_windows():
    clock = {"word": "Dijo,", "start": 1.3, "end": 1.7}
    segments = [
        {"text": "Eso dijo", "start": 1, "end": 2, "words": [clock]},
        {"text": "Dijo una vez", "start": 2, "end": 3, "words": [clock]},
        {"text": "Dijo una vez", "start": 3, "end": 4,
         "words": [{**clock, "start": 3.1, "end": 3.5}]},
    ]
    before = deepcopy(segments)
    assert build_lyric_observation(segments)["findings"] == [{
        "code": "reused_word_clock", "segment_indices": [0, 1], "start": 1.3,
        "end": 1.7, "shared_word_count": 1,
    }]
    assert segments == before


def test_real_repetitions_keep_their_clock_and_full_outro_phrase_is_review_only():
    report = build_lyric_observation([
        {"text": "Eh, eh", "start": 1, "end": 2},
        {"text": "EH EH", "start": 2, "end": 3},
        {"text": "¡Eh, eh!", "start": 1, "end": 2},
        {"text": "Gracias por ver el video de mi vida", "start": 5, "end": 6},
        {"t": "¡Gracias por ver el vídeo!", "s": 6, "e": 7},
    ])
    assert report["mode"] == "observe"
    assert report["findings"] == [
        {"code": "duplicate_lyric_event", "segment_indices": [0, 2], "start": 1, "end": 2},
        {"code": "possible_outro_caption", "segment_indices": [4], "start": 6, "end": 7},
    ]


def test_word_clock_fallback_ignores_invalid_and_nonadjacent_evidence():
    clock = {"text": "sol", "start": 3.1, "end": 3.2}
    invalid = {"text": "sol", "start": 3.1, "end": float("inf")}
    before_gap = {"text": "sol", "start": 2, "end": 3, "words": [clock]}
    first = {"text": "sol", "start": 3, "end": 4, "words": [invalid, clock]}
    second = {"text": "sol", "start": 4, "end": 5,
              "provider_evidence": {"words": [None, invalid, clock]}}
    assert build_lyric_observation([before_gap, {}, first, second])["findings"] == [{
        "code": "reused_word_clock", "segment_indices": [2, 3], "start": 3.1,
        "end": 3.2, "shared_word_count": 1,
    }]


def test_shared_provider_payload_is_ignored_for_words_outside_the_display_text():
    evidence = {"words": [{"word": "sol", "start": 1.3, "end": 1.7}]}
    segments = [
        {"text": "Sale el sol", "start": 1, "end": 2, "provider_evidence": evidence},
        {"text": "También la luna", "start": 2, "end": 3, "provider_evidence": evidence},
    ]
    assert build_lyric_observation(segments)["findings"] == []


def test_observations_are_shared_by_quality_and_final_qc_without_changing_gates():
    segments = [{"text": "Gracias por ver el video", "start": 1, "end": 4}]
    ordinary = [{**segments[0], "text": "Gracias por volver a verme"}]
    actual = evaluate(segments, None)
    control = evaluate(ordinary, None)
    for key in ("decision", "reasons", "render_blocked", "unsafe_windows"):
        assert actual[key] == control[key]

    report = build_delivery_preflight(metadata={}, segments=segments)
    assert report["decision"] == "PASS"
    assert report["issues"] == []
    assert actual["lyric_observation"]["findings"]
    assert report["lyric_observation"] == actual["lyric_observation"]
    assert build_delivery_preflight(metadata={}, segments=ordinary, quality=actual)[
        "lyric_observation"
    ]["findings"] == []
