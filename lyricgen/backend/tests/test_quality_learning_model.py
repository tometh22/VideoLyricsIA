"""Signed, interpretable shadow model capability boundaries."""
import base64
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_attestation import sign_artifact
from quality_learning_model import load_verified_artifact, predict
from scripts.train_quality_learning_model import _split, _split_group


def _artifact(private_b64):
    return sign_artifact({
        "schema": "quality-learning-model-v1", "artifact_id": "model-1",
        "mode": "shadow", "writes_lyrics": False,
        "models": {
            "missing_event": {
                "prior": 0.2,
                "features": {
                    "is_live=true": {"positive": 0.9, "negative": 0.1},
                },
                "calibration": [],
            },
        },
    }, private_b64, "model-key")


def test_model_only_suggests_route_and_never_mutates():
    output = predict({
        "models": {
            "missing_event": {
                "prior": 0.2,
                "features": {"is_live=true": {"positive": 0.9, "negative": 0.1}},
                "calibration": [],
            },
        },
    }, {"is_live": True})
    assert output["suggested_route"] == "mix_witness_second_asr"
    assert output["mutated_segments"] is False
    assert "segments" not in output and "text" not in output


def test_training_split_has_no_artist_leakage_across_songs_or_masters():
    first = _split_group(
        SimpleNamespace(artist="Los Pericos", song_title="Tema A"),
        SimpleNamespace(audio_hash="a" * 64, identity_hash="1" * 64),
    )
    second = _split_group(
        SimpleNamespace(artist="  los  pericos ", song_title="Tema B"),
        SimpleNamespace(audio_hash="b" * 64, identity_hash="2" * 64),
    )
    assert first == second
    assert _split("split-secret", first) == _split("split-secret", second)


def test_runtime_rejects_unsigned_or_write_capable_artifact(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    artifact = _artifact(base64.b64encode(private_raw).decode())
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv("QUALITY_LEARNING_MODEL_SHADOW_ENABLED", "1")
    monkeypatch.setenv("QUALITY_LEARNING_MODEL_PATH", str(path))
    monkeypatch.setenv("QUALITY_LEARNING_MODEL_PUBLIC_KEYS", json.dumps({
        "model-key": base64.b64encode(public_raw).decode(),
    }))
    loaded, reason = load_verified_artifact()
    assert reason == "verified" and loaded["artifact_id"] == "model-1"

    unsafe = sign_artifact({
        **{key: value for key, value in artifact.items() if key != "attestation"},
        "writes_lyrics": True,
    }, base64.b64encode(private_raw).decode(), "model-key")
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    assert load_verified_artifact()[1] == "unsafe_model_capability"
