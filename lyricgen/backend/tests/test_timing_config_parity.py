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
    monkeypatch.delenv("LYRIC_MIN_GAP_MS", raising=False)
    monkeypatch.delenv("LYRIC_MIN_GAP_AB_ENABLED", raising=False)
    monkeypatch.delenv("LYRIC_MIN_GAP_AB_ARM_B_MS", raising=False)

    assert runtime_timing_config() == {
        "lyric_hold_s": 0.5,
        "lyric_lead_in_s": 0.0,
        "lyric_lead_in_ms": 0,
        "stable_pitch_tail_enabled": False,
        "lyric_min_gap_ms": 0,
        "lyric_min_gap_ab_enabled": False,
        "lyric_min_gap_ab_arm_b_ms": 300,
    }


def test_runtime_timing_config_uses_same_safe_hold_default_as_pipeline(monkeypatch):
    monkeypatch.delenv("LYRIC_HOLD_S", raising=False)
    assert runtime_timing_config()["lyric_hold_s"] == 0.5


def test_parity_requires_render_and_quality_workers_to_match_too():
    config = {"lyric_hold_s": 0.5}
    result = timing_config_parity([
        _row("ShortWorker", config),
        _row("BatchShortWorker", config, queues=["transcription_batch"]),
        _row(
            "quality-worker", {"lyric_hold_s": 0.25},
            queues=["transcription_quality"],
        ),
    ], api_config=config)

    assert result["match"] is False
    assert result["participants"] == 4
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


# --- Valor efectivo y origen (no participa de la paridad) -------------------

import ast  # noqa: E402

from observability import TIMING_ENV, runtime_timing_config_effective  # noqa: E402

_TIMING_VARS = [env for _key, env, *_rest in TIMING_ENV]


def _clear(monkeypatch):
    for name in _TIMING_VARS:
        monkeypatch.delenv(name, raising=False)


def test_effective_view_distinguishes_unset_from_zero(monkeypatch):
    _clear(monkeypatch)
    unset = runtime_timing_config_effective()["lyric_lead_in_ms"]
    assert unset["value"] == 80 and unset["origin"] == "default" and unset["raw"] is None
    monkeypatch.setenv("LYRIC_LEAD_IN_MS", "0")
    zero = runtime_timing_config_effective()["lyric_lead_in_ms"]
    assert zero["value"] == 0 and zero["origin"] == "env" and zero["raw"] == "0"
    # La vista legacy (la que gatea la paridad) no cambia de forma ni de valor.
    assert runtime_timing_config()["lyric_lead_in_ms"] == 0


def test_effective_view_reports_empty_and_invalid(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LYRIC_HOLD_S", "")
    monkeypatch.setenv("LYRIC_MIN_GAP_MS", "abc")
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.4")
    effective = runtime_timing_config_effective()
    assert effective["lyric_hold_s"] == {**effective["lyric_hold_s"], "value": 0.5, "origin": "empty"}
    assert effective["lyric_min_gap_ms"]["origin"] == "invalid"
    assert effective["lyric_min_gap_ms"]["value"] == 0
    assert effective["lyric_lead_in_s"]["value"] == 0.15    # tope de lead_in


def test_effective_defaults_match_the_consumers(monkeypatch):
    _clear(monkeypatch)
    import lead_in
    import line_gap_experiment

    effective = runtime_timing_config_effective()
    assert effective["lyric_hold_s"]["value"] == lead_in.hold_seconds()
    assert effective["lyric_lead_in_s"]["value"] == lead_in.lead_seconds()
    assert effective["lyric_min_gap_ms"]["value"] / 1000 == lead_in.min_gap_seconds()
    assert effective["lyric_min_gap_ab_enabled"]["value"] == line_gap_experiment.enabled()
    assert effective["lyric_min_gap_ab_arm_b_ms"]["value"] == line_gap_experiment.arm_b_ms()
    source = (Path(__file__).resolve().parents[1] / "whisperx_transcribe.py").read_text()
    default_lead_ms = next(
        node.value.value for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "_DEFAULT_LEAD_MS" for t in node.targets)
    )
    assert effective["lyric_lead_in_ms"]["value"] == default_lead_ms


def test_effective_view_never_changes_the_parity_gate():
    config = runtime_timing_config()
    effective = runtime_timing_config_effective()
    other = {k: {**v, "value": "different"} for k, v in effective.items()}
    rows = [
        {**_row("ShortWorker", config), "timing_config_effective": other},
        _row("BatchWorker", config),                       # release anterior
    ]
    parity = timing_config_parity(rows, api_config=config)
    assert parity["match"] is True
    assert parity["effective_match"] is False
    assert parity["effective_unreported"] == ["BatchWorker:batchworker"]
