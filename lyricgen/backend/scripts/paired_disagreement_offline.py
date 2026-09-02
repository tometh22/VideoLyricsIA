#!/usr/bin/env python3
"""Offline paired LoRA↔base disagreement for a folder of songs.

Reproduces the router pilot's signal (scripts/pilot_lora_disagreement_router.py):
whisper-large-v3-turbo WITHOUT the adapter versus the SAME model WITH the
attested LoRA adapter, decoded on identical 30 s chunks, token edit distance
with case/punctuation ignored, aggregated per song as edits / comparison
tokens. This is the score the pilot thresholds (AUC 0.971) were derived on.

The runtime ``difficulty_router`` persisted by the worker compares the
WhisperX primary family against LoRA instead, which adds cross-family noise
and is not on the same scale; use this script until the worker computes the
paired signal itself.

Requires the attested family to be loadable through lora_family (set
LORA_V1_FAMILY_ENABLED=1 and either the R2 bridge or LORA_V1_EVAL_REPORT +
LORA_V1_ADAPTER_PATH). Never uses reference lyrics.

Example::

    python scripts/paired_disagreement_offline.py --folder /path/to/wavs \
        --manifest .context/universal-batch-manifest.json --output paired.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND_ROOT)

_TOKEN_RE = re.compile(r"[\wÀ-ÿ]+(?:['’][\wÀ-ÿ]+)?", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [t.casefold() for t in _TOKEN_RE.findall(str(text or ""))]


def edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for i, value in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (value != other)))
        previous = current
    return previous[-1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chunks(audio_path: Path, chunk_s: float = 30.0):
    import librosa
    import soundfile as sf
    audio, sr = sf.read(str(audio_path), always_2d=True)
    mono = audio.mean(axis=1).astype("float32")
    if sr != 16000:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=16000)
    step = int(chunk_s * 16000)
    for offset in range(0, len(mono), step):
        chunk = mono[offset:offset + step]
        if len(chunk) >= 1600:
            yield chunk


def _decode(asr, chunk, generate_kwargs) -> str:
    out = asr(chunk, generate_kwargs=generate_kwargs)
    return str(out.get("text") or "") if isinstance(out, dict) else ""


def song_pair(asr, model, torch, audio_path: Path, language: str) -> dict:
    generate_kwargs = {"task": "transcribe"}
    if language and language not in {"auto", "unknown", "none"}:
        generate_kwargs["language"] = language
    windows = edits = comparison = 0
    with torch.inference_mode():
        for chunk in _chunks(audio_path):
            with_lora = _decode(asr, chunk, generate_kwargs)
            with model.disable_adapter():
                base = _decode(asr, chunk, generate_kwargs)
            b, l = tokens(base), tokens(with_lora)
            windows += 1
            edits += edit_distance(b, l)
            comparison += max(len(b), len(l), 1)
    return {
        "windows": windows, "edits": edits, "comparison_tokens": comparison,
        "disagreement": round(edits / max(comparison, 1), 6),
        "source": "paired_turbo_base_vs_lora_offline",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--manifest", help="universal_batch manifest to map sha256 -> job_id")
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", default="es")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    from lora_family import _load_runtime_model, load_verified_family
    family = load_verified_family()
    if family is None:
        print("LoRA family not attested/loadable (check LORA_V1_* env)", file=sys.stderr)
        return 2
    _processor, model, torch, device, asr = _load_runtime_model(family)
    print(f"model on {device}; adapter {family['adapter_sha256'][:12]}", flush=True)

    sha_to_job: dict[str, dict] = {}
    if args.manifest:
        for entry in json.load(open(args.manifest)).get("entries", []):
            sha_to_job[entry.get("sha256", "")] = {"job_id": entry.get("job_id"), "filename": entry.get("filename")}

    results: dict[str, dict] = {}
    if Path(args.output).exists():
        results = json.load(open(args.output))
    wavs = sorted(p for p in Path(args.folder).iterdir() if p.suffix.lower() in {".wav", ".mp3", ".flac"})
    if args.limit:
        wavs = wavs[:args.limit]
    for index, path in enumerate(wavs, 1):
        sha = _sha256(path)
        if sha in results:
            continue
        started = time.monotonic()
        pair = song_pair(asr, model, torch, path, args.language)
        pair.update({"filename": path.name, "sha256": sha, **sha_to_job.get(sha, {}),
                     "elapsed_s": round(time.monotonic() - started, 1)})
        results[sha] = pair
        json.dump(results, open(args.output, "w"), ensure_ascii=False, indent=2)
        print(f"[{index}/{len(wavs)}] {path.name[:44]:44} disagreement={pair['disagreement']:.4f} windows={pair['windows']} {pair['elapsed_s']}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
