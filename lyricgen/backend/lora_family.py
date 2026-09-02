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
import re
import threading
import tempfile
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


def _runtime_cache_dir() -> Path:
    configured = os.environ.get(
        "LORA_V1_RUNTIME_CACHE_DIR", "/tmp/genly-lora-v1",
    ).strip()
    return Path(configured or "/tmp/genly-lora-v1")


# API and worker requests can start together on a cold container.  Serializing
# the report/adapter materialization prevents two R2 downloads from deleting
# each other's staging directory; model inference has its own lock below.
_ARTIFACT_MATERIALIZE_LOCK = threading.Lock()


def _r2_client():
    """Build a private R2 client only when the staging artifact bridge is set.

    Runtime deployments may use a Railway volume instead.  The R2 bridge is
    deliberately opt-in, uses the existing private bucket credentials, and is
    never reached while the family flag is disabled.
    """
    endpoint = os.environ.get("R2_ENDPOINT_URL", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    if not endpoint or not access_key or not secret_key:
        return None
    try:
        import boto3
        return boto3.client(
            "s3", endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
    except Exception:
        return None


def _download_r2_object(client, key: str, destination: Path) -> bool:
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not bucket or not key:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part",
        dir=str(destination.parent),
    )
    os.close(fd)
    try:
        client.download_file(bucket, key, temporary)
        os.replace(temporary, destination)
        return True
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def _materialize_report() -> Path | None:
    key = os.environ.get("LORA_V1_EVAL_REPORT_R2_KEY", "").strip()
    if not key:
        return None
    target = _runtime_cache_dir() / "evaluation.json"
    if target.is_file():
        return target
    client = _r2_client()
    if client is None or not _download_r2_object(client, key, target):
        return None
    return target


def _materialize_adapter(expected_sha: str = "") -> Path | None:
    prefix = os.environ.get("LORA_V1_ADAPTER_R2_PREFIX", "").strip()
    if not prefix:
        return None
    root = _runtime_cache_dir() / "adapter"
    marker = root / ".complete"
    model_file = root / "adapter_model.safetensors"
    if marker.is_file() and model_file.is_file():
        if not expected_sha or _sha256(model_file) == expected_sha:
            return root
    client = _r2_client()
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if client is None or not bucket:
        return None
    try:
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = response.get("Contents") or []
    except Exception:
        return None
    if not objects:
        return None
    # Download into a fresh directory so a partial deployment can never be
    # mistaken for a complete adapter after a process restart.
    staging = root.with_name(f".{root.name}.staging")
    try:
        if staging.exists():
            for item in sorted(staging.rglob("*"), reverse=True):
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()
        staging.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        prefix_value = prefix.rstrip("/") + "/"
        for item in objects:
            key = str(item.get("Key") or "")
            relative = key[len(prefix_value):] if key.startswith(prefix_value) else ""
            relative_path = Path(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                continue
            if _download_r2_object(client, key, staging / relative_path):
                downloaded += 1
        staged_model = staging / "adapter_model.safetensors"
        if downloaded == 0 or not staged_model.is_file():
            return None
        if expected_sha and _sha256(staged_model) != expected_sha:
            return None
        if root.exists():
            for item in sorted(root.rglob("*"), reverse=True):
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()
            root.rmdir()
        os.replace(staging, root)
        marker.write_text(expected_sha or _sha256(model_file), encoding="ascii")
        return root
    except Exception:
        return None
    finally:
        if staging.exists() and staging != root:
            for item in sorted(staging.rglob("*"), reverse=True):
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        item.rmdir()
                except OSError:
                    pass
            try:
                staging.rmdir()
            except OSError:
                pass


def load_verified_family(report_path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Return an attested family descriptor or ``None`` (fail closed)."""
    if not _enabled():
        return None
    path = Path(report_path or os.environ.get("LORA_V1_EVAL_REPORT", "").strip())
    if not path.is_file():
        with _ARTIFACT_MATERIALIZE_LOCK:
            path = _materialize_report() or path
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
    expected = report.get("adapter_sha256") or (report.get("candidate_artifact") or {}).get("sha256")
    artifact = (
        os.environ.get("LORA_V1_ADAPTER_PATH", "").strip()
        or report.get("adapter_path")
        or (report.get("candidate_artifact") or {}).get("path")
    )
    if artifact and not Path(artifact).exists():
        with _ARTIFACT_MATERIALIZE_LOCK:
            artifact = str(_materialize_adapter(str(expected or "")) or artifact)
    if not artifact or not Path(artifact).exists():
        return None
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


_ROUTER_TOKEN_RE = re.compile(r"[\wÀ-ÿ]+(?:['’][\wÀ-ÿ]+)?", re.UNICODE)


def _router_tokens(words: list[dict[str, Any]] | None) -> list[str]:
    values: list[str] = []
    for item in words or []:
        if not isinstance(item, dict):
            continue
        text = item.get("word") or item.get("text") or ""
        values.extend(
            token.casefold() for token in _ROUTER_TOKEN_RE.findall(str(text))
        )
    return values


def _router_edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for index, value in enumerate(left, 1):
        current = [index]
        for other_index, other in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (value != other),
            ))
        previous = current
    return previous[-1]


def song_disagreement_score(
    base_words: list[dict[str, Any]] | None,
    lora_words: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return the persisted LoRA↔base difficulty score for one song.

    This is deliberately a paired-model signal, not a quality verdict: it
    never reads a reference lyric and it does not mutate the selected output.
    ``None`` means one of the witnesses was unavailable, so the router must
    abstain rather than treat missing inference as agreement.
    """
    if not isinstance(base_words, list) or not isinstance(lora_words, list):
        return None
    base = _router_tokens(base_words)
    lora = _router_tokens(lora_words)
    if not base or not lora:
        return None
    denominator = max(len(base), len(lora), 1)
    edits = _router_edit_distance(base, lora)
    return {
        "score": round(min(1.0, edits / denominator), 6),
        "base_tokens": len(base),
        "lora_tokens": len(lora),
        "comparison_tokens": denominator,
        "edit_count": edits,
        "source": "paired_asr_disagreement",
        "gold_free": True,
        "abstain_without_both_families": True,
    }


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
