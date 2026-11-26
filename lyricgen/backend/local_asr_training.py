"""Offline decoder-LoRA training with generated validation and bounded MPS use.

The historical development corpus is not a fresh acoustic gold test. This
trainer records that limitation and never promotes an artifact to production.
"""
from __future__ import annotations

from collections import defaultdict
import gc
import json
import math
import os
from pathlib import Path
import random
import resource
import time

from local_lora_probe import private_json, sha256_file, verify_manifest
from reviewer_shadow import tokens

DECODER_TARGETS = r"model\.decoder\.layers\.\d+\.(self_attn|encoder_attn)\.(q_proj|v_proj)"


def edit_distance(reference, candidate):
    previous = list(range(len(candidate) + 1))
    for i, expected in enumerate(reference, 1):
        current = [i]
        for j, actual in enumerate(candidate, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def generated_metrics(rows, hypotheses):
    if len(rows) != len(hypotheses):
        raise ValueError("prediction_coverage_mismatch")
    songs = defaultdict(lambda: {"errors": 0, "words": 0, "samples": 0})
    for row, hypothesis in zip(rows, hypotheses):
        expected, actual = tokens(row["text"]), tokens(hypothesis)
        if not expected:
            raise ValueError("empty_lexical_reference")
        values = songs[row["job_id"]]
        values["errors"] += edit_distance(expected, actual)
        values["words"] += len(expected)
        values["samples"] += 1
    for values in songs.values():
        values["wer"] = values["errors"] / values["words"]
    errors, words = sum(s["errors"] for s in songs.values()), sum(s["words"] for s in songs.values())
    return {"samples": len(rows), "songs": len(songs), "errors": errors, "words": words,
            "micro_wer": errors / words if words else None,
            "song_macro_wer": sum(s["wer"] for s in songs.values()) / len(songs) if songs else None,
            "by_song": dict(songs), "comparator": "approved_editorial_line_text",
            "timing_evaluated": False, "fresh_holdout": False}


def balanced_subset(rows, maximum, seed):
    """Deterministic round robin prevents a large song dominating a subset."""
    if maximum < 1:
        raise ValueError("positive_subset_size_required")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["job_id"]].append(row)
    generator = random.Random(seed)
    jobs = sorted(grouped)
    generator.shuffle(jobs)
    for key in sorted(grouped):
        values = grouped[key]
        values.sort(key=lambda row: row["sample_id"])
        generator.shuffle(values)
    result = []
    while len(result) < maximum and any(grouped.values()):
        for job in jobs:
            if grouped[job] and len(result) < maximum:
                result.append(grouped[job].pop())
    return result


def load_partitions(manifest: Path, *, validation_samples=24, replay_samples=24):
    manifest_hash = verify_manifest(manifest)
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    seen_ids, identities, audio_hashes = set(), defaultdict(dict), {}
    partitions = {key: [] for key in ("train", "calibration", "development_replay")}
    for row in rows:
        split = row.get("split")
        if split == "excluded":
            continue
        if split not in partitions:
            raise ValueError("unknown_split")
        if not row.get("sample_id") or row["sample_id"] in seen_ids:
            raise ValueError("duplicate_or_missing_sample_id")
        seen_ids.add(row["sample_id"])
        for key in ("job_id", "audio_sha256", "split_group", "artist"):
            value = str(row.get(key) or "").strip().casefold()
            if not value:
                raise ValueError("missing_identity:" + key)
            if value in identities[key] and identities[key][value] != split:
                raise ValueError("partition_leakage:" + key)
            identities[key][value] = split
        if split == "train" and (set(row.get("known_registry_roles") or []) & {"val", "eval_holdout"}
                                 or row.get("eval_only") is True):
            raise ValueError("protected_training_role")
        start, end = float(row["start_s"]), float(row["end_s"])
        if not all(math.isfinite(value) for value in (start, end)) or not 0 <= start < end or end - start > 30:
            raise ValueError("invalid_crop")
        if not tokens(row.get("text", "")):
            raise ValueError("empty_lexical_reference")
        path = Path(row["audio_path"]).resolve()
        if path not in audio_hashes:
            audio_hashes[path] = sha256_file(path)
        if audio_hashes[path] != row["audio_sha256"]:
            raise ValueError("audio_hash_mismatch")
        partitions[split].append(row)
    if len({row["job_id"] for row in partitions["train"]}) < 2:
        raise ValueError("insufficient_train_songs")
    if len({row["split_group"] for row in partitions["calibration"]}) < 2:
        raise ValueError("insufficient_validation_groups")
    return {"manifest_sha256": manifest_hash,
            "train": balanced_subset(partitions["train"], len(partitions["train"]), 20261118),
            "validation": balanced_subset(partitions["calibration"], validation_samples, 20261119),
            "replay": balanced_subset(partitions["development_replay"], replay_samples, 20261120)
            if partitions["development_replay"] else [],
            "available_samples": {key: len(value) for key, value in partitions.items()},
            "verified_audio_files": len(audio_hashes)}


def read_audio(row):
    import numpy as np
    import soundfile as sf
    info = sf.info(row["audio_path"])
    if row["end_s"] > info.duration:
        raise ValueError("crop_exceeds_audio")
    audio, rate = sf.read(row["audio_path"], start=round(row["start_s"] * info.samplerate),
                          stop=round(row["end_s"] * info.samplerate), always_2d=True, dtype="float32")
    audio = audio.mean(axis=1)
    if rate != 16000:
        import librosa
        audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("invalid_audio_samples")
    return audio


def train_local(config):
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    import numpy as np
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    started = time.monotonic()
    output, snapshot = Path(config["output"]), Path(config["snapshot"])
    partitions = load_partitions(Path(config["manifest"]), validation_samples=config["validation_samples"],
                                 replay_samples=config["replay_samples"])
    train_rows = partitions["train"][:config["max_steps"]]
    if config["max_steps"] < 1 or not train_rows:
        raise ValueError("positive_training_steps_required")
    if not torch.backends.mps.is_available():
        raise RuntimeError("mps_unavailable")
    torch.set_num_threads(2)
    torch.manual_seed(20261118)
    torch.mps.set_per_process_memory_fraction(.16)
    processor = WhisperProcessor.from_pretrained(str(snapshot), local_files_only=True, language="es", task="transcribe")
    base = WhisperForConditionalGeneration.from_pretrained(str(snapshot), local_files_only=True,
                                                          dtype=torch.float32, attn_implementation="eager")
    base.config.use_cache = False
    model = get_peft_model(base, LoraConfig(r=8, lora_alpha=16, lora_dropout=.05,
                                           target_modules=DECODER_TARGETS, bias="none"))
    model.to("mps")
    trainable = [(name, value) for name, value in model.named_parameters() if value.requires_grad]
    if not trainable or any("decoder" not in name for name, _ in trainable):
        raise ValueError("decoder_only_training_required")
    before = {name: value.detach().cpu().clone() for name, value in trainable}
    optimizer = torch.optim.AdamW([value for _, value in trainable], lr=1e-4, weight_decay=.01)
    encoder = model.get_base_model().model.encoder
    history = []
    peak_driver = 0
    steps_done = 0

    def monitor():
        nonlocal peak_driver
        torch.mps.synchronize()
        peak_driver = max(peak_driver, torch.mps.driver_allocated_memory())
        if peak_driver > 2.3 * 1024 ** 3:
            raise RuntimeError("mps_memory_budget_exceeded")
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 2.0 * 1024 ** 3:
            raise RuntimeError("rss_memory_budget_exceeded")
        if time.monotonic() - started > config["timeout_s"]:
            raise RuntimeError("training_time_budget_exceeded")

    def progress(event, **values):
        item = {"event": event, "elapsed_s": round(time.monotonic() - started, 2), **values}
        print(json.dumps(item), flush=True)
        private_json(output / "progress.json", item)

    def inputs(row):
        return processor(read_audio(row), sampling_rate=16000, return_tensors="pt", return_attention_mask=True)

    def evaluate(rows, name, baseline=False):
        model.eval()
        hypotheses = []
        context = model.disable_adapter() if baseline else __import__("contextlib").nullcontext()
        with context, torch.inference_mode():
            for index, row in enumerate(rows):
                batch = inputs(row)
                generated = model.generate(batch.input_features.to("mps"),
                                           attention_mask=batch.attention_mask.to("mps"),
                                           language="es", task="transcribe", do_sample=False,
                                           max_new_tokens=192, use_cache=True)
                hypotheses.append(processor.batch_decode(generated, skip_special_tokens=True)[0].strip())
                del batch, generated
                monitor()
                if (index + 1) % 8 == 0:
                    progress("evaluation", partition=name, completed=index + 1, total=len(rows))
        metrics = generated_metrics(rows, hypotheses)
        private_json(output / (name + "-predictions.json"), {"metrics": metrics,
                     "rows": [{"sample_id": row["sample_id"], "reference": row["text"], "hypothesis": text}
                              for row, text in zip(rows, hypotheses)]})
        gc.collect()
        torch.mps.empty_cache()
        return metrics

    progress("loaded", model=config["model_id"], trainable_parameters=sum(value.numel() for _, value in trainable),
             train_samples=len(train_rows), validation_samples=len(partitions["validation"]),
             replay_samples=len(partitions["replay"]))
    baseline_validation = evaluate(partitions["validation"], "baseline-validation", baseline=True)
    baseline_replay = evaluate(partitions["replay"], "baseline-replay", baseline=True) if partitions["replay"] else None
    best_metrics, best_step, best_state = None, None, None
    losses = []
    training_started = time.monotonic()
    for step, row in enumerate(train_rows, 1):
        model.train()
        encoder.eval()
        optimizer.zero_grad(set_to_none=True)
        batch = inputs(row)
        labels = processor.tokenizer(row["text"], return_tensors="pt").input_ids
        if labels.shape[1] > model.config.max_target_positions:
            raise ValueError("label_exceeds_decoder_context")
        if labels[0, 0].item() == model.config.decoder_start_token_id:
            labels = labels[:, 1:]
        with torch.no_grad():
            encoded = encoder(batch.input_features.to("mps")).last_hidden_state
        loss = model(encoder_outputs=(encoded,), labels=labels.to("mps"), use_cache=False).loss
        if not torch.isfinite(loss).item():
            raise ValueError("nonfinite_training_loss")
        loss.backward()
        if any(value.grad is not None and not torch.isfinite(value.grad).all().item() for _, value in trainable):
            raise ValueError("nonfinite_training_gradients")
        torch.nn.utils.clip_grad_norm_([value for _, value in trainable], 1.)
        optimizer.step()
        steps_done = step
        losses.append(float(loss.detach().cpu()))
        del loss, batch, labels, encoded
        monitor()
        if step == 5 or step % 25 == 0:
            progress("training", steps=step, total_steps=len(train_rows), mean_loss_last25=float(np.mean(losses[-25:])),
                     rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     mps_driver_bytes=torch.mps.driver_allocated_memory(),
                     mean_step_seconds=(time.monotonic() - training_started) / step)
        if step % config["evaluate_every"] == 0 or step == len(train_rows):
            metrics = evaluate(partitions["validation"], f"validation-step-{step}")
            history.append({"step": step, "validation": metrics})
            if best_metrics is None or metrics["song_macro_wer"] < best_metrics["song_macro_wer"]:
                best_metrics, best_step = metrics, step
                best_state = {name: value.detach().cpu().clone() for name, value in trainable}
            progress("checkpoint_evaluated", steps=step, validation_macro_wer=metrics["song_macro_wer"],
                     baseline_macro_wer=baseline_validation["song_macro_wer"], selected_step=best_step)
    with torch.no_grad():
        for name, value in trainable:
            value.copy_(best_state[name].to("mps"))
    if any(not torch.isfinite(value).all().item() for _, value in trainable):
        raise ValueError("nonfinite_selected_adapter")
    changed = sum(not torch.equal(before[name], value.detach().cpu()) for name, value in trainable)
    if changed == 0:
        raise ValueError("no_learned_parameters")
    selected_replay = evaluate(partitions["replay"], "selected-replay") if partitions["replay"] else None
    adapter = output / "adapter"
    adapter.mkdir(mode=0o700, exist_ok=False)
    model.save_pretrained(adapter)
    for path in adapter.iterdir():
        if path.is_file():
            path.chmod(0o600)
    report = {"schema": "genly-local-asr-training-v1", "status": "trained_local_candidate",
              "model": config["model_id"], "snapshot_revision": snapshot.name,
              "base_weights_sha256": sha256_file(snapshot / "model.safetensors"),
              "base_config_sha256": sha256_file(snapshot / "config.json"),
              "adapter_sha256": sha256_file(adapter / "adapter_model.safetensors"),
              "adapter_config_sha256": sha256_file(adapter / "adapter_config.json"),
              "adapter_path": str(adapter.resolve()), "manifest_sha256": partitions["manifest_sha256"],
              "finite_gradients": True, "changed_parameter_tensors": changed,
              "trainable_parameters": sum(value.numel() for _, value in trainable),
              "device": "mps", "dtype": "float32", "encoder_frozen": True, "lora_rank": 8,
              "learning_rate": 1e-4, "steps": steps_done, "selected_step": best_step,
              "selection_metric": "validation_song_macro_wer", "selection_includes_baseline": False,
              "train_samples": len(train_rows), "train_songs": len({r["job_id"] for r in train_rows}),
              "selected_train_samples": best_step,
              "selected_train_songs": len({r["job_id"] for r in train_rows[:best_step]}),
              "available_samples": partitions["available_samples"],
              "validation_sample_ids": [r["sample_id"] for r in partitions["validation"]],
              "replay_sample_ids": [r["sample_id"] for r in partitions["replay"]],
              "train_sample_ids": [r["sample_id"] for r in train_rows],
              "selected_train_sample_ids": [r["sample_id"] for r in train_rows[:best_step]],
              "baseline_validation": baseline_validation, "selected_validation": best_metrics,
              "baseline_replay": baseline_replay, "selected_replay": selected_replay,
              "checkpoint_history": history,
              "improves_validation": best_metrics["song_macro_wer"] < baseline_validation["song_macro_wer"],
              "evaluation_claim": "historical_development_only", "fresh_holdout": False,
              "pipeline_quality_validated": False, "production_ready": False,
              "network_model_loading": False, "paid_provider_calls": 0,
              "elapsed_seconds": time.monotonic() - started,
              "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "peak_observed_mps_driver_bytes": peak_driver}
    private_json(output / "report.json", report)
    progress("complete", steps=steps_done, selected_step=best_step,
             baseline_validation_macro_wer=baseline_validation["song_macro_wer"],
             selected_validation_macro_wer=best_metrics["song_macro_wer"])
    return report
