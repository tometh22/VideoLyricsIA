import numpy as np

from eval.vocalization_resolver import canonical_vocalization, melisma_end, propose_vocalization


def test_stretched_vowels_are_canonicalized():
    assert canonical_vocalization("ooooooh ahhh") == ["oh", "ah"]
    assert canonical_vocalization("no puede") is None


def test_lexical_window_can_never_become_vocalization():
    window = {
        "start_s": 0, "end_s": 2, "content_type": "lexical_lyric", "content_confidence": 1,
        "candidates": [
            {"family_group": "whisper", "text": "oh"},
            {"family_group": "gemini", "text": "oh"},
        ],
    }
    result = propose_vocalization(window, np.zeros(32000, dtype=np.float32), 16000)
    assert result == {"status": "ABSTAIN", "reason": "content_gate_not_vocalization"}


def test_two_independent_nonlexical_sources_create_parenthesized_suggestion(monkeypatch):
    monkeypatch.setattr("eval.vocalization_resolver.pitch_articulations", lambda *_args: 3)
    window = {
        "start_s": 1, "end_s": 3, "content_type": "vocalization", "content_confidence": .95,
        "candidates": [
            {"family_group": "whisper", "text": "oooooh"},
            {"family_group": "gemini", "text": "oh"},
        ],
    }
    result = propose_vocalization(window, np.zeros(32000, dtype=np.float32), 16000)
    assert result["status"] == "PROPOSE"
    assert result["text"] == "(oh oh oh)"
    assert result["source"] == "independent_candidate_consensus"
    assert not result["auto_apply"]


def test_melisma_extension_stops_before_next_line(monkeypatch):
    pitch = np.full(200, 220.0)
    voiced = np.ones(200, dtype=bool)
    probability = np.ones(200)
    monkeypatch.setattr("eval.vocalization_resolver.librosa.pyin", lambda *_args, **_kwargs: (pitch, voiced, probability))
    result = melisma_end(np.zeros(64000), 16000, current_end_s=1.0, next_start_s=1.5)
    assert result["status"] == "PROPOSE"
    assert result["proposed_end_s"] <= 1.5
