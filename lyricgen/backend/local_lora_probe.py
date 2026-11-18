"""Bounded, offline LoRA feasibility probe; never a model-quality evaluation."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

MODELS = {"target": "openai/whisper-large-v3-turbo", "tiny": "openai/whisper-tiny"}
MIN_AVAILABLE_GIB = {"target": 6.0, "tiny": 1.5}
MAX_PROCESS_GIB = {"target": 6.0, "tiny": 2.0}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_sample(manifest: Path, sample_id: str | None = None) -> dict:
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    eligible = [row for row in rows if row.get("split") == "train"
                and (sample_id is None or row.get("sample_id") == sample_id)]
    if not eligible:
        raise ValueError("no_train_sample")
    row = sorted(eligible, key=lambda item: str(item.get("sample_id", "")))[0]
    if set(row.get("known_registry_roles") or []) & {"eval_holdout", "val"}:
        raise ValueError("protected_song_role")
    if row.get("eval_only") is True:
        raise ValueError("protected_song_role")
    start, end = float(row["start_s"]), float(row["end_s"])
    if not all(math.isfinite(v) for v in (start, end)) or start < 0 or not 0 < end - start <= 30:
        raise ValueError("invalid_interval")
    if not str(row.get("text") or "").strip():
        raise ValueError("empty_text")
    path = Path(row["audio_path"])
    expected = str(row.get("audio_sha256") or "")
    if len(expected) != 64 or sha256_file(path) != expected:
        raise ValueError("audio_hash_mismatch")
    return row


def verify_manifest(manifest: Path) -> str:
    attestation = json.loads((manifest.parent / "manifest.json").read_text())
    expected = attestation.get("outputs", {}).get(manifest.name, {}).get("sha256")
    actual = sha256_file(manifest)
    if not expected or actual != expected:
        raise ValueError("manifest_hash_mismatch")
    return actual


def cached_snapshot(model: str, cache: Path | None = None) -> Path:
    root = (cache or Path.home() / ".cache/huggingface/hub") / ("models--" + model.replace("/", "--"))
    main = root / "refs/main"
    snapshots = [root / "snapshots" / main.read_text().strip()] if main.is_file() else sorted((root / "snapshots").glob("*"))
    complete = [path for path in snapshots if all((path / name).is_file() for name in
                ("model.safetensors", "config.json", "tokenizer.json", "preprocessor_config.json"))]
    if len(complete) != 1:
        raise ValueError("one_complete_cached_snapshot_required")
    return complete[0]


def available_memory_gib(vm_stat_output: str) -> float:
    """Conservative free + file-backed pages, excluding compressed memory."""
    match = re.search(r"page size of (\d+) bytes", vm_stat_output)
    if not match:
        raise ValueError("unrecognized_vm_stat")
    pages = {}
    for line in vm_stat_output.splitlines():
        match_line = re.match(r"([^:]+):\s+(\d+)\.", line)
        if match_line:
            pages[match_line.group(1)] = int(match_line.group(2))
    if "Pages free" not in pages or "File-backed pages" not in pages:
        raise ValueError("missing_memory_counters")
    return (pages["Pages free"] + pages["File-backed pages"]) * int(match.group(1)) / 1024 ** 3


def private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def worker(config: dict) -> dict:
    # Explicit local paths + local_files_only prevent weights/audio egress.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    import resource
    import numpy as np
    import soundfile as sf
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    started = time.monotonic()
    torch.set_num_threads(2)
    if not torch.backends.mps.is_available():
        raise RuntimeError("mps_unavailable")
    torch.manual_seed(20261118)
    torch.mps.set_per_process_memory_fraction(0.12 if config["kind"] == "tiny" else 0.35)
    row = config["sample"]
    # Recheck bytes in the child rather than relying on preflight history.
    if sha256_file(Path(row["audio_path"])) != row["audio_sha256"]:
        raise ValueError("audio_hash_changed_after_preflight")
    info = sf.info(row["audio_path"])
    if float(row["end_s"]) > info.duration:
        raise ValueError("interval_exceeds_audio")
    audio, rate = sf.read(row["audio_path"], start=round(row["start_s"] * info.samplerate),
                          stop=round(row["end_s"] * info.samplerate), always_2d=True, dtype="float32")
    audio = audio.mean(axis=1)
    if rate != 16000:
        import librosa
        audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("invalid_audio_samples")
    processor = WhisperProcessor.from_pretrained(config["snapshot"], local_files_only=True,
                                                language=row.get("language", "es"), task="transcribe")
    dtype = torch.float32 if config["kind"] == "tiny" else torch.float16
    model = WhisperForConditionalGeneration.from_pretrained(
        config["snapshot"], local_files_only=True, dtype=dtype,
        attn_implementation="eager")
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(r=4, lora_alpha=8, lora_dropout=0,
                                           target_modules=["q_proj", "v_proj"], bias="none"))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.to("mps").train()
    features = processor.feature_extractor(audio, sampling_rate=16000, return_tensors="pt").input_features
    labels = processor.tokenizer(row["text"], return_tensors="pt").input_ids
    if labels.shape[1] > model.config.max_target_positions:
        raise ValueError("text_exceeds_model_context")
    if labels[0, 0].item() == model.config.decoder_start_token_id:
        labels = labels[:, 1:]
    trainable = [(name, parameter) for name, parameter in model.named_parameters() if parameter.requires_grad]
    before = {name: parameter.detach().cpu().clone() for name, parameter in trainable}
    optimizer = torch.optim.AdamW([parameter for _, parameter in trainable], lr=1e-4)
    torch.mps.synchronize()
    loaded_at = time.monotonic()
    loss = model(input_features=features.to("mps", dtype=dtype), labels=labels.to("mps")).loss
    if not torch.isfinite(loss).item():
        raise ValueError("nonfinite_loss")
    loss.backward()
    finite_grads = all(parameter.grad is None or torch.isfinite(parameter.grad).all().item()
                       for _, parameter in trainable)
    if not finite_grads:
        raise ValueError("nonfinite_gradients")
    norm = torch.nn.utils.clip_grad_norm_([parameter for _, parameter in trainable], 1.0)
    optimizer.step()
    torch.mps.synchronize()
    if not all(torch.isfinite(parameter).all().item() for _, parameter in trainable):
        raise ValueError("nonfinite_adapter_weights")
    changed = sum(not torch.equal(before[name], parameter.detach().cpu()) for name, parameter in trainable)
    if changed == 0:
        raise ValueError("no_adapter_weights_changed")
    finished_at = time.monotonic()
    adapter = Path(config["output"]) / "adapter"
    adapter.mkdir(mode=0o700, parents=True, exist_ok=False)
    model.save_pretrained(adapter)
    for path in adapter.iterdir():
        if path.is_file():
            path.chmod(0o600)
    return {"status": "smoke_completed", "model": MODELS[config["kind"]],
            "snapshot_revision": Path(config["snapshot"]).name, "kind": config["kind"],
            "target_model_measured": config["kind"] == "target", "device": "mps", "steps": 1,
            "dtype": str(dtype),
            "sample_id": row["sample_id"], "audio_sha256": row["audio_sha256"],
            "clip_duration_s": row["end_s"] - row["start_s"], "loss": float(loss.detach().cpu()),
            "gradient_norm": float(norm.detach().cpu()), "finite_gradients": finite_grads,
            "changed_parameter_tensors": changed,
            "trainable_parameters": sum(parameter.numel() for _, parameter in trainable),
            "load_seconds": loaded_at - started, "step_seconds": finished_at - loaded_at,
            "worker_seconds": finished_at - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "mps_current_allocated_bytes": torch.mps.current_allocated_memory(),
            "mps_driver_allocated_bytes": torch.mps.driver_allocated_memory(),
            "adapter_path": str(adapter), "adapter_sha256": sha256_file(adapter / "adapter_model.safetensors"),
            "quality_evaluated": False, "production_ready": False,
            "scope": "single_train_sample_mechanics_only", "network_model_loading": False}


def execute_probe(config: dict, script: Path, timeout_s: int) -> dict:
    output = Path(config["output"])
    config_path, worker_report = output / "worker-input.json", output / "worker-report.json"
    private_json(config_path, config)
    log_fd = os.open(output / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    started = time.monotonic()
    peak_rss = 0
    try:
        with os.fdopen(log_fd, "w") as log:
            process = subprocess.Popen([sys.executable, str(script), "--worker-config", str(config_path)],
                                       stdout=log, stderr=log)
            stopped = None
            try:
                while process.poll() is None:
                    if time.monotonic() - started > timeout_s:
                        stopped = "blocked_timeout"
                        break
                    rss = subprocess.run(["ps", "-o", "rss=", "-p", str(process.pid)],
                                         capture_output=True, text=True, check=False)
                    if rss.stdout.strip():
                        peak_rss = max(peak_rss, int(rss.stdout.strip()) * 1024)
                    if peak_rss > MAX_PROCESS_GIB[config["kind"]] * 1024 ** 3:
                        stopped = "blocked_process_memory_limit"
                        break
                    time.sleep(0.5)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            result = json.loads(worker_report.read_text()) if worker_report.is_file() else {"status": "worker_failed"}
            if stopped:
                result["status"] = stopped
            result.update(exit_code=process.returncode, wall_seconds=time.monotonic() - started,
                          observed_peak_rss_bytes=peak_rss)
            return result
    finally:
        config_path.unlink(missing_ok=True)
