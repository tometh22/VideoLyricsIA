import structural_hybrid as sh
import targeted_consensus as tc


def _events(starts, text="Real uoh uoh"):
    return [
        {"start": start, "end": start + 1.5, "text": text, "kind": "sung"}
        for start in starts
    ]


def _ctc(starts, min_score=0.7):
    return {
        "events": [
            {
                "start": start,
                "end": start + 1.5,
                "text": "Real uoh uoh",
                "ctc_score": min_score,
                "words": [],
            }
            for start in starts
        ],
        "median_score": min_score,
        "min_score": min_score,
    }


def test_gemini_event_tokens_collapse_into_complete_cycles():
    raw = []
    for start in (60.8, 67.0, 73.2, 79.3):
        raw.extend([
            {"start": start, "end": start + 0.7, "text": "Real", "kind": "sung"},
            {"start": start + 1.0, "end": start + 1.5, "text": "uoh", "kind": "vocalization"},
            {"start": start + 1.7, "end": start + 2.2, "text": "uoh", "kind": "vocalization"},
        ])
    cycles = tc._gemini_cycles(raw, "real")
    assert len(cycles) == 4
    assert [cycle["text"] for cycle in cycles] == ["Real uoh uoh"] * 4
    assert [cycle["start"] for cycle in cycles] == [60.8, 67.0, 73.2, 79.3]


def test_gemini_cycles_do_not_swallow_trailing_non_cycle_vocals():
    raw = []
    for start in (60.8, 67.0, 73.2, 79.3):
        raw.extend([
            {"start": start, "end": start + 0.7, "text": "Real", "kind": "sung"},
            {"start": start + 1.0, "end": start + 1.5, "text": "uoh", "kind": "vocalization"},
            {"start": start + 1.7, "end": start + 2.2, "text": "uoh", "kind": "vocalization"},
        ])
    raw.extend([
        {"start": 84.0, "end": 84.3, "text": "no", "kind": "sung"},
        {"start": 85.0, "end": 86.5, "text": "nooooo", "kind": "vocalization"},
    ])
    cycles = tc._gemini_cycles(raw, "real")
    assert len(cycles) == 4
    assert cycles[-1]["text"] == "Real uoh uoh"


def test_hybrid_accepts_only_when_stem_mix_ctc_and_topology_agree(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    calls = []

    def ctc(path, _texts, *_args):
        calls.append(path)
        starts = [60.8, 67.0, 73.2, 79.3]
        if path == "mix.wav":
            starts = [value + 0.12 for value in starts]
        return _ctc(starts)

    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3, 4]),
        window_start=54.0, window_end=87.0,
        ctc_fn=ctc,
        topology_fn=lambda *_args: {"accepted": True, "stem_dtw": 0.3},
    )
    assert calls == ["stem.wav", "mix.wav"]
    assert verdict["accepted"] is True
    assert verdict["max_phase_delta"] == 0.12
    assert [event["start"] for event in verdict["events"]] == [60.8, 67.0, 73.2, 79.3]
    assert all(event["consensus_sources"] == [
        "gemini_audio_cardinality",
        "ctc_vocal_stem",
        "ctc_original_mix",
        "acoustic_topology_stem_mix",
    ] for event in verdict["events"])


def test_hybrid_abstains_when_ctc_selects_different_phase(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")

    def ctc(path, _texts, *_args):
        starts = [60.8, 67.0, 73.2, 79.3]
        if path == "mix.wav":
            starts = [value + 1.1 for value in starts]
        return _ctc(starts)

    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3, 4]),
        window_start=54.0, window_end=87.0,
        ctc_fn=ctc,
        topology_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("topology must not run after phase disagreement")
        ),
    )
    assert verdict["accepted"] is False
    assert verdict["reason"] == "ctc_phase_or_score_disagreement"


def test_hybrid_abstains_when_topology_disagrees(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3, 4]),
        window_start=54.0, window_end=87.0,
        ctc_fn=lambda *_args: _ctc([60.8, 67.0, 73.2, 79.3]),
        topology_fn=lambda *_args: {"accepted": False, "reason": "ambiguous"},
    )
    assert verdict["accepted"] is False
    assert verdict["reason"] == "topology_disagreement"


def test_topology_first_path_uses_ctc_to_choose_a_distinct_phase(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    hypotheses = [
        {"anchors": [10.0, 16.0, 22.0], "topology_score": 0.30},
        {"anchors": [12.0, 18.0, 24.0], "topology_score": 0.32},
    ]

    def anchored(path, texts, anchors, *_args):
        strong = anchors[0] == 10.0
        offset = 0.08 if path == "mix.wav" else 0.0
        score = 0.70 if strong else 0.20
        return {
            "events": [
                {"start": anchor + offset, "end": anchor + 1.5,
                 "text": text, "words": [], "ctc_score": score}
                for anchor, text in zip(anchors, texts)
            ],
            "mean_score": score,
            "median_score": score,
            "min_score": score,
        }

    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3]),
        window_start=8.0, window_end=28.0,
        hypotheses_fn=lambda *_args: hypotheses,
        anchor_ctc_fn=anchored,
        topology_fn=lambda *_args: {"accepted": True},
    )
    assert verdict["accepted"] is True
    assert [event["start"] for event in verdict["events"]] == [10.0, 16.0, 22.0]
    assert all(event["review"] is False for event in verdict["events"])
    assert verdict["phase_margin"] > 0.3


def test_topology_first_path_abstains_when_phases_are_tied(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    hypotheses = [
        {"anchors": [10.0, 16.0, 22.0], "topology_score": 0.30},
        {"anchors": [12.0, 18.0, 24.0], "topology_score": 0.31},
    ]

    def anchored(_path, texts, anchors, *_args):
        return {
            "events": [
                {"start": anchor, "end": anchor + 1.5,
                 "text": text, "words": [], "ctc_score": 0.6}
                for anchor, text in zip(anchors, texts)
            ],
            "mean_score": 0.6, "median_score": 0.6, "min_score": 0.6,
        }

    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3]),
        window_start=8.0, window_end=28.0,
        hypotheses_fn=lambda *_args: hypotheses,
        anchor_ctc_fn=anchored,
        topology_fn=lambda *_args: {"accepted": True},
    )
    assert verdict["accepted"] is False
    assert verdict["reason"] == "anchored_ctc_ambiguous"


def test_invalid_high_scoring_phase_cannot_collapse_valid_margin(monkeypatch):
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    hypotheses = [
        {"anchors": [60.8, 67.0, 73.2, 79.3], "topology_score": 0.36},
        {"anchors": [63.2, 69.5, 75.8, 81.7], "topology_score": 0.34},
    ]

    def anchored(path, texts, anchors, *_args):
        valid = anchors[0] == 60.8
        if valid:
            starts = [value + (0.10 if path == "mix.wav" else 0.0) for value in anchors]
            lexical_score = 0.24
            line_score = 0.07
        else:
            # Strong character probabilities do not rescue CTC events that
            # abandoned their acoustic anchors.  These starts deliberately
            # duplicate the valid phase to exercise viability-first dedupe.
            starts = [60.8, 67.0, 73.2, 79.3]
            lexical_score = 0.80
            line_score = 0.80
        events = []
        for start, text in zip(starts, texts):
            events.append({
                "start": start,
                "end": start + 2.0,
                "text": text,
                "ctc_score": line_score,
                "words": [
                    {"word": "Real", "score": lexical_score},
                    {"word": "Oh", "score": 0.001},
                    {"word": "Oh", "score": 0.001},
                ],
            })
        return {
            "events": events,
            "mean_score": line_score,
            "median_score": line_score,
            "min_score": line_score,
        }

    verdict = sh.verify(
        "stem.wav", "mix.wav", _events([1, 2, 3, 4], text="Real Oh Oh"),
        window_start=54.0, window_end=87.0,
        hypotheses_fn=lambda *_args: hypotheses,
        anchor_ctc_fn=anchored,
        topology_fn=lambda *_args: {"accepted": True},
    )
    assert verdict["accepted"] is True
    assert verdict["viable_hypotheses"] == 1
    assert verdict["phase_margin"] == 1.0
    assert [event["start"] for event in verdict["events"]] == [60.8, 67.0, 73.2, 79.3]


def test_vocalization_tail_uses_stem_vad_not_low_score_word_spans():
    events = [
        {
            "start": 60.93,
            "end": 63.31,
            "text": "Real Oh Oh",
            "words": [{"word": "Real", "start": 60.93, "end": 61.95}],
        },
        {"start": 67.09, "end": 69.55, "text": "Real Oh Oh"},
    ]
    out = sh._extend_vocalization_tails(
        events,
        ["Real Oh Oh", "Real Oh Oh"],
        [60.93, 67.09],
        [(60.8, 62.65), (63.15, 65.0)],
        66.5,
    )
    assert out[0]["start"] == 60.93
    assert out[0]["end"] == 65.08
    assert out[0]["structural_tail_source"] == "vocal_stem_vad"
    assert "words" not in out[0]


def test_catalogue_only_normalizes_equivalent_vocalization_spelling():
    cycles = _events([60.8, 67.0], text="Real Oh Oh")
    targets = [
        (0, {"live_structural_suggestion": "Real... uoo uou"}),
        (1, {"live_structural_suggestion": "Real... uoo uou"}),
    ]
    out = tc._canonicalize_cycle_vocalizations(cycles, targets, "real")
    assert [event["text"] for event in out] == ["Real uoo uou"] * 2
    assert all(event["vocalization_spelling_source"] == "catalogue_consensus" for event in out)


def test_catalogue_cannot_add_lexical_content_to_gemini_cycles():
    cycles = _events([60.8, 67.0], text="Real Oh Oh")
    targets = [(0, {"live_structural_suggestion": "Real para siempre"})]
    assert tc._canonicalize_cycle_vocalizations(cycles, targets, "real") == cycles


def test_verified_hybrid_can_replace_wrong_cardinality_atomically(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    monkeypatch.setenv("TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED", "1")
    starts = [60.8, 67.0, 73.2, 79.3]
    target_rows = [
        {
            "start": 60.8 + index * 1.2,
            "end": 61.7 + index * 1.2,
            "text": "Real",
            "live_structural_suggestion": "Real uoh uoh",
        }
        for index in range(6)
    ]
    verified = _events(starts)
    for event in verified:
        event["structural_hybrid"] = True

    out, stats = tc.reprocess(
        {"segments": target_rows, "_asr_words": []},
        "mix.wav",
        [{
            "start": 54.0, "end": 87.0,
            "reasons": ["live_structural_disagreement"],
        }],
        transcribe_fn=lambda *_args: [],
        gemini_fn=lambda *_args: _events(starts),
        hybrid_fn=lambda *_args, **_kwargs: {
            "accepted": True, "reason": "verified", "events": verified,
        },
        stem_path="stem.wav",
    )
    assert stats["structural_hybrid_attempts"] == 1
    assert stats["structural_hybrid_accepts"] == 1
    assert stats["structural_repairs"] == 1
    assert stats["lines_replaced"] == 6
    assert len(out["segments"]) == 4
    assert [segment["start"] for segment in out["segments"]] == starts


def test_declined_hybrid_never_deletes_existing_rows(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TARGETED_ACOUSTIC_CTC_ENABLED", "1")
    monkeypatch.setenv("TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED", "1")
    original = [{
        "start": 60.0, "end": 62.0, "text": "Real",
        "live_structural_suggestion": "Real uoh uoh",
    }]
    out, stats = tc.reprocess(
        {"segments": original, "_asr_words": []}, "mix.wav",
        [{"start": 54.0, "end": 80.0,
          "reasons": ["live_structural_disagreement"]}],
        transcribe_fn=lambda *_args: [],
        gemini_fn=lambda *_args: _events([60.0, 67.0]),
        hybrid_fn=lambda *_args, **_kwargs: {
            "accepted": False, "reason": "ambiguous", "events": [],
        },
        stem_path="stem.wav",
    )
    assert out["segments"] == original
    assert stats["structural_repairs"] == 0
    assert stats["lines_replaced"] == 0
    assert stats["structural_hybrid_declined"] == ["hybrid_ambiguous"]
