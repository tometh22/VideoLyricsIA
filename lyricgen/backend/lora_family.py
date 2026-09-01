"""Runtime registration for a verified LoRA hypothesis family.

The runtime intentionally accepts metadata and optional words only after a
research evaluation report marks the adapter as an additional family.  It
cannot replace the base Whisper family and it never creates text by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any


def _enabled() -> bool:
    return os.environ.get("LORA_V1_FAMILY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_family(report_path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Return an attested family descriptor or ``None`` (fail closed)."""
    if not _enabled():
        return None
    path = Path(report_path or os.environ.get("LORA_V1_EVAL_REPORT", "").strip())
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gate = (
        report.get("replacement_gate")
        or report.get("runtime_policy")
        or report.get("gate")
        or {}
    )
    if gate.get("additional_family_only") is not True:
        return None
    if gate.get("runtime_replacement_allowed") is True:
        return None
    # A one-step smoke adapter, or an adapter that merely has files on disk,
    # is never eligible.  Promotion requires a completed held-out inference
    # replay and an explicit evaluation gate; this is what prevents the LoRA
    # family from entering consensus before it has earned that role.
    if report.get("pipeline_validated") is not True:
        return None
    evaluation_passed = report.get("evaluation_passed") is True or (
        isinstance(report.get("gate"), dict) and report["gate"].get("passed") is True
    )
    if not evaluation_passed:
        return None
    # Deployments may mount the attested adapter at a different path than the
    # research pod.  The report remains the source of truth for the expected
    # SHA-256; the explicit path override only changes where that bytestring
    # is read from.  Never accept a path from a job payload.
    artifact = (
        os.environ.get("LORA_V1_ADAPTER_PATH", "").strip()
        or report.get("adapter_path")
        or (report.get("candidate_artifact") or {}).get("path")
    )
    if not artifact or not Path(artifact).exists():
        return None
    expected = report.get("adapter_sha256") or (report.get("candidate_artifact") or {}).get("sha256")
    if expected:
        candidate = Path(artifact)
        if candidate.is_file():
            actual = _sha256(candidate)
        elif (candidate / "adapter_model.safetensors").is_file():
            actual = _sha256(candidate / "adapter_model.safetensors")
        else:
            return None
        if actual != str(expected):
            return None
    return {
        "name": "lora_v1",
        "family": "openai_whisper_large_v3_turbo_lora_v1",
        "role": "additional_consensus_family",
        "artifact": str(artifact),
        "evaluation_report": str(path),
        "replacement_allowed": False,
        "model": str(report.get("base_model") or "openai/whisper-large-v3-turbo"),
        "adapter_sha256": str(expected or ""),
    }


def attach_hypothesis(result: dict[str, Any], words: list[dict[str, Any]], *, report_path: str | None = None) -> bool:
    """Attach LoRA words to a result only when the family is verified."""
    family = load_verified_family(report_path)
    if family is None or not isinstance(result, dict) or not isinstance(words, list):
        return False
    result["_lora_asr_words"] = [dict(item) for item in words if isinstance(item, dict)]
    result["_lora_asr_family"] = family["family"]
    result["_lora_family_role"] = family["role"]
    return True


# Model loading is intentionally process-local.  The production pipeline may
# handle many jobs concurrently, but loading a 1.5B Whisper checkpoint for
# every request would exhaust VRAM and turn an optional witness into an outage.
_MODEL_LOCK = threading.Lock()
_MODEL_INFERENCE_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, tuple[Any, Any, Any, str, Any]] = {}


def _runtime_limit_seconds() -> float:
    try:
        return max(15.0, min(900.0, float(os.environ.get(
            "LORA_V1_MAX_AUDIO_SECONDS", "420",
        ))))
    except (TypeError, ValueError):
        return 420.0


def _load_runtime_model(family: dict[str, Any]):
    """Load processor/model once, keeping optional dependencies out of import.

    This is the only place where ``transformers``/``peft`` are imported.  A
    normal API or worker image without the optional runtime simply records a
    bounded ``missing_dependency`` decline and continues with base ASR.
    """
    artifact = str(family["artifact"])
    model_name = str(family.get("model") or "openai/whisper-large-v3-turbo")
    cache_key = f"{model_name}|{artifact}"
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            import torch
            from peft import PeftModel
            from transformers import (
                WhisperForConditionalGeneration, WhisperProcessor, pipeline,
            )
        except ImportError as exc:
            raise RuntimeError("missing_dependency") from exc
        processor = WhisperProcessor.from_pretrained(model_name)
        base = WhisperForConditionalGeneration.from_pretrained(model_name)
        model = PeftModel.from_pretrained(base, artifact)
        device = "cuda" if torch.cuda.is_available() else (
            "mps" if getattr(torch.backends, "mps", None)
            and torch.backends.mps.is_available() else "cpu"
        )
        model.to(device)
        model.eval()
        asr = pipeline(
            "automatic-speech-recognition", model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            device=0 if device == "cuda" else -1,
            chunk_length_s=30,
        )
        loaded = (processor, model, torch, device, asr)
        _MODEL_CACHE[cache_key] = loaded
        return loaded


def transcribe_words(
    audio_path: str | os.PathLike[str],
    *,
    language: str = "",
    report_path: str | os.PathLike[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate an optional LoRA witness with bounded, auditable behavior.

    The witness is never selected or written into ``segments`` here.  It is a
    list of approximate word spans consumed by ``targeted_consensus`` as an
    additional family.  Word-level timestamps are requested from Whisper; if
    a backend does not return them, the chunk is conservatively omitted rather
    than inventing timing.  Any decline is returned as telemetry and must not
    fail the transcription job.
    """
    started = time.monotonic()
    family = load_verified_family(report_path)
    stats: dict[str, Any] = {
        "enabled": bool(family), "family": family.get("family") if family else None,
        "status": "declined", "words": 0,
    }
    if family is None:
        stats["reason"] = "not_attested_or_disabled"
        return [], stats
    try:
        import librosa
        import soundfile as sf
        _processor, _model, _torch, device, asr = _load_runtime_model(family)
        info = sf.info(str(audio_path))
        duration = max(0.0, float(info.duration or 0.0))
        if duration <= 0.0:
            stats["reason"] = "duration_unavailable"
            return [], stats
        if duration > _runtime_limit_seconds():
            stats["reason"] = "duration_budget"
            stats["duration_s"] = round(duration, 3)
            return [], stats
        audio, sample_rate = sf.read(str(audio_path), always_2d=True)
        mono = audio.mean(axis=1).astype("float32")
        if sample_rate != 16000:
            mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=16000)
        generate_kwargs: dict[str, Any] = {"task": "transcribe"}
        value = str(language or "").strip().lower()
        if value and value not in {"auto", "unknown", "none"}:
            generate_kwargs["language"] = value
        words: list[dict[str, Any]] = []
        chunk_s = 30.0
        # A process-local HF pipeline is not thread-safe on CUDA/MPS.  Keep
        # optional witness inference serialized; base transcription remains
        # unaffected and separate worker processes can still run in parallel.
        with _MODEL_INFERENCE_LOCK, _torch.inference_mode():
            for offset in range(0, len(mono), int(chunk_s * 16000)):
                chunk = mono[offset:offset + int(chunk_s * 16000)]
                if len(chunk) < 1600:
                    continue
                decoded = asr(
                    chunk, return_timestamps="word",
                    generate_kwargs=generate_kwargs,
                )
                for item in (decoded.get("chunks") or []) if isinstance(decoded, dict) else []:
                    if not isinstance(item, dict):
                        continue
                    token = str(item.get("text") or "").strip()
                    timestamp = item.get("timestamp")
                    if not token or not isinstance(timestamp, (tuple, list)) or len(timestamp) != 2:
                        continue
                    try:
                        start = float(timestamp[0]) + offset / 16000.0
                        end = float(timestamp[1]) + offset / 16000.0
                    except (TypeError, ValueError):
                        continue
                    if end > start >= 0.0:
                        words.append({"word": token, "start": round(start, 3), "end": round(end, 3)})
        stats.update({
            "status": "ok", "words": len(words), "device": device,
            "duration_s": round(duration, 3),
        })
        return words, stats
    except Exception as exc:  # optional witness must never break base ASR
        stats["reason"] = type(exc).__name__
        return [], stats
    finally:
        stats["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
