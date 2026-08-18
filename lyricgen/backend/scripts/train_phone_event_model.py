#!/usr/bin/env python3
"""Offline GPU scaffold for an XLS-R phone/event model.

Heavy ML dependencies are imported only by the explicit ``train``/``smoke``
commands.  Hugging Face is forced into local-only mode, so this script never
downloads a model.  Training produces a signed research checkpoint report
whose state is always ``trained_uncalibrated`` and whose runtime/export flags
are false.  Only a separate signed v6 calibration can evaluate it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from evidence_attestation import (  # noqa: E402
    sign_artifact,
    verify_artifact,
    write_json_exclusive,
)
from quality_v6_calibration import (  # noqa: E402
    POLICY_VERSION,
    TRAINING_REPORT_SCHEMA,
    artifact_sha256,
    validate_dataset_manifest,
)


ARCHITECTURE = "xls-r-phone-event-v1"
DEFAULT_BASE_MODEL = "facebook/wav2vec2-xls-r-300m"
PHONE_VOCAB = (
    "<blank>", "<sil>", "<unk>", "<voc>", "<speech>", "<crowd>",
    "a", "e", "i", "o", "u", "b", "d", "f", "g", "k", "l", "m",
    "n", "ɲ", "p", "r", "ɾ", "s", "t", "x", "ʝ", "tʃ", "w", "j",
)
EVENT_CLASSES = ("SUNG_LEAD", "SUNG_CROWD", "SPEECH", "NONLEXICAL", "CROWD_NOISE", "UNKNOWN")
BOUNDARY_CLASSES = ("CONTINUE", "SUBEVENT", "PHRASE")
AUGMENTATIONS = (
    "tempo", "pitch", "reverberation", "clipping", "crowd",
    "backing_vocals", "degraded_separation",
)
CPU_EXPORT_SCHEMA = "lyrics-quality-v6-phone-event-cpu-export-v1"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(path: Path) -> str:
    """Content-address a local checkpoint directory without following links."""
    root = path.resolve()
    digest = hashlib.sha256()
    files = sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    if not files:
        raise ValueError("base model directory is empty")
    for candidate in files:
        resolved = candidate.resolve()
        if root not in resolved.parents:
            raise ValueError(f"base model contains escaping symlink: {candidate}")
        relative = resolved.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(resolved)))
    return digest.hexdigest()


def create_training_plan(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    base_model_path: Path,
    base_model_sha256: str,
    epochs: int,
    learning_rate: float,
) -> dict[str, Any]:
    errors = validate_dataset_manifest(manifest, require_signature=True, require_adequate=True)
    if errors:
        raise ValueError("signed dataset rejected: " + "; ".join(errors))
    if not base_model_path.is_dir():
        raise ValueError("base model must be an existing local directory")
    actual_base_hash = sha256_directory(base_model_path)
    if actual_base_hash != base_model_sha256:
        raise ValueError("base model directory SHA-256 mismatch")
    if epochs <= 0 or not 0 < learning_rate <= 0.1:
        raise ValueError("epochs and learning rate must be positive and bounded")
    return {
        "schema": "lyrics-quality-v6-phone-event-training-plan-v1",
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline_only": True,
        "architecture": ARCHITECTURE,
        "base_model": DEFAULT_BASE_MODEL,
        "base_model_local_path": str(base_model_path.resolve()),
        "base_model_sha256": actual_base_hash,
        "dataset_manifest_path": str(manifest_path.resolve()),
        "dataset_manifest_sha256": artifact_sha256(manifest),
        "dataset_summary": manifest["summary"],
        "heads": {
            "phone_ctc": list(PHONE_VOCAB),
            "event": list(EVENT_CLASSES),
            "boundary": list(BOUNDARY_CLASSES),
            "timing": ["onset", "offset"],
        },
        "features": ["xls_r", "f0", "chroma"],
        "augmentations": list(AUGMENTATIONS),
        "augmentation_policy": {
            "clean_probability": 0.20,
            "maximum_per_example": 3,
            "seeded": True,
            "tempo_updates_frame_targets": True,
            "pitch_updates_f0_chroma": True,
        },
        "optimization": {
            "epochs": epochs,
            "learning_rate": learning_rate,
            "device": "cuda",
            "batch_size": 1,
            "losses": ["ctc", "event_cross_entropy", "boundary_cross_entropy", "timing_smooth_l1"],
        },
        "status": "planned",
        "trained": False,
        "calibrated": False,
        "exported": False,
        "runtime_authorization": False,
    }


def _training_examples(
    manifest: dict[str, Any], manifest_path: Path,
) -> list[tuple[Path, str, dict[str, Any]]]:
    root = manifest_path.resolve().parent
    examples: list[tuple[Path, str, dict[str, Any]]] = []
    for entry in manifest["entries"]:
        if entry["split"] != "training":
            continue
        descriptor = entry["training_example"]
        relative = Path(descriptor["path"])
        if relative.is_absolute():
            raise ValueError("training example paths must be relative")
        resolved = (root / relative).resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError("training example path escapes manifest directory")
        if not resolved.is_file() or sha256_file(resolved) != descriptor["sha256"]:
            raise ValueError(f"training example missing or hash mismatch: {relative}")
        examples.append((resolved, entry["case_id"], descriptor))
    return examples


def _load_example(path: Path) -> dict[str, Any]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        required = {
            "input_values", "phone_tokens", "event_labels",
            "boundary_labels", "timing_targets", "auxiliary_features",
        }
        missing = required - set(archive.files)
        if missing:
            raise ValueError(f"{path}: missing arrays {', '.join(sorted(missing))}")
        result = {name: archive[name] for name in required}
    if result["input_values"].ndim != 1 or result["input_values"].size < 320:
        raise ValueError(f"{path}: input_values must be a non-trivial mono waveform")
    if result["phone_tokens"].ndim != 1 or result["phone_tokens"].size == 0:
        raise ValueError(f"{path}: phone_tokens must be one-dimensional and non-empty")
    frame_count = result["event_labels"].shape[0]
    if (
        result["event_labels"].ndim != 1
        or result["boundary_labels"].shape != (frame_count,)
        or result["timing_targets"].shape != (frame_count, 2)
        or result["auxiliary_features"].shape != (frame_count, 14)
    ):
        raise ValueError(f"{path}: labels and 14-D F0/chroma features must share a frame axis")
    return result


def _resample_frame_axis(values, length: int, *, nearest: bool = False):
    """Resize frame-aligned arrays without importing training frameworks."""
    import numpy as np

    source = np.asarray(values)
    if source.ndim not in {1, 2} or source.shape[0] <= 0 or length <= 0:
        raise ValueError("frame-aligned augmentation input is invalid")
    if source.shape[0] == length:
        return source.copy()
    positions = np.linspace(0.0, source.shape[0] - 1, num=length)
    if nearest:
        indices = np.clip(np.rint(positions).astype(int), 0, source.shape[0] - 1)
        return source[indices].astype(source.dtype, copy=False)
    source_positions = np.arange(source.shape[0], dtype=float)
    columns = source.reshape(source.shape[0], -1)
    resized = np.empty((length, columns.shape[1]), dtype=np.float32)
    for column_index in range(columns.shape[1]):
        column = columns[:, column_index].astype(float)
        valid = np.isfinite(column)
        if not valid.any():
            resized[:, column_index] = np.nan
        elif valid.sum() == 1:
            resized[:, column_index] = column[valid][0]
        else:
            resized[:, column_index] = np.interp(
                positions, source_positions[valid], column[valid],
            )
    return resized.reshape((length,) + source.shape[1:])


def _resample_timing_targets(values, length: int, *, tempo_rate: float):
    """Warp valid onset/offset targets and preserve invalid regions as -1."""
    import numpy as np

    source = np.asarray(values, dtype=np.float32)
    valid_rows = np.isfinite(source).all(axis=1) & (source >= 0).all(axis=1)
    safe = source.copy()
    safe[~valid_rows] = np.nan
    resized = _resample_frame_axis(safe, length)
    resized_validity = _resample_frame_axis(
        valid_rows.astype(np.int8), length, nearest=True,
    ).astype(bool)
    resized[~resized_validity] = -1.0
    valid_values = resized >= 0
    resized[valid_values] /= float(tempo_rate)
    return resized.astype(np.float32, copy=False)


def _normalize_waveform(values):
    import numpy as np

    waveform = np.asarray(values, dtype=np.float32)
    if waveform.ndim != 1 or waveform.size < 320 or not np.isfinite(waveform).all():
        raise ValueError("augmentation produced an invalid waveform")
    peak = float(np.max(np.abs(waveform))) if waveform.size else 0.0
    if peak > 0.999:
        waveform = waveform * (0.999 / peak)
    return waveform.astype(np.float32, copy=False)


def apply_pilot_augmentations(
    row: dict[str, Any],
    *,
    sample_rate: int,
    randomizer: random.Random,
    augmentation_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply reproducible waveform augmentations and aligned target warps.

    The 14-D auxiliary contract is ``F0, 12 chroma bins, voicing``. Tempo
    changes resize every frame-aligned target; pitch changes update F0/chroma.
    Other degradations perturb the audio and the corresponding acoustic view
    without altering lexical/event labels.
    """
    import numpy as np

    if not isinstance(sample_rate, int) or sample_rate < 8_000:
        raise ValueError("sample_rate must be an integer of at least 8000 Hz")
    augmented = {
        key: np.asarray(value).copy() for key, value in row.items()
    }
    if augmented["auxiliary_features"].ndim != 2 or augmented["auxiliary_features"].shape[1] != 14:
        raise ValueError("pilot augmentation requires F0+12 chroma+voicing auxiliary layout")

    if augmentation_names is None:
        if randomizer.random() < 0.20:
            selected: list[str] = []
        else:
            selected = randomizer.sample(
                list(AUGMENTATIONS), randomizer.randint(1, 3),
            )
    else:
        selected = list(dict.fromkeys(str(name) for name in augmentation_names))
    unknown = sorted(set(selected) - set(AUGMENTATIONS))
    if unknown:
        raise ValueError(f"unknown pilot augmentations: {', '.join(unknown)}")

    waveform = _normalize_waveform(augmented["input_values"])
    auxiliary = np.asarray(augmented["auxiliary_features"], dtype=np.float32)
    applied: list[str] = []

    # Temporal transforms must precede stationary degradations so labels and
    # timing remain on the same clock as the augmented waveform.
    if "tempo" in selected:
        import librosa

        tempo_rate = randomizer.uniform(0.90, 1.10)
        waveform = _normalize_waveform(
            librosa.effects.time_stretch(waveform, rate=tempo_rate),
        )
        target_frames = max(1, int(round(auxiliary.shape[0] / tempo_rate)))
        augmented["event_labels"] = _resample_frame_axis(
            augmented["event_labels"], target_frames, nearest=True,
        )
        augmented["boundary_labels"] = _resample_frame_axis(
            augmented["boundary_labels"], target_frames, nearest=True,
        )
        augmented["timing_targets"] = _resample_timing_targets(
            augmented["timing_targets"], target_frames, tempo_rate=tempo_rate,
        )
        auxiliary = _resample_frame_axis(auxiliary, target_frames)
        applied.append("tempo")

    if "pitch" in selected:
        import librosa

        semitones = randomizer.uniform(-2.0, 2.0)
        if abs(semitones) < 0.35:
            semitones = 0.35 if semitones >= 0 else -0.35
        waveform = _normalize_waveform(librosa.effects.pitch_shift(
            waveform, sr=sample_rate, n_steps=semitones,
        ))
        ratio = 2.0 ** (semitones / 12.0)
        voiced_f0 = np.isfinite(auxiliary[:, 0]) & (auxiliary[:, 0] > 0)
        auxiliary[voiced_f0, 0] *= ratio
        auxiliary[:, 1:13] = np.roll(
            auxiliary[:, 1:13], int(round(semitones)), axis=1,
        )
        applied.append("pitch")

    if "reverberation" in selected:
        wet = waveform.copy()
        for reflection in range(1, 5):
            delay = max(1, int(sample_rate * randomizer.uniform(.025, .075) * reflection))
            if delay >= waveform.size:
                continue
            gain = randomizer.uniform(.08, .24) * (.72 ** (reflection - 1))
            wet[delay:] += waveform[:-delay] * gain
        waveform = _normalize_waveform(wet)
        applied.append("reverberation")

    if "crowd" in selected:
        rng = np.random.default_rng(randomizer.getrandbits(64))
        noise = rng.normal(0.0, 1.0, waveform.size).astype(np.float32)
        noise = np.convolve(noise, np.ones(17, dtype=np.float32) / 17.0, mode="same")
        envelope = .45 + .55 * np.sin(
            np.linspace(0.0, randomizer.uniform(3.0, 8.0) * np.pi, waveform.size),
        ) ** 2
        rms = float(np.sqrt(np.mean(waveform ** 2)) + 1e-6)
        waveform = _normalize_waveform(
            waveform + noise * envelope.astype(np.float32) * rms * randomizer.uniform(.08, .20),
        )
        auxiliary[:, 13] = np.clip(auxiliary[:, 13] + randomizer.uniform(.03, .12), 0, 1)
        applied.append("crowd")

    if "backing_vocals" in selected:
        import librosa

        backing_steps = randomizer.choice((-4.0, -3.0, 3.0, 4.0))
        backing = librosa.effects.pitch_shift(
            waveform, sr=sample_rate, n_steps=backing_steps,
        ).astype(np.float32, copy=False)
        delay = max(1, int(sample_rate * randomizer.uniform(.035, .120)))
        delayed = np.zeros_like(waveform)
        delayed[delay:] = backing[:waveform.size - delay]
        waveform = _normalize_waveform(
            waveform + delayed * randomizer.uniform(.08, .18),
        )
        shifted_chroma = np.roll(auxiliary[:, 1:13], int(backing_steps), axis=1)
        auxiliary[:, 1:13] = .90 * auxiliary[:, 1:13] + .10 * shifted_chroma
        applied.append("backing_vocals")

    if "degraded_separation" in selected:
        rng = np.random.default_rng(randomizer.getrandbits(64))
        delay = max(1, int(sample_rate * randomizer.uniform(.008, .030)))
        bleed = np.zeros_like(waveform)
        bleed[delay:] = waveform[:-delay]
        hiss = rng.normal(0.0, 1.0, waveform.size).astype(np.float32)
        hiss = np.convolve(hiss, np.ones(9, dtype=np.float32) / 9.0, mode="same")
        rms = float(np.sqrt(np.mean(waveform ** 2)) + 1e-6)
        waveform = _normalize_waveform(
            waveform * randomizer.uniform(.72, .90)
            + bleed * randomizer.uniform(.06, .14)
            + hiss * rms * randomizer.uniform(.02, .07),
        )
        dropout_count = max(1, auxiliary.shape[0] // 20)
        for _ in range(dropout_count):
            frame = randomizer.randrange(auxiliary.shape[0])
            auxiliary[frame, :] *= randomizer.uniform(.25, .65)
        applied.append("degraded_separation")

    if "clipping" in selected:
        threshold = randomizer.uniform(.35, .75)
        waveform = _normalize_waveform(
            np.clip(waveform, -threshold, threshold) / threshold,
        )
        applied.append("clipping")

    augmented["input_values"] = waveform
    augmented["auxiliary_features"] = auxiliary.astype(np.float32, copy=False)
    frame_count = augmented["auxiliary_features"].shape[0]
    if not (
        augmented["event_labels"].shape == (frame_count,)
        and augmented["boundary_labels"].shape == (frame_count,)
        and augmented["timing_targets"].shape == (frame_count, 2)
    ):
        raise ValueError("augmentation broke frame-target alignment")
    return augmented, applied


def _build_model(local_model_path: Path):
    """Import heavy dependencies lazily and forbid network fallback."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from transformers import AutoModel

    encoder = AutoModel.from_pretrained(str(local_model_path), local_files_only=True)

    class PhoneEventModel(torch.nn.Module):
        def __init__(self, backbone):
            super().__init__()
            self.backbone = backbone
            hidden = int(backbone.config.hidden_size)
            self.dropout = torch.nn.Dropout(0.1)
            self.phone_head = torch.nn.Linear(hidden, len(PHONE_VOCAB))
            self.event_head = torch.nn.Linear(hidden + 14, len(EVENT_CLASSES))
            self.boundary_head = torch.nn.Linear(hidden + 14, len(BOUNDARY_CLASSES))
            self.timing_head = torch.nn.Linear(hidden + 14, 2)

        def forward(self, input_values, auxiliary):
            hidden = self.dropout(self.backbone(input_values=input_values).last_hidden_state)
            if auxiliary.shape[1] != hidden.shape[1]:
                auxiliary = torch.nn.functional.interpolate(
                    auxiliary.transpose(1, 2), size=hidden.shape[1], mode="linear", align_corners=False,
                ).transpose(1, 2)
            combined = torch.cat((hidden, auxiliary), dim=-1)
            return {
                "phone": self.phone_head(hidden),
                "event": self.event_head(combined),
                "boundary": self.boundary_head(combined),
                "timing": self.timing_head(combined),
            }

    return torch, PhoneEventModel(encoder)


def _resample_label(torch, values, frames: int, *, mode: str):
    tensor = torch.as_tensor(values)
    if mode == "nearest":
        return torch.nn.functional.interpolate(
            tensor.float().view(1, 1, -1), size=frames, mode="nearest",
        ).view(-1).long()
    return torch.nn.functional.interpolate(
        tensor.float().transpose(0, 1).unsqueeze(0), size=frames,
        mode="linear", align_corners=False,
    ).squeeze(0).transpose(0, 1)


def train_offline(
    plan: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    output_dir: Path,
    *,
    seed: int,
) -> dict[str, Any]:
    """Run a conservative batch-size-one research loop on materialized NPZ rows."""
    examples = _training_examples(manifest, manifest_path)
    if len(examples) < int(manifest["summary"]["split_counts"]["training"]):
        raise ValueError("not every signed training row has materialized data")
    torch, model = _build_model(Path(plan["base_model_local_path"]))
    if not torch.cuda.is_available():
        raise RuntimeError("v6 training requires CUDA; CPU may only validate/plan")
    device = torch.device("cuda")
    model.to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=float(plan["optimization"]["learning_rate"]),
    )
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    cross_entropy = torch.nn.CrossEntropyLoss()
    randomizer = random.Random(seed)
    epoch_losses: list[float] = []
    augmentation_counts = {name: 0 for name in AUGMENTATIONS}
    clean_examples = 0
    model.train()
    for _epoch in range(int(plan["optimization"]["epochs"])):
        shuffled = list(examples)
        randomizer.shuffle(shuffled)
        running = 0.0
        for path, _case_id, descriptor in shuffled:
            row = _load_example(path)
            expected_samples = int(
                round(float(descriptor["duration_seconds"]) * int(descriptor["sample_rate_hz"]))
            )
            if abs(int(row["input_values"].shape[0]) - expected_samples) > 320:
                raise ValueError(f"{path}: waveform duration does not match signed descriptor")
            row, applied_augmentations = apply_pilot_augmentations(
                row,
                sample_rate=int(descriptor["sample_rate_hz"]),
                randomizer=randomizer,
            )
            if not applied_augmentations:
                clean_examples += 1
            for augmentation in applied_augmentations:
                augmentation_counts[augmentation] += 1
            waveform = torch.as_tensor(row["input_values"], dtype=torch.float32, device=device).unsqueeze(0)
            auxiliary = torch.as_tensor(
                row["auxiliary_features"], dtype=torch.float32, device=device,
            ).unsqueeze(0)
            output = model(waveform, auxiliary)
            frames = output["phone"].shape[1]
            phone_tokens = torch.as_tensor(row["phone_tokens"], dtype=torch.long, device=device)
            if phone_tokens.min() < 1 or phone_tokens.max() >= len(PHONE_VOCAB):
                raise ValueError(f"{path}: phone token outside the declared vocabulary")
            phone_loss = ctc(
                output["phone"].log_softmax(-1).transpose(0, 1),
                phone_tokens,
                torch.tensor([frames], device=device),
                torch.tensor([phone_tokens.numel()], device=device),
            )
            event_labels = _resample_label(torch, row["event_labels"], frames, mode="nearest").to(device)
            boundary_labels = _resample_label(torch, row["boundary_labels"], frames, mode="nearest").to(device)
            timing = _resample_label(torch, row["timing_targets"], frames, mode="linear").to(device)
            if event_labels.min() < 0 or event_labels.max() >= len(EVENT_CLASSES):
                raise ValueError(f"{path}: event label outside the declared vocabulary")
            if boundary_labels.min() < 0 or boundary_labels.max() >= len(BOUNDARY_CLASSES):
                raise ValueError(f"{path}: boundary label outside the declared vocabulary")
            timing_mask = torch.isfinite(timing).all(dim=-1) & (timing >= 0).all(dim=-1)
            timing_loss = (
                torch.nn.functional.smooth_l1_loss(output["timing"][0][timing_mask], timing[timing_mask])
                if timing_mask.any() else output["timing"].sum() * 0.0
            )
            loss = (
                phone_loss
                + cross_entropy(output["event"][0], event_labels)
                + cross_entropy(output["boundary"][0], boundary_labels)
                + timing_loss
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            running += float(loss.detach().cpu())
        epoch_losses.append(running / len(shuffled))

    output_dir.mkdir(parents=True, exist_ok=False)
    checkpoint = output_dir / "research-checkpoint.safetensors"
    from safetensors.torch import save_file
    save_file({key: value.detach().cpu().contiguous() for key, value in model.state_dict().items()}, checkpoint)
    return {
        "schema": TRAINING_REPORT_SCHEMA,
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "architecture": ARCHITECTURE,
        "status": "trained_uncalibrated",
        "offline_only": True,
        "calibrated": False,
        "exported": False,
        "runtime_authorization": False,
        "automatic_apply_allowed": False,
        "dataset_manifest_sha256": artifact_sha256(manifest),
        "base_model_sha256": plan["base_model_sha256"],
        "checkpoint": {
            "path": checkpoint.name,
            "sha256": sha256_file(checkpoint),
            "deployment_export": False,
        },
        "training_cases": len(examples),
        "training_hours": manifest["summary"]["training_hours"],
        "training_events": manifest["summary"]["training_events"],
        "epoch_losses": epoch_losses,
        "augmentation_counts": augmentation_counts,
        "clean_presentations": clean_examples,
        "training_presentations": len(examples) * int(plan["optimization"]["epochs"]),
        "augmentation_operations": sum(augmentation_counts.values()),
        "seed": seed,
    }


def _validate_training_report_for_cpu_export(
    report: Any,
    *,
    report_path: Path,
    base_model_path: Path,
    base_model_sha256: str,
) -> Path:
    """Return the verified checkpoint or refuse before loading ML code."""
    verified, reason = verify_artifact(report, "QUALITY_V6_TRAINING_PUBLIC_KEYS")
    if not verified:
        raise ValueError(f"training report attestation rejected: {reason}")
    if not isinstance(report, dict) or report.get("schema") != TRAINING_REPORT_SCHEMA:
        raise ValueError("training report schema mismatch")
    if report.get("policy_version") != POLICY_VERSION:
        raise ValueError("training report policy mismatch")
    if report.get("status") != "trained_uncalibrated":
        raise ValueError("only a completed uncalibrated training report may be exported")
    for field in ("calibrated", "exported", "runtime_authorization", "automatic_apply_allowed"):
        if report.get(field) is not False:
            raise ValueError(f"training report must keep {field}=false")
    if int(report.get("training_cases") or 0) <= 0:
        raise ValueError("training report contains no completed cases")
    if not base_model_path.is_dir():
        raise ValueError("base model must be an existing local directory")
    actual_base_hash = sha256_directory(base_model_path)
    if (
        actual_base_hash != str(base_model_sha256).lower()
        or actual_base_hash != str(report.get("base_model_sha256") or "").lower()
    ):
        raise ValueError("base model identity does not match the training report")

    checkpoint = report.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("deployment_export") is not False:
        raise ValueError("training checkpoint metadata is invalid")
    relative = Path(str(checkpoint.get("path") or ""))
    if not relative.name or relative.is_absolute():
        raise ValueError("training checkpoint path must be relative")
    root = report_path.resolve().parent
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("training checkpoint escapes its signed report directory")
    if not resolved.is_file() or sha256_file(resolved) != checkpoint.get("sha256"):
        raise ValueError("training checkpoint is missing or hash-mismatched")
    return resolved


def _cpu_export_report(
    training_report: dict[str, Any],
    *,
    training_report_path: Path,
    model_path: Path,
    quantized_linear_modules: int,
    quantized_engine: str,
) -> dict[str, Any]:
    """Describe a real deployment artifact without granting deployment."""
    if quantized_linear_modules <= 0 or not model_path.is_file():
        raise ValueError("quantized CPU artifact was not materialized")
    return {
        "schema": CPU_EXPORT_SCHEMA,
        "policy_version": POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "architecture": ARCHITECTURE,
        "status": "exported_uncalibrated",
        "offline_only": True,
        "calibrated": False,
        "exported": True,
        "runtime_authorization": False,
        "automatic_apply_allowed": False,
        "source_training_report_sha256": sha256_file(training_report_path),
        "source_training_artifact_sha256": artifact_sha256(training_report),
        "dataset_manifest_sha256": training_report.get("dataset_manifest_sha256"),
        "base_model_sha256": training_report.get("base_model_sha256"),
        "quantization": {
            "target": "cpu",
            "method": "pytorch_dynamic_int8_linear",
            "engine": quantized_engine,
            "linear_modules": quantized_linear_modules,
            "validated_after_reload": True,
        },
        "model": {
            "path": model_path.name,
            "sha256": sha256_file(model_path),
            "format": "torchscript",
        },
    }


def export_cpu_quantized(
    *,
    training_report_path: Path,
    base_model_path: Path,
    base_model_sha256: str,
    output_dir: Path,
    private_key: str,
    key_id: str,
) -> dict[str, Any]:
    """Quantize, serialize and reload a CPU artifact; publish atomically."""
    if not private_key or not key_id:
        raise ValueError("CPU export signing keys are required")
    # Validate signing material before loading the base model or creating files.
    sign_artifact({"schema": "quality-v6-cpu-export-signing-preflight"}, private_key, key_id)
    report = _read_json(training_report_path)
    checkpoint_path = _validate_training_report_for_cpu_export(
        report,
        report_path=training_report_path,
        base_model_path=base_model_path,
        base_model_sha256=base_model_sha256,
    )
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite output directory: {output_dir}")
    if not output_dir.parent.is_dir():
        raise ValueError("CPU export output parent must already exist")

    torch, model = _build_model(base_model_path)
    from safetensors.torch import load_file

    state = load_file(str(checkpoint_path), device="cpu")
    model.load_state_dict(state, strict=True)
    model.cpu().eval()
    supported_engines = [
        engine for engine in torch.backends.quantized.supported_engines
        if engine and engine != "none"
    ]
    quantized_engine = next(
        (engine for engine in ("x86", "fbgemm", "qnnpack") if engine in supported_engines),
        None,
    )
    if quantized_engine is None:
        raise RuntimeError("this PyTorch build has no usable CPU quantization engine")
    previous_engine = torch.backends.quantized.engine
    torch.backends.quantized.engine = quantized_engine
    try:
        quantize_dynamic = getattr(
            getattr(torch, "ao", None), "quantization", None,
        )
        quantize_dynamic = getattr(quantize_dynamic, "quantize_dynamic", None) or getattr(
            torch.quantization, "quantize_dynamic", None,
        )
        if quantize_dynamic is None:
            raise RuntimeError("this PyTorch build cannot perform dynamic CPU quantization")
        quantized = quantize_dynamic(
            model, {torch.nn.Linear}, dtype=torch.qint8, inplace=False,
        )
        quantized.eval()
        quantized_linear_modules = sum(
            "quantized.dynamic" in type(module).__module__
            and type(module).__name__ == "Linear"
            for module in quantized.modules()
        )
        if quantized_linear_modules <= 0:
            raise RuntimeError("quantization produced no dynamic int8 Linear modules")

        class CpuExportWrapper(torch.nn.Module):
            def __init__(self, inner):
                super().__init__()
                self.inner = inner

            def forward(self, input_values, auxiliary):
                output = self.inner(input_values, auxiliary)
                return (
                    output["phone"], output["event"],
                    output["boundary"], output["timing"],
                )

        wrapper = CpuExportWrapper(quantized).cpu().eval()
        example_waveform = torch.zeros((1, 16_000), dtype=torch.float32)
        example_auxiliary = torch.zeros((1, 50, 14), dtype=torch.float32)
        with torch.no_grad():
            eager_outputs = wrapper(example_waveform, example_auxiliary)
        if len(eager_outputs) != 4 or any(
            not torch.isfinite(value).all() or value.ndim != 3
            for value in eager_outputs
        ):
            raise RuntimeError("quantized model failed the CPU output contract")
        traced = torch.jit.trace(
            wrapper, (example_waveform, example_auxiliary), strict=True,
        )
        traced = torch.jit.freeze(traced.eval())

        temporary = Path(tempfile.mkdtemp(
            prefix=f".{output_dir.name}.", dir=output_dir.parent,
        ))
        try:
            model_path = temporary / "phone-event-int8-cpu.pt"
            torch.jit.save(traced, str(model_path))
            reloaded = torch.jit.load(str(model_path), map_location="cpu").eval()
            with torch.no_grad():
                reloaded_outputs = reloaded(example_waveform, example_auxiliary)
            if len(reloaded_outputs) != 4 or any(
                not torch.isfinite(value).all()
                or tuple(value.shape) != tuple(expected.shape)
                for value, expected in zip(reloaded_outputs, eager_outputs)
            ):
                raise RuntimeError("serialized CPU model failed reload validation")
            exported = _cpu_export_report(
                report,
                training_report_path=training_report_path,
                model_path=model_path,
                quantized_linear_modules=quantized_linear_modules,
                quantized_engine=quantized_engine,
            )
            signed = sign_artifact(exported, private_key, key_id)
            write_json_exclusive(temporary / "cpu-export-report.json", signed)
            os.rename(temporary, output_dir)
            return signed
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    finally:
        # Some CPU-only builds report the sentinel ``none`` as current while
        # refusing to set it back through the public API. In that case retain
        # the verified usable engine selected above.
        if previous_engine in supported_engines:
            torch.backends.quantized.engine = previous_engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "plan", "smoke", "train"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--manifest", required=True, type=Path)
        if command != "validate":
            sub.add_argument("--base-model-path", required=True, type=Path)
            sub.add_argument("--base-model-sha256", required=True)
            sub.add_argument("--epochs", type=int, default=3)
            sub.add_argument("--learning-rate", type=float, default=1e-5)
        if command == "plan":
            sub.add_argument("--output", required=True, type=Path)
        if command == "train":
            sub.add_argument("--output-dir", required=True, type=Path)
            sub.add_argument("--seed", type=int, default=1701)
    export = subparsers.add_parser("export-cpu")
    export.add_argument("--training-report", required=True, type=Path)
    export.add_argument("--base-model-path", required=True, type=Path)
    export.add_argument("--base-model-sha256", required=True)
    export.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export-cpu":
            private_key = os.environ.get("QUALITY_V6_EXPORT_PRIVATE_KEY", "").strip()
            key_id = os.environ.get("QUALITY_V6_EXPORT_KEY_ID", "").strip()
            exported = export_cpu_quantized(
                training_report_path=args.training_report,
                base_model_path=args.base_model_path,
                base_model_sha256=args.base_model_sha256,
                output_dir=args.output_dir,
                private_key=private_key,
                key_id=key_id,
            )
            print(json.dumps({
                "output": str(args.output_dir),
                "status": exported["status"],
                "calibrated": False,
                "runtime_authorization": False,
            }, indent=2))
            return 0
        manifest = _read_json(args.manifest)
        errors = validate_dataset_manifest(manifest, require_signature=True, require_adequate=True)
        if errors:
            raise ValueError("; ".join(errors))
        if args.command == "validate":
            print("Signed quality-v6 dataset is adequate for offline research.")
            return 0
        plan = create_training_plan(
            manifest,
            manifest_path=args.manifest,
            base_model_path=args.base_model_path,
            base_model_sha256=args.base_model_sha256,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
        )
        if args.command == "plan":
            write_json_exclusive(args.output, plan)
            print(json.dumps({"output": str(args.output), "status": "planned"}, indent=2))
            return 0
        if args.command == "smoke":
            torch, model = _build_model(args.base_model_path)
            if not torch.cuda.is_available():
                raise RuntimeError("smoke requires CUDA")
            parameter_count = sum(parameter.numel() for parameter in model.parameters())
            print(json.dumps({
                "architecture": ARCHITECTURE,
                "parameters": parameter_count,
                "calibrated": False,
                "exported": False,
            }, indent=2))
            return 0
        private_key = os.environ.get("QUALITY_V6_TRAINING_PRIVATE_KEY", "").strip()
        key_id = os.environ.get("QUALITY_V6_TRAINING_KEY_ID", "").strip()
        if not private_key or not key_id:
            raise RuntimeError("training report signing keys are required before training starts")
        if args.output_dir.exists():
            raise FileExistsError(f"refusing to overwrite output directory: {args.output_dir}")
        # Validate key material before spending GPU time or creating a partial
        # output directory.
        sign_artifact({"schema": "quality-v6-signing-preflight"}, private_key, key_id)
        report = train_offline(plan, manifest, args.manifest, args.output_dir, seed=args.seed)
        signed = sign_artifact(report, private_key, key_id)
        write_json_exclusive(args.output_dir / "training-report.json", signed)
        print(json.dumps({
            "output": str(args.output_dir),
            "status": signed["status"],
            "calibrated": False,
            "exported": False,
        }, indent=2))
        return 0
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"Phone/event operation refused (fail-closed): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
