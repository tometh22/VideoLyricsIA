import asyncio
from types import SimpleNamespace

import pipeline
from pipeline import _tag_recognition_family, transcription_family
from recognition_provenance import (
    begin_collection,
    bounded_provider_string,
    clear_collection,
    end_collection,
    provider_text_completion,
    record_completed,
    response_text_completion,
    resume_from_result,
    snapshot_into_result,
)
import whisperx_transcribe


def test_bounded_provider_string_never_raises_for_opaque_sdk_values():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    assert bounded_provider_string(Unprintable()) == (
        "<opaque-provider-value-Unprintable>"
    )


def test_provider_text_completion_preserves_unprintable_success():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    text, events = provider_text_completion(
        Unprintable(), label="opaque-test-text",
    )

    assert text == ""
    assert events == [{
        "raw": "<opaque-test-text-Unprintable>",
        "serialization_error": "RuntimeError",
    }]


def test_response_text_completion_preserves_hostile_attribute():
    class HostileResponse:
        @property
        def text(self):
            raise ValueError("blocked response")

    text, events = response_text_completion(
        HostileResponse(), label="opaque-test-response",
    )

    assert text == ""
    assert events == [{
        "raw": "<opaque-test-response-HostileResponse>",
        "serialization_error": "ValueError",
    }]


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

    def _primary(*args, **kwargs):
        record_completed(
            family="openai/whisper-1", events=initial,
            view="mix", transformation="full_file_raw",
        )
        return [dict(row) for row in initial]

    def _vad(*args, **kwargs):
        record_completed(
            family="openai/whisper-1", events=recovered,
            view="mix", transformation="vad_chunk_raw:index=0",
        )
        return [dict(row) for row in recovered]

    monkeypatch.setattr(
        pipeline, "_transcribe_via_openai_api", _primary,
    )
    monkeypatch.setattr(
        pipeline, "_vad_chunk_transcribe", _vad,
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
    ] == ["full_file_raw", "vad_chunk_raw:index=0"]
    assert [len(row["events"]) for row in snapshot["hypotheses"]] == [1, 5]


def test_vad_records_each_completed_provider_chunk_separately(monkeypatch):
    chunks = [(0.0, 10.0), (8.0, 18.0)]
    monkeypatch.setattr(
        pipeline, "_build_chunks_from_audio", lambda _path: chunks,
    )
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    def transcribe_chunk(_path, **kwargs):
        raw = [{"start": 1.0, "end": 2.0, "text": "raw chunk"}]
        record_completed(
            family="openai/whisper-1", events=raw,
            view=kwargs["provenance_view"],
            transformation=kwargs["provenance_transformation"],
        )
        return [dict(row) for row in raw]

    monkeypatch.setattr(
        pipeline, "_transcribe_via_openai_api", transcribe_chunk,
    )
    collector, token = begin_collection()
    try:
        selected = pipeline._vad_chunk_transcribe(
            "fixture.wav", provenance_view="stem",
        )
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert [row["start"] for row in selected] == [1.0, 9.0]
    assert snapshot["completed_attempt_count"] == 2
    assert [row["view"] for row in snapshot["hypotheses"]] == [
        "stem", "stem",
    ]
    assert [row["transformation"] for row in snapshot["hypotheses"]] == [
        "vad_chunk_raw:index=0;start=0.000;end=10.000",
        "vad_chunk_raw:index=1;start=8.000;end=18.000",
    ]


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
        "full_file_raw", "late_onset_retry_raw", "sparse_result_retry_raw",
    ]


def test_local_whisper_freezes_rows_before_candidate_filters(monkeypatch):
    model = _FakeLocalModel([[
        _local_segment("real lyric", 1.0, 2.0),
        {
            **_local_segment("Subtitles by Amara.org", 2.0, 3.0),
            "no_speech_prob": 0.99,
        },
    ]])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("WHISPER_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(pipeline, "_get_whisper_model", lambda _name: model)

    collector, token = begin_collection()
    try:
        selected = pipeline.transcribe("fixture.wav", provenance_view="mix")
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert [row["text"] for row in selected] == ["real lyric"]
    assert [
        row["text"] for row in snapshot["hypotheses"][0]["events"]
    ] == ["real lyric", "Subtitles by Amara.org"]
    assert snapshot["hypotheses"][0]["transformation"] == "full_file_raw"


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


def test_postpass_collection_extends_orchestrator_snapshot():
    initial, token = begin_collection()
    try:
        record_completed(
            family="primary/family",
            events=[{"text": "first", "start": 0.0, "end": 1.0}],
        )
        prefix = initial.snapshot()
    finally:
        end_collection(token)

    result = {
        "_recognition_hypotheses": prefix["hypotheses"],
        "_recognition_attempt_count": prefix["completed_attempt_count"],
    }
    try:
        resume_from_result(result)
        record_completed(
            family="postpass/family",
            events=[{"text": "second", "start": 1.0, "end": 2.0}],
            transformation="gap_rescue",
        )
        snapshot_into_result(result)
    finally:
        clear_collection()

    assert result["_recognition_attempt_count"] == 2
    assert [
        row["attempt_id"] for row in result["_recognition_hypotheses"]
    ] == [0, 1]
    assert [
        row["family"] for row in result["_recognition_hypotheses"]
    ] == ["primary/family", "postpass/family"]
