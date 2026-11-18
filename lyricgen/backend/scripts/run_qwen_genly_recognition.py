#!/usr/bin/env python3
"""Blind full-song Qwen recognition in isolated MLX; CTC runs in a later process."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genly_model_runtime import cached_stem_for_audio, plan_windows, recognize_song
from local_correction_selector import load_artifact, rank_segments
from local_genly_review import normalize_snapshot
from local_lora_probe import available_memory_gib, private_json
from local_qwen_mlx import LocalQwenMLX
from reviewer_shadow_audio import file_sha, probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--model-name", default="mlx-community/Qwen3-ASR-0.6B-4bit")
    parser.add_argument("--memory-limit-gib", type=float, default=2.0)
    parser.add_argument("--stem-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-windows", type=int, default=128)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not 1.5 <= args.memory_limit_gib <= 3:
        raise ValueError("invalid_memory_limit")
    song = normalize_snapshot(json.loads(args.snapshot.read_text()))
    if file_sha(args.audio) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = probe(args.audio)
    windows = plan_windows(duration, max_windows=args.max_windows)
    available = available_memory_gib(subprocess.check_output(["vm_stat"], text=True))
    preflight = {"status": "preflight", "model": args.model_name,
                 "duration_seconds": duration, "windows": len(windows),
                 "available_memory_gib": round(available, 3), "required_memory_gib": 1.8,
                 "hosted_provider_calls": 0}
    if not args.execute:
        print(json.dumps(preflight))
        return 0
    if available < 1.8:
        print(json.dumps({**preflight, "status": "blocked_memory"}))
        return 2
    if (args.output / "recognition.json").exists():
        raise ValueError("recognition_exists_choose_new_output_directory")
    ranking = rank_segments(load_artifact(args.selector), song["segments"])
    stem = cached_stem_for_audio(args.stem_manifest, song["audio_sha256"])
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    started = time.monotonic()
    listener = LocalQwenMLX(args.checkpoint, memory_limit_bytes=round(args.memory_limit_gib * 1024**3))
    recognition = recognize_song(song, args.audio, args.output / "clips", listener=listener,
                                 ranking=ranking, stem=stem, max_windows=args.max_windows)
    import mlx.core as mx
    import resource
    recognition.update(base_model=args.model_name, adapter_loaded=False,
                       stem_status="verified_cached_stem" if stem else "unavailable_original_only",
                       mlx_peak_memory_gib=round(mx.get_peak_memory() / 1024**3, 3),
                       process_peak_rss_gib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3, 3),
                       elapsed_seconds=round(time.monotonic() - started, 3))
    private_json(args.output / "recognition.json", recognition)
    print(json.dumps({"status": "recognition_finished", "model_id": recognition["model_id"],
                      "observations": len(recognition["observations"]),
                      "failed": sum(o["status"] != "recognized" for o in recognition["observations"]),
                      "elapsed_seconds": recognition["elapsed_seconds"],
                      "artifact": str(args.output / "recognition.json"), "hosted_provider_calls": 0}))
    return 0


if __name__ == "__main__":
    main()
