"""Las compuertas de calidad tienen que ser visibles, no silenciosas.

El replay de calidad estuvo muerto 21 días en staging y desde siempre en
producción mientras /health devolvía ``ok``: sin calibración el score queda en
null, sin artefactos fijados las amarillas nunca ofrecen candidatos, y si el
token de runtime difiere entre servicios cada replay se descarta en 4 segundos
sin una línea de log.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability import quality_gates_snapshot  # noqa: E402


def _clear_pinned(monkeypatch) -> None:
    for name in (
        "QUALITY_V6_DATASET_MANIFEST_PATH", "QUALITY_V6_DATASET_MANIFEST_SHA256",
        "QUALITY_V6_CALIBRATION_PATH", "QUALITY_V6_CALIBRATION_SHA256",
        "QUALITY_V6_PROPOSALS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_calibration_is_red(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_QUALITY_CALIBRATED", raising=False)
    _clear_pinned(monkeypatch)
    gates = quality_gates_snapshot({})
    assert gates["state"] == "red"
    assert gates["calibrated"] is False
    assert "quality_calibration_unavailable" in gates["reasons"]


def test_pinned_artifacts_missing_is_reported(monkeypatch):
    _clear_pinned(monkeypatch)
    gates = quality_gates_snapshot({})
    proposals = gates["quality_v6_proposals"]
    assert proposals["authorized"] is False
    assert "pinned_artifacts_missing" in proposals["blockers"]
    assert "proposal_kill_switch_off" in proposals["blockers"]
    assert "quality_v6_proposals_blocked" in gates["reasons"]


def test_fleet_token_mismatch_is_red(monkeypatch):
    gates = quality_gates_snapshot({
        "worker_releases": [
            {"service": "ShortWorker", "runtime_token": "aaaaaaaaaaaaaaaa"},
            {"service": "quality-worker", "runtime_token": "bbbbbbbbbbbbbbbb"},
        ],
    })
    assert gates["fleet_runtime_token_match"] is False
    assert "fleet_runtime_token_mismatch" in gates["reasons"]
    assert len(gates["fleet_runtime_tokens"]) >= 2


def test_fleet_token_agreement_does_not_add_a_reason(monkeypatch):
    token = quality_gates_snapshot({})["runtime_token"]
    gates = quality_gates_snapshot({
        "worker_releases": [
            {"service": "ShortWorker", "runtime_token": token},
            {"service": "quality-worker", "runtime_token": token},
        ],
    })
    assert gates["fleet_runtime_token_match"] is True
    assert "fleet_runtime_token_mismatch" not in gates["reasons"]


def test_snapshot_always_reports_a_token_and_a_state(monkeypatch):
    gates = quality_gates_snapshot({})
    assert gates["state"] in {"red", "green"}
    assert isinstance(gates["runtime_token"], (str, type(None)))
    assert gates["policy_version"]
