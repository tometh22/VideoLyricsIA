"""API/worker timing settings are an explicit operational gate."""
from __future__ import annotations

from pathlib import Path

from observability import runtime_timing_config, timing_config_parity


def _row(service: str, config: dict | None, *, queues=None) -> dict:
    row = {
        "service": service,
        "worker": service.lower(),
        "queues": queues or ["transcription"],
    }
    if config is not None:
        row["timing_config"] = config
    return row


def test_runtime_timing_config_normalizes_equivalent_values(monkeypatch):
    monkeypatch.setenv("LYRIC_HOLD_S", ".50")
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.00")
    monkeypatch.setenv("LYRIC_LEAD_IN_MS", "000")
    monkeypatch.setenv("STABLE_PITCH_TAIL_ENABLED", "false")

    assert runtime_timing_config() == {
        "lyric_hold_s": 0.5,
        "lyric_lead_in_s": 0.0,
        "lyric_lead_in_ms": 0,
        "stable_pitch_tail_enabled": False,
    }


def test_parity_matches_api_and_both_transcription_worker_families():
    config = {"lyric_hold_s": 0.5}
    result = timing_config_parity([
        _row("ShortWorker", config),
        _row("BatchShortWorker", config, queues=["transcription_batch"]),
        _row(
            "quality-worker", {"lyric_hold_s": 0.25},
            queues=["transcription_quality"],
        ),
    ], api_config=config)

    assert result["match"] is True
    assert result["participants"] == 3
    assert result["missing"] == []


def test_parity_fails_when_hold_differs():
    result = timing_config_parity([
        _row(
            "BatchShortWorker", {"lyric_hold_s": 0.5},
            queues=["transcription_batch"],
        ),
    ], api_config={"lyric_hold_s": 0.25})

    assert result["match"] is False
    assert result["missing"] == []


def test_parity_fails_when_transcription_worker_does_not_publish():
    result = timing_config_parity([
        _row("ShortWorker", None),
    ], api_config={"lyric_hold_s": 0.5})

    assert result["match"] is False
    assert result["missing"] == ["ShortWorker:shortworker"]


def test_worker_heartbeat_publishes_timing_config():
    source = (Path(__file__).resolve().parents[1] / "worker.py").read_text()
    assert '"timing_config": _timing_config' in source
