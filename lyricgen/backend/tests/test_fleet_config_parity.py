"""The fleet-config parity check used before promotions (scripts/check_fleet_config_parity.py)."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_fleet_config_parity", Path(__file__).parents[1] / "scripts" / "check_fleet_config_parity.py")
parity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parity)


def test_identical_services_have_no_drift():
    envs = {"api": {"A": "1", "B": ""}, "Worker": {"A": "1"}}
    assert parity.diff_services(envs, ["A", "B"]) == {}


def test_a_missing_key_counts_as_drift_and_values_are_reported_per_service():
    envs = {"api": {"A": "1"}, "Worker": {}, "quality-worker": {"A": "1"}}
    assert parity.diff_services(envs, ["A"]) == {"A": {"api": "1", "Worker": "", "quality-worker": "1"}}


def test_conflicting_values_are_drift():
    assert parity.diff_services({"api": {"A": "0.30"}, "Worker": {"A": "0.35"}}, ["A"])


def test_secret_looking_values_are_never_printed():
    out = parity.render({"X_TOKEN": {"api": "abc", "Worker": ""}, "A": {"api": "1", "Worker": ""}})
    assert "abc" not in out and "<oculto>" in out and "A: api=1, Worker=∅" in out


def test_the_real_key_list_matches_the_token_inputs():
    keys = parity.config_keys()
    assert "CTC_ALIGN_MIN_MED_SCORE" in keys and "QUALITY_V6_PROPOSALS_ENABLED" in keys
    assert "TRANSCRIPTION_QUALITY_CALIBRATION_ID" in keys and len(keys) == len(set(keys))


def test_exit_codes(monkeypatch):
    monkeypatch.setattr(parity, "load_variables", lambda service, env: {"A": "1"} if service == "api" else {"A": "2"})
    monkeypatch.setattr(parity, "config_keys", lambda: ("A",))
    assert parity.main(["x", "production", "api", "Worker"]) == 1
    monkeypatch.setattr(parity, "load_variables", lambda service, env: {"A": "1"})
    assert parity.main(["x", "production", "api", "Worker"]) == 0

    def boom(service, env):
        raise OSError("no railway")
    monkeypatch.setattr(parity, "load_variables", boom)
    assert parity.main(["x", "production"]) == 2
