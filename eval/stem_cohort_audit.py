#!/usr/bin/env python3
"""Audit RunPod stems against local reruns of the same five source audios.

The audit deliberately separates model identity from numerical identity:
Demucs can produce slightly different samples on CUDA and MPS while still
preserving the exact timeline.  Cross-correlation therefore measures the
temporal offset and a correlation coefficient records residual similarity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import soundfile as sf
from scipy.signal import correlate, correlation_lags, resample_poly

from eval.canonical import read_json, write_json


MODEL = "mdx_extra"
RUNPOD_DEMUCS_VERSION = "4.0.1"
RUNPOD_ORIGIN = "runpod_demucs_exact_model_name"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select_pairs(manifest: dict[str, Any], count: int = 5) -> list[dict[str, Any]]:
    """Choose the shortest RunPod cases deterministically before scoring."""
    rows = [row for row in manifest["cases"] if row.get("origin") == RUNPOD_ORIGIN]
    return sorted(rows, key=lambda row: (float(row["duration_s"]), str(row["song_id"])))[:count]


def _mono_resampled(path: Path, target_sr: int = 8000) -> tuple[np.ndarray, int, dict[str, Any]]:
    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = np.asarray(data.mean(axis=1), dtype=np.float64)
    if sample_rate != target_sr:
        divisor = int(np.gcd(sample_rate, target_sr))
        mono = resample_poly(mono, target_sr // divisor, sample_rate // divisor)
    info = sf.info(str(path))
    return mono, target_sr, {
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "frames": int(info.frames),
        "duration_s": float(info.frames / info.samplerate),
        "sha256": _sha256(path),
    }


def _high_energy_window(signal: np.ndarray, sample_rate: int, seconds: float = 90.0) -> tuple[np.ndarray, int]:
    length = min(len(signal), max(1, int(seconds * sample_rate)))
    if length >= len(signal):
        return signal, 0
    block = sample_rate
    blocks = len(signal) // block
    energy = np.asarray([
        float(np.mean(np.square(signal[index * block:(index + 1) * block])))
        for index in range(blocks)
    ])
    width = max(1, length // block)
    rolling = np.convolve(energy, np.ones(width), mode="valid")
    start = int(np.argmax(rolling)) * block
    return signal[start:start + length], start


def cross_correlation_offset(
    runpod: np.ndarray, local: np.ndarray, sample_rate: int, *, max_offset_s: float = 1.0,
) -> dict[str, float]:
    """Return local-minus-RunPod lag; positive means the local stem is later."""
    size = min(len(runpod), len(local))
    runpod_window, start = _high_energy_window(runpod[:size], sample_rate)
    local_window = local[start:start + len(runpod_window)]
    runpod_window = runpod_window - float(np.mean(runpod_window))
    local_window = local_window - float(np.mean(local_window))
    values = correlate(local_window, runpod_window, mode="full", method="fft")
    lags = correlation_lags(len(local_window), len(runpod_window), mode="full")
    limit = int(round(max_offset_s * sample_rate))
    allowed = np.abs(lags) <= limit
    chosen = int(np.argmax(values[allowed]))
    lag = int(lags[allowed][chosen])
    if lag >= 0:
        left, right = local_window[lag:], runpod_window[:len(local_window) - lag]
    else:
        left, right = local_window[:len(local_window) + lag], runpod_window[-lag:]
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    coefficient = float(np.dot(left, right) / denominator) if denominator else 0.0
    return {
        "local_minus_runpod_samples_at_8khz": lag,
        "local_minus_runpod_ms": 1000.0 * lag / sample_rate,
        "aligned_correlation": coefficient,
        "analysis_window_start_s": start / sample_rate,
        "analysis_window_duration_s": len(runpod_window) / sample_rate,
    }


def _local_stem(output: Path, song_id: str) -> Path:
    return output / song_id / MODEL / "audio" / "vocals.wav"


def separate_pairs(
    golden: Path, output: Path, pairs: Sequence[dict[str, Any]], device: str,
) -> None:
    for position, row in enumerate(pairs, 1):
        song_id = str(row["song_id"])
        destination = _local_stem(output, song_id)
        if destination.is_file():
            continue
        source = golden / song_id / "audio.wav"
        print(f"stem-audit {position}/{len(pairs)} {song_id} device={device}", flush=True)
        subprocess.run([
            sys.executable, "-m", "demucs.separate", "-n", MODEL,
            "--two-stems", "vocals", "-d", device, "-o", str(output / song_id),
            str(source),
        ], check=True)


def run(
    golden: Path, stems: Path, local_output: Path, report_path: Path,
    *, count: int = 5, device: str = "mps", skip_separation: bool = False,
) -> dict[str, Any]:
    import demucs
    import torch

    manifest = read_json(stems / "manifest.json")
    pairs = select_pairs(manifest, count)
    if not skip_separation:
        separate_pairs(golden, local_output, pairs, device)
    results = []
    for row in pairs:
        song_id = str(row["song_id"])
        runpod_path = stems / song_id / "vocals.wav"
        local_path = _local_stem(local_output, song_id)
        if not local_path.is_file():
            raise FileNotFoundError(f"missing local control stem: {local_path}")
        runpod_audio, rate, runpod_info = _mono_resampled(runpod_path)
        local_audio, local_rate, local_info = _mono_resampled(local_path)
        if rate != local_rate:
            raise AssertionError("comparison resampling rate mismatch")
        results.append({
            "song_id": song_id,
            "source_audio_sha256": row.get("source_audio_sha256"),
            "runpod": runpod_info,
            "local": local_info,
            "duration_delta_ms_local_minus_runpod": 1000.0 * (
                local_info["duration_s"] - runpod_info["duration_s"]
            ),
            "offset": cross_correlation_offset(runpod_audio, local_audio, rate),
        })
    all_stems = []
    for row in manifest["cases"]:
        path = stems / str(row["song_id"]) / "vocals.wav"
        info = sf.info(str(path))
        all_stems.append({
            "song_id": row["song_id"], "origin": row.get("origin"),
            "model": row.get("model"), "device": row.get("device"),
            "sample_rate": int(info.samplerate), "channels": int(info.channels),
        })
    offsets = [abs(float(row["offset"]["local_minus_runpod_ms"])) for row in results]
    report = {
        "schema_version": 1,
        "experiment": "runpod-vs-local-same-audio-stem-audit",
        "selection": "five shortest RunPod cases, fixed before offset inspection",
        "model_identity": {
            "model": MODEL,
            "runpod_demucs_version": RUNPOD_DEMUCS_VERSION,
            "runpod_version_evidence": "eval.runpod_stems runner pins demucs==4.0.1",
            "local_demucs_version": str(getattr(demucs, "__version__", "unknown")),
            "local_torch_version": str(torch.__version__),
            "runpod_device": "cuda",
            "local_device": device,
            "byte_identity_expected": False,
        },
        "all_cached_stems": {
            "count": len(all_stems),
            "sample_rates": sorted({row["sample_rate"] for row in all_stems}),
            "channels": sorted({row["channels"] for row in all_stems}),
            "rows": all_stems,
        },
        "pairs": results,
        "conclusion": {
            "pairs": len(results),
            "maximum_absolute_offset_ms": max(offsets) if offsets else None,
            "timeline_equivalent_within_1ms": bool(offsets and max(offsets) <= 1.0),
        },
    }
    write_json(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--local-output", type=Path, default=Path(".context/stem-cohort-audit/local-reruns"))
    parser.add_argument("--report", type=Path, default=Path("eval/runs/stem_cohort_audit/report.json"))
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--device", default="mps", choices=("cpu", "mps", "cuda"))
    parser.add_argument("--skip-separation", action="store_true")
    args = parser.parse_args()
    report = run(
        args.golden.resolve(), args.stems.resolve(), args.local_output.resolve(),
        args.report.resolve(), count=args.count, device=args.device,
        skip_separation=args.skip_separation,
    )
    print(json.dumps(report["conclusion"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
