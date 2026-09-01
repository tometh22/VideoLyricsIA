"""Tier 3a — worker queue segmentation.

Guards the QUEUES env parsing that lets the same image run as a segmented fleet
(a dedicated ShortWorker pool for transcription/bg_preview so they never wait
behind renders). The critical property: the DEFAULT is all four queues, so
shipping the code is a zero-behavior-change until QUEUES is set per service.
"""

import worker


def test_default_is_all_queues_in_priority_order(monkeypatch):
    # canary al final (2026-07-17): el bot golden drena último.
    monkeypatch.delenv("QUEUES", raising=False)
    assert worker._resolve_queue_names() == [
        "transcription", "bg_preview", "enterprise", "default", "audio_preview", "canary",
    ]


def test_short_worker_subset(monkeypatch):
    monkeypatch.setenv("QUEUES", "transcription,bg_preview")
    assert worker._resolve_queue_names() == ["transcription", "bg_preview"]


def test_render_worker_subset(monkeypatch):
    monkeypatch.setenv("QUEUES", "enterprise,default")
    assert worker._resolve_queue_names() == ["enterprise", "default"]


def test_whitespace_and_blanks_tolerated(monkeypatch):
    monkeypatch.setenv("QUEUES", " enterprise , , default ,")
    assert worker._resolve_queue_names() == ["enterprise", "default"]


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("QUEUES", "   ")
    assert worker._resolve_queue_names() == [
        "transcription", "bg_preview", "enterprise", "default", "audio_preview", "canary",
    ]
