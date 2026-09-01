from __future__ import annotations

from learning_triggers import TRIGGER_SPECS, catalog_training_authorization, env_enabled


def test_catalog_authorization_is_fail_closed(monkeypatch):
    monkeypatch.delenv("CATALOG_AUDIO_TRAINING_ENABLED", raising=False)
    assert catalog_training_authorization()["authorized"] is False
    monkeypatch.setenv("CATALOG_AUDIO_TRAINING_ENABLED", "1")
    status = catalog_training_authorization()
    assert status["authorized"] is True
    assert status["authorization_reference"] is None


def test_trigger_thresholds_are_explicit(monkeypatch):
    monkeypatch.setenv("CORPUS_RETRAIN_EVERY_SONGS", "100")
    assert TRIGGER_SPECS["lora_retraining"]["threshold_env"] == "CORPUS_RETRAIN_EVERY_SONGS"
    assert env_enabled("LORA_V1_AUTORETRAIN_ENABLED") is False
