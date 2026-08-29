#!/usr/bin/env python3
"""Run the policy-gated Whisper LoRA executor on a licensed JSONL manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


REQUIRED = {"sample_id", "song_id", "audio_path", "start_s", "end_s", "text", "language"}


def validate_manifest(path: Path, policy: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("LoRA manifest is empty")
    for index, row in enumerate(rows):
        missing = REQUIRED - set(row)
        if missing:
            raise ValueError(f"sample {index} is missing {sorted(missing)}")
        audio_path = Path(row["audio_path"])
        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        if float(row["end_s"]) <= float(row["start_s"]):
            raise ValueError(f"sample {index} has an invalid interval")
        if float(row["end_s"]) - float(row["start_s"]) > 30.0:
            raise ValueError(f"sample {index} exceeds 30 seconds")
        if policy == "research" and not str(row.get("license") or "").strip():
            raise ValueError(f"research sample {index} lacks a license identifier")
    if policy == "umg" and os.environ.get("ALLOW_UMG_TRAINING") != "1":
        raise RuntimeError("UMG training is policy-blocked; set ALLOW_UMG_TRAINING=1 only after recorded authorization")
    return rows


class _AudioDataset:
    def __init__(self, rows: list[dict[str, Any]], processor: Any):
        self.rows, self.processor = rows, processor

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        import librosa
        import soundfile as sf

        row = self.rows[index]
        info = sf.info(row["audio_path"])
        start_frame = max(0, round(float(row["start_s"]) * info.samplerate))
        frames = max(1, round((float(row["end_s"]) - float(row["start_s"])) * info.samplerate))
        audio, sample_rate = sf.read(row["audio_path"], start=start_frame, frames=frames, always_2d=True)
        mono = audio.mean(axis=1).astype("float32")
        if sample_rate != 16000:
            mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=16000)
        features = self.processor.feature_extractor(mono, sampling_rate=16000).input_features[0]
        labels = self.processor.tokenizer(str(row["text"])).input_ids
        return {"input_features": features, "labels": labels}


class _Collator:
    def __init__(self, processor: Any):
        self.processor = processor

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        import torch

        inputs = self.processor.feature_extractor.pad(
            [{"input_features": item["input_features"]} for item in features], return_tensors="pt",
        )
        labels = self.processor.tokenizer.pad(
            [{"input_ids": item["labels"]} for item in features], return_tensors="pt",
        )
        input_ids = labels["input_ids"].masked_fill(labels.attention_mask.ne(1), -100)
        if (input_ids[:, 0] == self.processor.tokenizer.bos_token_id).all().cpu().item():
            input_ids = input_ids[:, 1:]
        return {**inputs, "labels": input_ids.to(torch.long)}


def train(
    rows: list[dict[str, Any]], output: Path, model_name: str, max_steps: int,
    rank: int, learning_rate: float,
) -> dict[str, Any]:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        Seq2SeqTrainer, Seq2SeqTrainingArguments, WhisperForConditionalGeneration,
        WhisperProcessor,
    )

    processor = WhisperProcessor.from_pretrained(model_name)
    model = WhisperForConditionalGeneration.from_pretrained(model_name)
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        r=rank, lora_alpha=2 * rank, lora_dropout=0.05,
        target_modules=["q_proj", "v_proj"], bias="none",
    ))
    song_ids = sorted({str(row["song_id"]) for row in rows})
    validation_songs = set(song_ids[::5] or song_ids[-1:])
    train_rows = [row for row in rows if row["song_id"] not in validation_songs]
    validation_rows = [row for row in rows if row["song_id"] in validation_songs]
    if not train_rows:
        train_rows, validation_rows = rows, rows[:1]
    arguments = Seq2SeqTrainingArguments(
        output_dir=str(output), max_steps=max_steps,
        per_device_train_batch_size=1, gradient_accumulation_steps=1,
        learning_rate=learning_rate, logging_steps=1, save_strategy="no",
        eval_strategy="no", report_to=[], remove_unused_columns=False,
        fp16=bool(torch.cuda.is_available()), use_mps_device=bool(torch.backends.mps.is_available()),
    )
    trainer = Seq2SeqTrainer(
        model=model, args=arguments,
        train_dataset=_AudioDataset(train_rows, processor),
        data_collator=_Collator(processor),
    )
    result = trainer.train()
    adapter = output / "adapter"
    model.save_pretrained(adapter)
    processor.save_pretrained(adapter)
    report = {
        "schema_version": 1,
        "base_model": model_name,
        "lora_rank": rank,
        "max_steps": max_steps,
        "train_samples": len(train_rows),
        "validation_samples_reserved": len(validation_rows),
        "songs": len(song_ids),
        "train_loss": result.training_loss,
        "training_executor_validated": True,
        "pipeline_validated": False,
        "remaining_pipeline_gate": "run held-out song inference through eval.score and publish song-bootstrap CI",
        "adapter_path": str(adapter),
        "data_egress": False,
        "note": "Metric improvement requires a full research run followed by eval.score; a smoke step validates mechanics only.",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-policy", choices=["research", "umg"], required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/runs/lora_executor"))
    parser.add_argument("--model", default="openai/whisper-large-v3-turbo")
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    rows = validate_manifest(args.manifest.resolve(), args.dataset_policy)
    if args.validate_only:
        print(json.dumps({"valid": True, "samples": len(rows), "policy": args.dataset_policy}, indent=2))
        return 0
    print(json.dumps(train(rows, args.output.resolve(), args.model, args.max_steps, args.rank, args.learning_rate), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
