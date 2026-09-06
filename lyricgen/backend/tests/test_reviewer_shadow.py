from copy import deepcopy
import math

import pytest

from reviewer_shadow import (assert_current, freeze_sample, local_to_global, plan_windows,
                             review_window, select_content, select_endpoint,
                             sequence_discrepancies, tokens)
from reviewer_shadow_audio import BlindAudioTools, localized_witnesses, private_write
from shadow_reference_import import digest


def song(i=0, artist=None):
    segments = [{"text": "Canto así", "start": 2., "end": 4.},
                {"text": "Otra vez", "start": 6., "end": 8.}]
    return {"job_id": str(i), "artist": artist or f"artist-{i}", "title": f"title-{i}",
            "audio_sha256": f"{i:064x}", "audio_revision": 1, "segments_revision": 1,
            "segments": segments, "segments_sha256": digest(segments), "duration_seconds": 12.}


def witness(family, text="Canto aquí", **updates):
    return {"family": family, "text": text, "tool_status": "ok", "received_audio": True,
            "conditioning_texts": [], "occurrence_verified": True, **updates}


def test_normalization_preserves_accents_and_repetitions():
    assert tokens("Él, él… ¡si!") == ["él", "él", "si"]
    assert sequence_discrepancies([{"text": "el"}], "él")[0]["operation"] == "replace"
    assert sequence_discrepancies([{"text": "otra vez"}], "otra vez otra vez")[0]["operation"] == "insert"


def test_same_family_prompts_do_not_manufacture_independence():
    result = select_content("Canto así", ["Canto aquí"], [witness("gemini-2.5"), witness("gemini-3")])
    assert result["decision"] != "propose"


@pytest.mark.parametrize("bad", [{"received_audio": False}, {"conditioning_texts": ["candidate"]},
                                  {"occurrence_verified": False}, {"editorial_ambiguity": True},
                                  {"tool_status": "tool_error"}])
def test_unsupported_witness_cannot_vote(bad):
    evidence = [witness("gemini"), witness("whisper-1", **bad)]
    assert select_content("Canto así", ["Canto aquí"], evidence)["decision"] != "propose"


def test_independent_audio_support_allows_shadow_proposal_not_autoapply():
    current = song()
    window = plan_windows(current)[0]
    evidence = [{**witness("gemini"), "kind": "content"}, {**witness("whisper-1"), "kind": "content"}]
    before = deepcopy(current)
    result = review_window(current, window, evidence=evidence, commit="a" * 40)
    assert result["content"]["decision"] == "propose"
    assert result["automatic_apply_allowed"] is False
    assert current == before
    assert_current(result, current)
    changed = deepcopy(current)
    changed["audio_revision"] += 1
    with pytest.raises(ValueError, match="stale"):
        assert_current(result, changed)


def test_energy_alone_or_model_clock_cannot_be_selected_as_timing():
    current = song()["segments"][0]
    candidate = {"tool_status": "ok", "clock_source": "acoustic_tool", "end_seconds": 4.5}
    assert select_endpoint(current, [candidate], next_start=6., duration=12.)["decision"] == "abstain"
    candidate.update(target_voice_verified=True, phonetic_end_supported=True, mix_stem_sync_verified=True)
    assert select_endpoint(current, [candidate], next_start=6., duration=12.)["decision"] == "propose"
    candidate["clock_source"] = "conversation_model"
    assert select_endpoint(current, [candidate], next_start=6., duration=12.)["decision"] == "abstain"


def test_render_clipping_cannot_hide_early_endpoint_and_locked_is_untouched():
    current = song()["segments"][0]
    candidate = {"tool_status": "ok", "clock_source": "acoustic_tool", "end_seconds": 6.5,
                 "target_voice_verified": True, "phonetic_end_supported": True,
                 "mix_stem_sync_verified": True}
    assert select_endpoint(current, [candidate], next_start=6., duration=12.)["reason"] == "render_overlap_policy_conflict"
    current["operator_locked"] = True
    assert select_endpoint(current, [candidate], next_start=6., duration=12.)["reason"] == "human_locked"


def test_clock_conversion_is_explicit_bounded_and_finite():
    assert local_to_global(1., 2., offset=20., clip_duration=3., song_duration=25.) == (21., 22.)
    for end in [math.nan, 4., -1.]:
        with pytest.raises(ValueError):
            local_to_global(1., end, offset=20., clip_duration=3., song_duration=25.)


def test_sample_freezes_related_artist_and_recording_groups_together():
    jobs = [song(i) for i in range(30)]
    jobs[1]["artist"] = jobs[0]["artist"]
    jobs[2]["audio_sha256"] = jobs[1]["audio_sha256"]
    sample = freeze_sample(jobs, count=30, base_commit="a" * 40)
    rows = {r["job_id"]: r for r in sample["songs"]}
    assert len({rows[str(i)]["split"] for i in range(3)}) == 1
    assert sample["used_traffic_light"] is False
    assert sample == freeze_sample(jobs, count=30, base_commit="a" * 40)


def test_blind_adapter_caches_and_preserves_failed_calls(tmp_path, monkeypatch):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"test audio fixture")
    tools = BlindAudioTools(tmp_path / "cache")
    def fail(_):
        raise RuntimeError("provider failed, secret must not be logged")
    monkeypatch.setattr(tools, "_whisper", fail)
    first = tools.listen(clip, provider="openai", view="mix", source={"id": 1}, window={})
    second = tools.listen(clip, provider="openai", view="mix", source={"id": 1}, window={})
    assert first["tool_status"] == "tool_error"
    assert first["received_audio"] is False
    assert "secret" not in str(first)
    assert second["cache_hit"] is True
    assert tools.calls == 1


def test_external_text_alone_never_creates_a_proposal():
    current = song()
    result = review_window(current, plan_windows(current)[0], evidence=[],
                           external_reference="texto completamente distinto", commit="a" * 40)
    assert result["content"]["decision"] == "abstain"
    assert result["timing"]["decision"] == "abstain"


def test_invalid_provider_response_preserves_audio_receipt_usage_and_raw(tmp_path, monkeypatch):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"fixture")
    tools = BlindAudioTools(tmp_path / "cache")
    monkeypatch.setattr(tools, "_gemini", lambda _: {"tool_status": "invalid_response",
                        "raw_response_text": "{broken", "usage": {"prompt_token_count": 123}})
    result = tools.listen(clip, provider="google", view="stem", source={}, window={})
    assert result["tool_status"] == "invalid_response"
    assert result["received_audio"] is True
    assert result["usage"]["prompt_token_count"] == 123
    assert result["raw_response_text"] == "{broken"


def test_repeated_phrase_cannot_claim_unique_occurrence():
    results = [{"provider": "openai", "tool_status": "ok", "response": {"words": [
        {"word": "hola", "start": 1., "end": 2.}, {"word": "hola", "start": 3., "end": 4.}]}},
        {"provider": "google", "tool_status": "ok", "response": {"events": [{"text": "hola", "kind": "sung"}]}}]
    assert localized_witnesses(results, {"start": 1., "end": 2.}, {"offset_seconds": 0., "start": 0., "end": 5.}, 10.) == []
