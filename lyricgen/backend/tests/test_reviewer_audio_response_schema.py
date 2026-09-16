from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

import reviewer_shadow_audio as audio
from reviewer_shadow import ShadowPolicy
from shadow_reference_import import digest


def payload():
    return {"events": [{"text": "No, no", "start": 1., "end": 3., "kind": "sung"}],
        "editorial_ambiguity": False, "ambiguity_reason": "none", "reverb": "absent"}


def invoke(monkeypatch, tmp_path, text, *, finish="STOP"):
    from google import genai
    from google.oauth2 import service_account
    seen = []
    response = SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)],
        usage_metadata=None, model_version="gemini-2.5-flash", response_id="mock-request")
    class FakeClient:
        def __init__(self, **kwargs):
            self.models = self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def generate_content(self, **kwargs):
            seen.append(kwargs)
            return response
    credentials = SimpleNamespace(project_id="test", with_quota_project=lambda project: "fake")
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", lambda *a, **k: credentials)
    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/not/read/in/mock.json")
    clip = tmp_path / "blind.wav"
    clip.write_bytes(b"mock audio bytes")
    return audio.BlindAudioTools._gemini(clip), seen


def test_google_request_uses_supported_bounded_schema_same_model_and_token_budget(monkeypatch, tmp_path):
    result, seen = invoke(monkeypatch, tmp_path, json.dumps(payload()))
    assert result["response"] == payload()
    request = seen[0]
    assert request["model"] == "gemini-2.5-flash"
    assert request["config"].max_output_tokens == 4096
    schema = request["config"].response_schema
    assert schema["properties"]["events"]["maxItems"] == 16
    assert "maxLength" not in json.dumps(schema)
    assert result["character_limit_provider_enforced"] is False
    assert request["config"].system_instruction == audio.BLIND_PROMPT
    assert len(request["contents"]) == 1
    assert request["contents"][0].inline_data.data == b"mock audio bytes"
    assert audio.PROMPT_VERSION != audio.LEGACY_PROMPT_VERSION


@pytest.mark.parametrize("text,finish,error", [
    ('{"events":[{"text":"no,no,no', "MAX_TOKENS", "incomplete_or_blocked_generation"),
    (json.dumps(payload()), "MAX_TOKENS", "incomplete_or_blocked_generation"),
    ('{"events":[', "STOP", "invalid_json_response"),
    (json.dumps(payload()), "SAFETY", "incomplete_or_blocked_generation"),
])
def test_truncation_never_repaired_or_accepted(monkeypatch, tmp_path, text, finish, error):
    result, _ = invoke(monkeypatch, tmp_path, text, finish=finish)
    assert result["tool_status"] == "invalid_response"
    assert result["error_type"] == error
    assert result["response"] == {}
    assert result["raw_response_text"] == text


@pytest.mark.parametrize("mutate", [
    lambda p: p["events"][0].update(text="no," * 81),
    lambda p: p.update(events=p["events"] * 17),
    lambda p: p.update(ambiguity_reason="x" * 1000),
    lambda p: p.update(editorial_ambiguity="false"),
    lambda p: p["events"][0].update(end=float("nan")),
    lambda p: p["events"][0].update(start=True),
    lambda p: p["events"][0].update(end=25.),
    lambda p: p["events"][0].update(end=0.),
    lambda p: p["events"][0].update(kind="invented"),
])
def test_local_validation_fails_closed_without_truncating(mutate):
    value = payload()
    mutate(value)
    before = deepcopy(value)
    assert audio.valid_blind_response(value) is False
    assert value == before


def cache_setup(tmp_path, status="ok"):
    clip = tmp_path / "blind.wav"
    clip.write_bytes(b"mock audio bytes")
    cache = tmp_path / "cache"
    cache.mkdir()
    source, window = {"job_id": "test"}, {"start": 0., "end": 24.}
    identity = {"clip_sha256": audio.file_sha(clip), "provider": "google", "model": "gemini-2.5-flash",
        "prompt_version": audio.LEGACY_PROMPT_VERSION, "source": source, "window": window,
        "view": "mix", "policy": asdict(ShadowPolicy())}
    response = {**identity, "response": payload(), "tool_status": status,
        "received_audio": True, "conditioning_texts": [], "calls": 1}
    return clip, cache, source, window, identity, response


def test_successful_v1_cache_not_repurchased_or_relabelled(monkeypatch, tmp_path):
    clip, cache, source, window, identity, response = cache_setup(tmp_path)
    audio.private_write(cache / f"{digest(identity)}.json", response)
    monkeypatch.setattr(audio.BlindAudioTools, "_gemini", staticmethod(lambda clip: pytest.fail("paid call")))
    listener = audio.BlindAudioTools(cache)
    result = listener.listen(clip, provider="google", view="mix", source=source, window=window)
    assert listener.calls == 0
    assert result["calls_this_run"] == 0
    assert result["prompt_version"] == audio.LEGACY_PROMPT_VERSION
    assert result["cache_compatibility"] == "valid_legacy_blind_v1"


def test_failed_v1_request_can_be_explicitly_retried_under_new_identity(monkeypatch, tmp_path):
    clip, cache, source, window, identity, response = cache_setup(tmp_path, "invalid_response")
    audio.private_write(cache / f"{digest(identity)}.json", response)
    monkeypatch.setattr(audio.BlindAudioTools, "_gemini", staticmethod(lambda clip: {"response": payload()}))
    listener = audio.BlindAudioTools(cache)
    result = listener.listen(clip, provider="google", view="mix", source=source, window=window)
    assert listener.calls == 1 and result["prompt_version"] == audio.PROMPT_VERSION
    assert json.loads((cache / f"{digest(identity)}.json").read_text())["tool_status"] == "invalid_response"


def test_unknown_old_attempt_never_repurchased(monkeypatch, tmp_path):
    clip, cache, source, window, identity, _ = cache_setup(tmp_path)
    audio.private_write(cache / f"{digest(identity)}.attempt.json", {"identity": identity})
    monkeypatch.setattr(audio.BlindAudioTools, "_gemini", staticmethod(lambda clip: pytest.fail("paid call")))
    result = audio.BlindAudioTools(cache).listen(clip, provider="google", view="mix", source=source, window=window)
    assert result["tool_status"] == "unknown_completion"
    assert result["calls_this_run"] == 0
