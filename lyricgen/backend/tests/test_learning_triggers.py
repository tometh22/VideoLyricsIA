from __future__ import annotations

from learning_triggers import (
    TRIGGER_SPECS,
    catalog_training_authorization,
    env_enabled,
    run_realign_selector_trigger,
)


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
    assert TRIGGER_SPECS["realignment_selector"]["threshold_default"] == 200
    assert TRIGGER_SPECS["realignment_selector"]["companion_triggers"] == ("t4_95",)
    assert env_enabled("LORA_V1_AUTORETRAIN_ENABLED") is False


def test_t4_uses_the_selector_executor_once(monkeypatch):
    monkeypatch.delenv("REALIGN_SELECTOR_EXECUTOR", raising=False)
    result = run_realign_selector_trigger(bucket=1, corpus_songs=200)
    assert result["status"] == "blocked_executor_missing"
    assert result["companion_triggers"] == ["t4_95"]


def test_lora_retraining_trigger_is_300_train_role_songs():
    """Archivo de LoRA v1: el trigger sube a 300 y cuenta solo rol train."""
    import learning_triggers as lt
    spec = lt.TRIGGER_SPECS["lora_retraining"]
    assert spec["threshold_default"] == 300
    assert spec["count"] == "train_role_songs"
    assert spec["baseline_to_beat"] == "data/lora_v1_archive.json"
    import json
    from pathlib import Path
    archive = json.loads((Path(lt.__file__).parent / "data" / "lora_v1_archive.json").read_text())
    assert archive["status"] == "archived"
    assert archive["holdout_evaluation"]["delta_wer_candidate_minus_baseline"] == 0.2182
    assert archive["retraining_trigger"]["threshold_train_role_songs"] == 300
