from __future__ import annotations

from learning_triggers import (
    TRIGGER_SPECS,
    catalog_training_authorization,
    env_enabled,
    lora_retraining_eligibility,
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


def test_lora_trigger_requires_100_corrected_songs_and_artist_diversity():
    assert lora_retraining_eligibility(
        corrected_songs=99, distinct_artists=30,
    )["reasons"] == ["insufficient_human_corrected_songs"]
    assert lora_retraining_eligibility(
        corrected_songs=100, distinct_artists=19,
    )["reasons"] == ["insufficient_artist_diversity"]
    eligible = lora_retraining_eligibility(
        corrected_songs=100, distinct_artists=20,
    )
    assert eligible["eligible"] is True
    assert eligible["due_bucket"] == 1
    assert eligible["split_policy"] == "song_and_artist_disjoint"


def test_t4_uses_the_selector_executor_once(monkeypatch):
    monkeypatch.delenv("REALIGN_SELECTOR_EXECUTOR", raising=False)
    result = run_realign_selector_trigger(bucket=1, corpus_songs=200)
    assert result["status"] == "blocked_executor_missing"
    assert result["companion_triggers"] == ["t4_95"]
