from copy import deepcopy

from reviewer_text_patch import propose_patches


def evidence():
    return [{"family": "openai/whisper-1", "tool_status": "ok",
             "received_audio": True, "conditioning_texts": [],
             "response": {"text": "como es tripartito sobra el apetito"}},
            {"family": "google/gemini-audio", "tool_status": "ok",
             "received_audio": True, "conditioning_texts": [],
             "response": {"events": [{"text": "cómo es tripartito"},
                                      {"text": "sobra el apetito"}],
                          "editorial_ambiguity": False}}]


def test_only_audio_supported_changed_span_not_whole_line_is_certified():
    rows = evidence()
    before = deepcopy(rows)
    result = propose_patches("¿Cómo estripartito sobra el apetito?", rows)
    assert result[0]["text"] == "¿Cómo es tripartito sobra el apetito?"
    assert result[0]["unchanged_text_verified"] is False
    assert rows == before


def test_same_family_is_not_two_votes():
    rows = evidence()
    rows[1]["family"] = "openai/whisper-1"
    assert not propose_patches("¿Cómo estripartito sobra el apetito?", rows)


def test_conditioned_text_cannot_support_a_patch():
    rows = evidence()
    rows[1]["conditioning_texts"] = ["external lyric"]
    assert not propose_patches("¿Cómo estripartito sobra el apetito?", rows)


def test_ambiguous_or_forced_alignment_cannot_vote():
    for change in [{"view": "alignment_audio"}, {"received_audio": False}]:
        rows = evidence()
        rows[1].update(change)
        assert not propose_patches("¿Cómo estripartito sobra el apetito?", rows)


def test_unique_context_anchor_is_required():
    rows = evidence()
    for r in rows:
        r["response"] = {"text": "es tripartito"}
    assert not propose_patches("estripartito", rows)
