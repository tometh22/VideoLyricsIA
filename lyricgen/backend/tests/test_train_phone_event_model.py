import base64
import json
import random

import numpy as np
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_attestation import sign_artifact
from scripts import train_phone_event_model as trainer
from scripts.train_phone_event_model import (
    AUGMENTATIONS,
    CPU_EXPORT_SCHEMA,
    _cpu_export_report,
    _parser,
    _validate_training_report_for_cpu_export,
    apply_pilot_augmentations,
    main,
    sha256_directory,
    sha256_file,
)


def _training_row(sample_rate=16_000, seconds=1.0, frames=50):
    timeline = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    waveform = .22 * np.sin(2 * np.pi * 220.0 * timeline)
    auxiliary = np.zeros((frames, 14), dtype=np.float32)
    auxiliary[:, 0] = 220.0
    auxiliary[:, 1] = 1.0
    auxiliary[:, 13] = .9
    timing = np.column_stack((
        np.linspace(0.0, seconds - .02, frames),
        np.linspace(.02, seconds, frames),
    )).astype(np.float32)
    timing[10:13] = -1.0
    return {
        "input_values": waveform,
        "phone_tokens": np.array([6, 7, 8], dtype=np.int64),
        "event_labels": np.zeros(frames, dtype=np.int64),
        "boundary_labels": np.zeros(frames, dtype=np.int64),
        "timing_targets": timing,
        "auxiliary_features": auxiliary,
    }


def test_all_declared_pilot_augmentations_really_transform_aligned_training_data():
    pytest.importorskip("librosa")
    source = _training_row()
    augmented, applied = apply_pilot_augmentations(
        source,
        sample_rate=16_000,
        randomizer=random.Random(1701),
        augmentation_names=list(AUGMENTATIONS),
    )

    assert set(applied) == set(AUGMENTATIONS)
    assert augmented["input_values"].dtype == np.float32
    assert np.isfinite(augmented["input_values"]).all()
    assert np.max(np.abs(augmented["input_values"])) <= 0.9991
    assert not np.array_equal(augmented["input_values"], source["input_values"])
    frame_count = augmented["auxiliary_features"].shape[0]
    assert augmented["event_labels"].shape == (frame_count,)
    assert augmented["boundary_labels"].shape == (frame_count,)
    assert augmented["timing_targets"].shape == (frame_count, 2)
    assert source["input_values"].shape == (16_000,)
    assert source["auxiliary_features"].shape == (50, 14)


def test_pilot_augmentation_is_seeded_and_does_not_mutate_source():
    pytest.importorskip("librosa")
    source = _training_row()
    original = {key: value.copy() for key, value in source.items()}
    first, first_applied = apply_pilot_augmentations(
        source, sample_rate=16_000, randomizer=random.Random(9),
    )
    second, second_applied = apply_pilot_augmentations(
        source, sample_rate=16_000, randomizer=random.Random(9),
    )

    assert first_applied == second_applied
    for key in first:
        np.testing.assert_allclose(first[key], second[key])
        np.testing.assert_array_equal(source[key], original[key])


def test_unknown_augmentation_and_invalid_auxiliary_layout_fail_closed():
    with pytest.raises(ValueError, match="unknown pilot augmentations"):
        apply_pilot_augmentations(
            _training_row(), sample_rate=16_000, randomizer=random.Random(1),
            augmentation_names=["magic"],
        )
    invalid = _training_row()
    invalid["auxiliary_features"] = np.zeros((50, 13), dtype=np.float32)
    with pytest.raises(ValueError, match=r"F0\+12 chroma\+voicing"):
        apply_pilot_augmentations(
            invalid, sample_rate=16_000, randomizer=random.Random(1),
            augmentation_names=[],
        )


def test_cpu_export_rejects_unsigned_training_report_before_loading_model(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("QUALITY_V6_TRAINING_PUBLIC_KEYS", raising=False)
    report_path = tmp_path / "training-report.json"
    report_path.write_text("{}", encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir()
    (base / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="attestation rejected"):
        _validate_training_report_for_cpu_export(
            {}, report_path=report_path, base_model_path=base,
            base_model_sha256="0" * 64,
        )


def test_cpu_export_report_is_honest_about_calibration_and_runtime(tmp_path):
    training_report_path = tmp_path / "training-report.json"
    training_report_path.write_text('{"status":"trained_uncalibrated"}', encoding="utf-8")
    model_path = tmp_path / "phone-event-int8-cpu.pt"
    model_path.write_bytes(b"real serialized model placeholder for metadata test")

    report = _cpu_export_report(
        {"dataset_manifest_sha256": "a" * 64, "base_model_sha256": "b" * 64},
        training_report_path=training_report_path,
        model_path=model_path,
        quantized_linear_modules=12,
        quantized_engine="qnnpack",
    )

    assert report["schema"] == CPU_EXPORT_SCHEMA
    assert report["status"] == "exported_uncalibrated"
    assert report["exported"] is True
    assert report["calibrated"] is False
    assert report["runtime_authorization"] is False
    assert report["automatic_apply_allowed"] is False
    assert report["quantization"]["linear_modules"] == 12


def test_cpu_export_quantizes_serializes_reloads_and_stays_unauthorized(
    tmp_path, monkeypatch,
):
    torch = pytest.importorskip("torch")
    safetensors = pytest.importorskip("safetensors.torch")

    class TinyPhoneEventModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.phone = torch.nn.Linear(1, 5)
            self.event = torch.nn.Linear(15, 3)
            self.boundary = torch.nn.Linear(15, 3)
            self.timing = torch.nn.Linear(15, 2)

        def forward(self, input_values, auxiliary):
            hidden = input_values[:, :50].unsqueeze(-1)
            combined = torch.cat((hidden, auxiliary[:, :50]), dim=-1)
            return {
                "phone": self.phone(hidden),
                "event": self.event(combined),
                "boundary": self.boundary(combined),
                "timing": self.timing(combined),
            }

    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private_b64 = base64.b64encode(private_raw).decode("ascii")
    public_b64 = base64.b64encode(public_raw).decode("ascii")
    monkeypatch.setenv(
        "QUALITY_V6_TRAINING_PUBLIC_KEYS",
        json.dumps({"training-test": public_b64}),
    )

    base_model = tmp_path / "base-model"
    base_model.mkdir()
    (base_model / "config.json").write_text('{"hidden_size":1}', encoding="utf-8")
    base_hash = sha256_directory(base_model)
    checkpoint = tmp_path / "research-checkpoint.safetensors"
    safetensors.save_file(TinyPhoneEventModel().state_dict(), checkpoint)
    report = sign_artifact({
        "schema": trainer.TRAINING_REPORT_SCHEMA,
        "policy_version": trainer.POLICY_VERSION,
        "status": "trained_uncalibrated",
        "offline_only": True,
        "calibrated": False,
        "exported": False,
        "runtime_authorization": False,
        "automatic_apply_allowed": False,
        "training_cases": 1,
        "dataset_manifest_sha256": "a" * 64,
        "base_model_sha256": base_hash,
        "checkpoint": {
            "path": checkpoint.name,
            "sha256": sha256_file(checkpoint),
            "deployment_export": False,
        },
    }, private_b64, "training-test")
    report_path = tmp_path / "training-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        trainer, "_build_model", lambda _path: (torch, TinyPhoneEventModel()),
    )

    output_dir = tmp_path / "cpu-export"
    exported = trainer.export_cpu_quantized(
        training_report_path=report_path,
        base_model_path=base_model,
        base_model_sha256=base_hash,
        output_dir=output_dir,
        private_key=private_b64,
        key_id="export-test",
    )

    assert exported["status"] == "exported_uncalibrated"
    assert exported["calibrated"] is False
    assert exported["runtime_authorization"] is False
    assert exported["quantization"]["linear_modules"] == 4
    assert (output_dir / "phone-event-int8-cpu.pt").is_file()
    assert (output_dir / "cpu-export-report.json").is_file()


def test_export_cpu_cli_requires_keys_and_never_creates_output(tmp_path, monkeypatch):
    args = _parser().parse_args([
        "export-cpu",
        "--training-report", str(tmp_path / "training-report.json"),
        "--base-model-path", str(tmp_path / "base"),
        "--base-model-sha256", "0" * 64,
        "--output-dir", str(tmp_path / "export"),
    ])
    assert args.command == "export-cpu"
    monkeypatch.delenv("QUALITY_V6_EXPORT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("QUALITY_V6_EXPORT_KEY_ID", raising=False)

    assert main([
        "export-cpu",
        "--training-report", str(tmp_path / "training-report.json"),
        "--base-model-path", str(tmp_path / "base"),
        "--base-model-sha256", "0" * 64,
        "--output-dir", str(tmp_path / "export"),
    ]) == 1
    assert not (tmp_path / "export").exists()
