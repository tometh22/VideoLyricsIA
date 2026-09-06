import copy
import json

import pytest

from reviewer_acoustic_cache import cached_receipts, request_index


def fixture():
    song = {"job_id": "one", "audio_sha256": "a" * 64, "audio_revision": 1,
            "segments_revision": 3, "segments_sha256": "b" * 64, "duration_seconds": 40}
    source = {k: v for k, v in song.items() if k != "duration_seconds"}
    source["segments_revision"] = 0
    request = {"source": source, "clip_sha256": "c" * 64, "view": "mix",
               "provider": "google", "model": "gemini-2.5-flash",
               "family": "google/gemini-2.5-flash-audio", "prompt_version": "blind-vocal-events-shadow-v1",
               "conditioning_texts": [], "window": {"start": 12, "end": 36, "offset_seconds": 12},
               "received_audio": True, "tool_status": "ok", "response": {"events": [
                   {"text": "hola", "start": 1, "end": 3}]}}
    return song, {"request": request, "cache_path": "private/request.json", "evidence_sha256": "d" * 64}


def test_rebind_only_audio_and_preserve_origin():
    song, entry = fixture()
    result = cached_receipts(song, index=[entry])
    assert result["receipts"][0]["source_rebound"]
    assert result["receipts"][0]["original_source"]["segments_revision"] == 0
    assert result["receipts"][0]["source"]["segments_revision"] == 3
    assert result["records"][0]["annotations"][0]["global_end"] == 15
    assert entry["request"]["source"]["segments_revision"] == 0


@pytest.mark.parametrize("field,value,reason", [
    ("view", "stem", "stem_clock_not_transferred"),
    ("conditioning_texts", ["secret lyric"], "text_conditioning_unverified"),
    ("conditioning_texts", None, "text_conditioning_unverified"),
    ("prompt_version", "other", "unsupported_model_or_blind_prompt"),
    ("tool_status", "tool_error", "tool_error"),
    ("response", {}, "unusable_audio_response"),
])
def test_exclusions_are_not_coverage(field, value, reason):
    song, entry = fixture()
    entry["request"][field] = value
    result = cached_receipts(song, index=[entry])
    assert not result["receipts"]
    assert result["excluded"][0]["reason"] == reason


def test_changed_audio_revision_rejected():
    song, entry = fixture()
    song["audio_revision"] = 2
    assert cached_receipts(song, index=[entry])["excluded"][0]["reason"] == "audio_identity_mismatch"


def test_out_of_clip_event_preserved_not_used():
    song, entry = fixture()
    entry["request"]["response"]["events"][0]["end"] = 29
    result = cached_receipts(song, index=[entry])
    assert len(result["receipts"]) == 1
    assert result["records"][0]["annotations"] == []
    assert result["summary"]["invalid_annotations"] == 1


def test_deduplicate_cached_copies():
    song, entry = fixture()
    assert len(cached_receipts(song, index=[entry, copy.deepcopy(entry)])["receipts"]) == 1


def test_last_bound_metadata_rounding_only():
    song, entry = fixture()
    song["duration_seconds"] = 35.999999
    receipt = cached_receipts(song, index=[entry])["receipts"][0]
    assert receipt["end"] == song["duration_seconds"]
    assert receipt["original_window"]["end"] == 36
    assert receipt["clock_adjustment"]["kind"] == "metadata_duration_rounding_only"
    song["duration_seconds"] = 35.998
    assert cached_receipts(song, index=[entry])["excluded"][0]["reason"] == "invalid_mix_window"


def test_index_retains_unknown_completion_without_reading_large_artifacts(tmp_path):
    _, entry = fixture()
    requests = tmp_path / "experiment" / "requests"
    requests.mkdir(parents=True)
    (requests / "attempt.attempt.json").write_text(json.dumps({"identity": entry["request"]}))
    index = request_index(tmp_path)
    assert index[0]["request"]["tool_status"] == "unknown_completion"
    (requests / "attempt.json").write_text(json.dumps(entry["request"]))
    assert len(request_index(tmp_path)) == 1


def test_whisper_words_and_empty_google_events_valid():
    song, entry = fixture()
    entry["request"]["response"] = {"events": []}
    assert len(cached_receipts(song, index=[entry])["receipts"]) == 1
    entry["request"].update(provider="openai", model="whisper-1", family="openai/whisper-1",
                            prompt_version="no-prompt-v1", response={"words": [{"word": "hola", "start": 0, "end": 1}]})
    assert cached_receipts(song, index=[entry])["records"][0]["annotations"][0]["text"] == "hola"
