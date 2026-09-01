#!/usr/bin/env python3
"""Train the LoRA-v1 candidate, fail-closed before any model is loaded.

The resulting adapter is a *candidate family*.  This command does not alter
the runtime model registry; evaluation must publish a signed report before a
worker can consume the adapter as an extra consensus witness.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from learning_triggers import catalog_training_authorization  # noqa: E402
from lora_v1 import BASE_MODEL, read_jsonl, sha256_file  # noqa: E402

REQUIRED = {
    "sample_id", "song_id", "audio_path", "start_s", "end_s", "text", "language",
}


def validate_manifest(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if not rows:
        raise ValueError("LoRA manifest is empty")
    for index, row in enumerate(rows):
        missing = REQUIRED - set(row)
        if missing:
            raise ValueError(f"sample {index} is missing {sorted(missing)}")
        audio = Path(str(row["audio_path"]))
        if not audio.is_file():
            raise FileNotFoundError(audio)
        try:
            start, end = float(row["start_s"]), float(row["end_s"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"sample {index} has invalid timing") from exc
        if end <= start or end - start > 30.0:
            raise ValueError(f"sample {index} has invalid interval")
    return rows


def _training_rows(rows: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    # The canonical 23 are never used for training.  They remain available to
    # evaluate the resulting adapter with a separate inference manifest.
    usable = [row for row in rows if not bool(row.get("eval_only"))]
    songs = sorted({str(row["song_id"]) for row in usable})
    validation = set(songs[::5] or songs[-1:])
    train = [row for row in usable if str(row["song_id"]) not in validation]
    valid = [row for row in usable if str(row["song_id"]) in validation]
    artists = sorted({str(row.get("artist") or "unknown") for row in usable})
    held_artist = artists[::5]
    held = [row for row in usable if str(row.get("artist") or "unknown") in held_artist]
    return train, valid, held


def _write_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def train(rows: list[dict[str, Any]], output: Path, *, model_name: str = BASE_MODEL,
          max_steps: int = 100, rank: int = 8, learning_rate: float = 1e-4) -> dict[str, Any]:
    authorization = catalog_training_authorization()
    if not authorization["authorized"]:
        report = {
            "status": "blocked_policy_authorization", "authorization": authorization,
            "base_model": model_name, "training_started": False,
        }
        _write_report(output, report)
        return report
    train_rows, validation_rows, held_rows = _training_rows(rows)
    if not train_rows:
        raise ValueError("no non-eval training rows remain")
    try:
        import librosa
        import soundfile as sf
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import (
            Seq2SeqTrainer, Seq2SeqTrainingArguments, WhisperForConditionalGeneration,
            WhisperProcessor,
        )
    except ImportError as exc:
        report = {
            "status": "blocked_dependencies", "error_type": type(exc).__name__,
            "missing_dependency": str(exc), "base_model": model_name,
            "training_started": False,
        }
        _write_report(output, report)
        return report

    processor = WhisperProcessor.from_pretrained(model_name)
    model = WhisperForConditionalGeneration.from_pretrained(model_name)
    model.config.use_cache = False

    class Dataset:
        def __init__(self, data):
            self.data = data
        def __len__(self):
            return len(self.data)
        def __getitem__(self, index):
            row = self.data[index]
            info = sf.info(row["audio_path"])
            start = max(0, round(float(row["start_s"]) * info.samplerate))
            frames = max(1, round((float(row["end_s"]) - float(row["start_s"])) * info.samplerate))
            audio, sample_rate = sf.read(row["audio_path"], start=start, frames=frames, always_2d=True)
            mono = audio.mean(axis=1).astype("float32")
            if sample_rate != 16000:
                mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=16000)
            features = processor.feature_extractor(mono, sampling_rate=16000).input_features[0]
            labels = processor.tokenizer(str(row["text"])).input_ids
            return {"input_features": features, "labels": labels}

    class Collator:
        def __call__(self, features):
            inputs = processor.feature_extractor.pad(
                [{"input_features": item["input_features"]} for item in features], return_tensors="pt",
            )
            labels = processor.tokenizer.pad(
                [{"input_ids": item["labels"]} for item in features], return_tensors="pt",
            )
            input_ids = labels["input_ids"].masked_fill(labels.attention_mask.ne(1), -100)
            if (input_ids[:, 0] == processor.tokenizer.bos_token_id).all().cpu().item():
                input_ids = input_ids[:, 1:]
            return {**inputs, "labels": input_ids.to(torch.long)}

    model = get_peft_model(model, LoraConfig(
        r=rank, lora_alpha=2 * rank, lora_dropout=0.05,
        target_modules=["q_proj", "v_proj"], bias="none",
    ))
    arguments = Seq2SeqTrainingArguments(
        output_dir=str(output), max_steps=max_steps, per_device_train_batch_size=1,
        gradient_accumulation_steps=1, learning_rate=learning_rate, logging_steps=1,
        save_strategy="no", eval_strategy="no", report_to=[], remove_unused_columns=False,
        fp16=bool(torch.cuda.is_available()),
        use_mps_device=bool(torch.backends.mps.is_available()),
    )
    trainer = Seq2SeqTrainer(
        model=model, args=arguments, train_dataset=Dataset(train_rows), data_collator=Collator(),
    )
    result = trainer.train()
    adapter = output / "adapter"
    model.save_pretrained(adapter)
    processor.save_pretrained(adapter)
    report = {
        "status": "trained_uncalibrated", "schema_version": 1,
        "base_model": model_name, "lora_rank": rank, "max_steps": max_steps,
        "train_samples": len(train_rows), "validation_samples_reserved": len(validation_rows),
        "leave_artist_out_samples_reserved": len(held_rows),
        "train_loss": float(result.training_loss), "adapter_path": str(adapter),
        "adapter_sha256": sha256_file(adapter / "adapter_model.safetensors")
        if (adapter / "adapter_model.safetensors").is_file() else None,
        "training_executor_validated": True, "pipeline_validated": False,
        "evaluation_passed": False,
        "additional_family_only": True, "runtime_replacement_allowed": False,
        "data_egress": False, "authorization": authorization,
    }
    _write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=BASE_MODEL)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    rows = validate_manifest(args.manifest.resolve())
    if args.validate_only:
        train_rows, validation_rows, held_rows = _training_rows(rows)
        report = {
            "status": "validated", "samples": len(rows),
            "train_samples": len(train_rows), "validation_samples": len(validation_rows),
            "leave_artist_out_samples": len(held_rows),
            "canonical_eval_excluded": sum(bool(row.get("eval_only")) for row in rows),
            "base_model": args.model, "authorization": catalog_training_authorization(),
        }
        _write_report(args.output.resolve(), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    report = train(rows, args.output.resolve(), model_name=args.model,
                   max_steps=args.max_steps, rank=args.rank,
                   learning_rate=args.learning_rate)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"trained_uncalibrated"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
