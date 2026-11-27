"""Rejected/transient WhisperX windows are retried, traced and never silently lost."""
import pytest

import whisperx_windows as windows
import replicate_budget as budget
from recognition_provenance import begin_collection, end_collection
import transcription_quality as tq


class ReplicateError(Exception):
    pass


THROTTLED = ReplicateError(
    "ReplicateError Details:\nstatus: 429\ndetail: Request was throttled. Your rate limit "
    "for creating predictions is reduced to 6 requests per minute with a burst of 1 "
    "requests while you have less than $5.0 in credit. Your rate limit resets in ~2s.")
OK = dict(detected_language="es", _language_detection_probability=.91,
          segments=[dict(start=1, end=4, text="Abrázame muy fuerte",
                         words=[dict(word="Abrázame", start=1, end=2)])])


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(windows, "audio_windows", lambda _: [(38.0, 64.0)])
    monkeypatch.setattr(windows.subprocess, "run",
                        lambda args, **kw: open(args[-1], "wb").write(b"x"))
    sleeps = []
    monkeypatch.setattr(windows.time, "sleep", sleeps.append)
    script = []

    def call(model, factory, *, error_sink=None, **kw):
        factory()["audio_file"].close()
        item = script.pop(0)
        if isinstance(item, Exception):
            error_sink.append(item)
            return None
        return item
    monkeypatch.setattr(budget, "call_with_budget", call)
    fallback = []
    monkeypatch.setattr(windows, "_independent_retry", lambda *a: fallback.pop(0))
    return script, sleeps, fallback


def run():
    collector, token = begin_collection()
    try:
        result = windows.transcribe_windows("audio.wav", "m:h", {}, 480)
    finally:
        end_collection(token)
    return result, collector.snapshot()["window_outcomes"]


def test_rate_limited_window_waits_provider_hint_and_keeps_whisperx(env):
    script, sleeps, fallback = env
    script += [THROTTLED, THROTTLED, OK]
    result, outcomes = run()
    assert sleeps == [3.0, 3.0]                       # "resets in ~2s" + 1 s
    assert result["segments"][0]["text"] == "Abrázame muy fuerte"
    assert result["segments"][0]["words"][0]["start"] == 39.0
    (o,) = outcomes
    assert o["attempts"] == 3 and o["errors"] == ["rate_limited", "rate_limited"]
    assert o["whisperx"] == "succeeded" and o["source"] == "whisperx"
    assert o["language"] == "es" and o["language_probability"] == .91
    assert o["decision"] == "accepted"


def test_retry_limit_then_fallback_hallucination_is_marked_unrecognized(env, monkeypatch):
    script, sleeps, fallback = env
    monkeypatch.setenv("WHISPERX_WINDOW_MAX_ATTEMPTS", "3")
    script += [THROTTLED] * 3
    fallback.append(([dict(start=0, end=20, text="Subtítulos realizados por la comunidad de Amara.org")], "spanish"))
    result, outcomes = run()
    assert len(sleeps) == 2 and result["segments"] == []
    (o,) = outcomes
    assert o["attempts"] == 3 and o["whisperx"] == "failed"
    assert o["fallback"] == "openai/whisper-1" and o["hallucinations_discarded"] == 1
    assert o["decision"] == "unrecognized"
    review = windows.unrecognized_windows(outcomes)
    assert review == [{"start": 38.0, "end": 64.0, "reason": "vocal_window_unrecognized",
                       "attempts": 3, "errors": ["rate_limited"] * 3}]


def test_deterministic_provider_error_is_not_retried(env):
    script, sleeps, fallback = env
    script.append(ReplicateError("prediction failed: expected 2d or 3d input"))
    fallback.append(([dict(start=1, end=4, text="Abrázame muy fuerte")], "spanish"))
    result, outcomes = run()
    assert sleeps == [] and outcomes[0]["attempts"] == 1
    assert outcomes[0]["errors"] == ["provider_error"]
    assert outcomes[0]["source"] == "whisper-1" and outcomes[0]["decision"] == "accepted"


def test_network_error_is_retried(env):
    script, sleeps, _ = env
    script += [ConnectionError("[Errno 32] Broken pipe"), OK]
    _, outcomes = run()
    assert outcomes[0]["errors"] == ["transient_network"] and len(sleeps) == 1


def test_low_confidence_language_path_is_unchanged_and_traced(env):
    script, sleeps, fallback = env
    script.append(dict(OK, detected_language="en", _language_detection_probability=.28))
    fallback.append(([dict(start=1, end=4, text="Abrázame fuerte")], "spanish"))
    result, outcomes = run()
    assert sleeps == [] and result["segments"][0]["text"] == "Abrázame fuerte"
    o = outcomes[0]
    assert o["language"] == "en" and o["language_probability"] == .28
    assert o["fallback_reason"] == "low_language_confidence" and o["source"] == "whisper-1"


def test_empty_successful_window_is_no_voice_not_incomplete(env):
    script, _, _ = env
    script.append(dict(detected_language="es", _language_detection_probability=.9, segments=[]))
    _, outcomes = run()
    assert outcomes[0]["decision"] == "no_voice"
    assert windows.unrecognized_windows(outcomes) == []


def test_call_with_budget_exposes_last_error(monkeypatch):
    import sys, types
    def create(**kw):
        raise THROTTLED
    monkeypatch.setitem(sys.modules, "replicate", types.SimpleNamespace(
        predictions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(budget, "start_replicate_provenance", lambda *a: None)
    monkeypatch.setattr(budget, "finish_replicate_provenance", lambda *a, **k: None)
    sink = []
    out = budget.call_with_budget("m:h", lambda: {}, total_budget_s=10, backoff=[0],
                                  call_label="WHISPERX", error_sink=sink)
    assert out is None and sink == [THROTTLED]


def test_incomplete_window_blocks_pass_and_shadow_approval():
    coverage = {"audio_coverage": 1.0, "uncovered_seconds": 0, "text_mismatches": 0, "voiced_gap_s": 0}
    segs = [{"start": 1.0, "end": 3.0, "text": "hola mundo"}]
    window = {"start": 38.0, "end": 64.0, "reason": "vocal_window_unrecognized"}
    q = tq.evaluate(segs, coverage, unsafe_windows=[window])
    codes = {r["code"]: r for r in q["reasons"]}
    assert codes["incomplete_transcription"]["severity"] == "critical"
    assert codes["incomplete_transcription"]["value"] == 26.0
    assert q["decision"] != "pass"
    assert q["shadow_decision"]["eligible"] is False
    assert q["shadow_decision"]["would_approve"] is False
    assert any(w.get("reason") == "vocal_window_unrecognized" for w in q["unsafe_windows"])
