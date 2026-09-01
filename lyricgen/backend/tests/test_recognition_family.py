import asyncio

import pipeline
from pipeline import _tag_recognition_family, transcription_family
from recognition_provenance import (
    begin_collection,
    end_collection,
    record_completed,
)
import whisperx_transcribe


def test_transport_marker_records_exact_selected_local_model(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = _tag_recognition_family(
        [{"start": 0.0, "end": 1.0, "text": "line"}],
        "openai-whisper/large-v3-local",
    )
    assert transcription_family(rows) == "openai-whisper/large-v3-local"


def test_production_family_is_openai_whisper_1(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    assert transcription_family([]) == "openai/whisper-1"


def test_whisperx_family_includes_pinned_provider_model():
    family = whisperx_transcribe.recognition_family()
    assert family.startswith("replicate/")
    assert ":" in family


def test_api_fallback_preserves_rejected_and_selected_attempts(monkeypatch):
    initial = [{
        "start": 0.0, "end": 50.0, "text": "collapsed initial",
    }]
    recovered = [
        {"start": 0.0, "end": 1.0, "text": "first line"},
        {"start": 2.0, "end": 3.0, "text": "second line"},
        {"start": 4.0, "end": 5.0, "text": "third line"},
        {"start": 6.0, "end": 7.0, "text": "fourth line"},
        {"start": 8.0, "end": 9.0, "text": "fifth line"},
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setattr(
        pipeline, "_transcribe_via_openai_api",
        lambda *args, **kwargs: [dict(row) for row in initial],
    )
    monkeypatch.setattr(
        pipeline, "_vad_chunk_transcribe",
        lambda *args, **kwargs: [dict(row) for row in recovered],
    )
    monkeypatch.setattr(pipeline, "_audio_duration", lambda _path: 60.0)
    monkeypatch.setattr(
        pipeline, "_detect_hallucination",
        lambda *args, **kwargs: (False, ""),
    )

    collector, token = begin_collection()
    try:
        selected = pipeline.transcribe(
            "fixture.wav", return_words=True, provenance_view="mix",
        )
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert [row["text"] for row in selected] == [
        "first line", "second line", "third line", "fourth line",
        "fifth line",
    ]
    assert snapshot["completed_attempt_count"] == 2
    assert [
        row["transformation"] for row in snapshot["hypotheses"]
    ] == ["full_file", "vad_chunks_retry"]
    assert [len(row["events"]) for row in snapshot["hypotheses"]] == [1, 5]


class _FakeLocalModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def transcribe(self, _path, **_kwargs):
        return {"segments": self.outputs.pop(0)}


def _local_segment(text, start, end):
    return {
        "text": text, "start": start, "end": end,
        "no_speech_prob": 0.0,
        "words": [{"word": text, "start": start, "end": end}],
    }


def test_local_retries_remain_separate_named_attempts(monkeypatch):
    turbo = _FakeLocalModel([
        [_local_segment("late line", 35.0, 36.0)],
        [_local_segment("early line", 1.0, 2.0)],
    ])
    large = _FakeLocalModel([[
        _local_segment("large one", 1.0, 2.0),
        _local_segment("large two", 3.0, 4.0),
    ]])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        pipeline, "_get_whisper_model",
        lambda name: large if name == "large-v3" else turbo,
    )

    collector, token = begin_collection()
    try:
        pipeline.transcribe("fixture.wav", provenance_view="mix")
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert snapshot["completed_attempt_count"] == 3
    assert [row["family"] for row in snapshot["hypotheses"]] == [
        "openai-whisper/turbo-local",
        "openai-whisper/turbo-local",
        "openai-whisper/large-v3-local",
    ]
    assert [row["transformation"] for row in snapshot["hypotheses"]] == [
        "full_file", "late_onset_retry", "sparse_result_retry",
    ]


def test_attempt_counter_survives_hypothesis_serialization_loss():
    class BrokenDict(dict):
        def __deepcopy__(self, _memo):
            raise RuntimeError("cannot serialize")

    collector, token = begin_collection()
    try:
        record_completed(
            family="test/family", events=[BrokenDict(text="line")],
        )
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert snapshot["completed_attempt_count"] == 1
    assert snapshot["hypotheses"] == []


def test_collector_context_reaches_provider_worker_thread():
    collector, token = begin_collection()
    try:
        asyncio.run(asyncio.to_thread(
            record_completed,
            family="threaded/family",
            events=[{"text": "line", "start": 0.0, "end": 1.0}],
        ))
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert snapshot["completed_attempt_count"] == 1
    assert snapshot["hypotheses"][0]["family"] == "threaded/family"
