import phonetic_verifier as verifier
import ctc_align


TEST_REVISION = "1" * 40


def _mapping():
    return {
        "selected_candidate_id": "correct",
        "phonetic_candidates": [
            {
                "candidate_id": "correct",
                "texts": ["real uoh", "noooo"],
                "anchors": [60.8, 79.3],
                "source": "gemini", "family": "gemini",
            },
            {
                "candidate_id": "wrong",
                "texts": ["no", "real"],
                "anchors": [60.8, 79.3],
                "source": "whisper", "family": "whisper",
            },
        ],
    }


def _emissions(_path, *, view, **_kwargs):
    return {
        "view": view, "model_id": "trusted-test-model",
        "model_revision": TEST_REVISION,
        "dictionary": {"a": 1, "b": 2}, "blank_id": 0,
    }, False


def _calibration():
    from transcription_quality import runtime_identity
    runtime = runtime_identity()
    return {
        "schema": "quality-ctc-calibration-v1",
        "calibration_id": "gold-test",
        "policy_version": "lyrics-quality-v5",
        "model_identity": {
            "model_id": "trusted-test-model",
            "model_revision": TEST_REVISION,
            "vocab_sha256": verifier._vocab_sha256({"a": 1, "b": 2}),
            "blank_id": 0,
        },
        "thresholds": {
            "stem_min_score": .10, "stem_min_margin": .01,
            "mix_min_score": .10, "mix_min_margin": .01,
        },
        "release_gate_decision": "GO",
        "pipeline_release": runtime["pipeline_release"],
        "pipeline_config_fingerprint": runtime["pipeline_config_fingerprint"],
        "benchmark_manifest_sha256": "a" * 64,
        "release_report_sha256": "b" * 64,
    }


def test_two_correlated_views_must_choose_same_real_alternative(monkeypatch):
    monkeypatch.setattr(ctc_align, "MODEL_REVISION", TEST_REVISION)
    def score(bundle, _candidates):
        assert bundle["view"] in {"vocal_stem", "original_mix"}
        return [
            {"candidate_id": "correct", "mean_score": .72,
             "min_score": .61},
            {"candidate_id": "wrong", "mean_score": .31,
             "min_score": .24},
        ]

    result = verifier.verify_mapping(
        "stem.wav", "mix.wav", _mapping(),
        window_start=54, window_end=87,
        score_fn=score, emission_fn=_emissions, calibration=_calibration(),
    )
    assert result["accepted"] is True
    assert result["views_are_correlated"] is True
    assert result["stem"]["winner"]["candidate_id"] == "correct"
    assert len(result["evidence_sha256"]) == 64


def test_disagreement_between_stem_and_mix_fails_closed(monkeypatch):
    monkeypatch.setattr(ctc_align, "MODEL_REVISION", TEST_REVISION)
    def score(bundle, _candidates):
        winner, loser = (
            ("correct", "wrong") if bundle["view"] == "vocal_stem"
            else ("wrong", "correct")
        )
        return [
            {"candidate_id": winner, "mean_score": .72, "min_score": .61},
            {"candidate_id": loser, "mean_score": .31, "min_score": .24},
        ]

    result = verifier.verify_mapping(
        "stem.wav", "mix.wav", _mapping(),
        window_start=54, window_end=87,
        score_fn=score, emission_fn=_emissions, calibration=_calibration(),
    )
    assert result["accepted"] is False


def test_duplicate_text_is_not_a_contrastive_alternative():
    mapping = _mapping()
    mapping["phonetic_candidates"][1]["texts"] = ["REAL  UOH", "NOOOO"]
    called = False

    def emissions(*_args, **_kwargs):
        nonlocal called
        called = True
        return None, False

    result = verifier.verify_mapping(
        "stem.wav", "mix.wav", mapping,
        window_start=54, window_end=87, emission_fn=emissions,
    )
    assert result["accepted"] is False
    assert result["reason"] == "real_alternatives_unavailable"
    assert called is False


def test_moving_model_revision_and_malformed_calibration_fail_closed(monkeypatch):
    monkeypatch.setattr(ctc_align, "MODEL_REVISION", "main")
    moving = verifier.verify_mapping(
        "stem.wav", "mix.wav", _mapping(),
        window_start=54, window_end=87, emission_fn=_emissions,
    )
    assert moving["reason"] == "model_revision_unpinned"

    monkeypatch.setattr(ctc_align, "MODEL_REVISION", TEST_REVISION)
    malformed = _calibration()
    malformed["thresholds"]["stem_min_margin"] = float("-inf")
    result = verifier.verify_mapping(
        "stem.wav", "mix.wav", _mapping(),
        window_start=54, window_end=87,
        emission_fn=_emissions, calibration=malformed,
    )
    assert result["accepted"] is False
    assert result["reason"] == "uncalibrated"
