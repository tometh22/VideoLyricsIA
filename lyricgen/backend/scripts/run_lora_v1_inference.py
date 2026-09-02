#!/usr/bin/env python3
"""Run a local LoRA-v1/base Whisper replay and emit evaluator JSONL.

This command is intentionally offline: audio paths must already exist on the
machine and the output contains text hypotheses only.  It is suitable for a
base-vs-adapter replay on the 23-song ``eval_only`` portion of the prepared
manifest; it does not promote an adapter or modify a worker registry.
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

from lora_v1 import BASE_MODEL, read_jsonl  # noqa: E402


def _language_prompt(processor, language: str) -> list[list[int]] | None:
    value = str(language or "").strip().lower()
    if value in {"", "unknown", "auto", "none"}:
        return None
    try:
        return processor.get_decoder_prompt_ids(language=value, task="transcribe")
    except (KeyError, TypeError, ValueError):
        return None


def run(
    rows: list[dict[str, Any]], output: Path, *, model_name: str = BASE_MODEL,
    adapter: Path | None = None, canonical_only: bool = True,
    max_new_tokens: int = 128,
) -> dict[str, Any]:
    try:
        import librosa
        import soundfile as sf
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        if adapter is not None:
            from peft import PeftModel
    except ImportError as exc:
        raise RuntimeError(f"inference dependencies unavailable: {exc}") from exc

    selected = [row for row in rows if not canonical_only or bool(row.get("eval_only"))]
    if not selected:
        raise ValueError("inference manifest contains no selected rows")
    processor = WhisperProcessor.from_pretrained(str(adapter or model_name))
    model = WhisperForConditionalGeneration.from_pretrained(model_name)
    if adapter is not None:
        model = PeftModel.from_pretrained(model, str(adapter))
    device_name = "cuda" if torch.cuda.is_available() else (
        "mps" if torch.backends.mps.is_available() else "cpu"
    )
    device = torch.device(device_name)
    model.to(device)
    model.eval()
    results: list[dict[str, Any]] = []
    with torch.inference_mode():
        for row in sorted(selected, key=lambda item: str(item.get("sample_id") or "")):
            info = sf.info(str(row["audio_path"]))
            start = max(0, round(float(row["start_s"]) * info.samplerate))
            frames = max(1, round((float(row["end_s"]) - float(row["start_s"])) * info.samplerate))
            audio, sample_rate = sf.read(
                str(row["audio_path"]), start=start, frames=frames, always_2d=True,
            )
            mono = audio.mean(axis=1).astype("float32")
            if sample_rate != 16000:
                mono = librosa.resample(mono, orig_sr=sample_rate, target_sr=16000)
            features = processor.feature_extractor(
                mono, sampling_rate=16000, return_tensors="pt",
            ).input_features.to(device)
            kwargs: dict[str, Any] = {"max_new_tokens": max_new_tokens}
            prompt = _language_prompt(processor, str(row.get("language") or ""))
            if prompt:
                kwargs["forced_decoder_ids"] = prompt
            tokens = model.generate(features, **kwargs)
            hypothesis = processor.batch_decode(tokens, skip_special_tokens=True)[0].strip()
            results.append({
                "sample_id": row.get("sample_id"), "song_id": row.get("song_id"),
                "artist": row.get("artist"), "difficulty": row.get("difficulty"),
                "reference": row.get("text") or "", "hypothesis": hypothesis,
                "start_s": row.get("start_s"), "end_s": row.get("end_s"),
            })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )
    return {
        "status": "completed", "model": model_name, "adapter": str(adapter) if adapter else None,
        "device": device_name, "rows": len(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--model", default=BASE_MODEL)
    parser.add_argument("--all-rows", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=128)
    args = parser.parse_args()
    report = run(
        read_jsonl(args.manifest.resolve()), args.output.resolve(),
        model_name=args.model, adapter=args.adapter.resolve() if args.adapter else None,
        canonical_only=not args.all_rows, max_new_tokens=args.max_new_tokens,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
