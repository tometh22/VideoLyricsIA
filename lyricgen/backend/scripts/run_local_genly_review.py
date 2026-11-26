#!/usr/bin/env python3
"""Rank and review local audio using cached weights; no hosted inference."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_correction_selector import load_artifact, rank_segments
from local_genly_review import LocalWhisper, normalize_snapshot, review
from local_lora_probe import available_memory_gib, cached_snapshot, private_json
from reviewer_shadow_audio import file_sha, probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--model", choices=["tiny", "target"], default="tiny")
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
    parser.add_argument("--max-lines", type=int, default=6)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    song = normalize_snapshot(json.loads(args.snapshot.read_text()))
    if file_sha(args.audio) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    model_name = "openai/whisper-tiny" if args.model == "tiny" else "openai/whisper-large-v3-turbo"
    checkpoint = cached_snapshot(model_name)
    ranking = rank_segments(load_artifact(args.selector), song["segments"])
    available = available_memory_gib(subprocess.check_output(["vm_stat"], text=True))
    required = 1.5 if args.model == "tiny" else 6.0
    summary = {"status": "preflight", "model": model_name, "duration": probe(args.audio),
               "lines": len(song["segments"]), "available_memory_gib": round(available, 3),
               "required_memory_gib": required, "memory_ok": available >= required,
               "hosted_provider_calls": 0, "publication_enabled": False}
    if not args.execute:
        print(json.dumps(summary))
        return 0
    if available < required:
        print(json.dumps({**summary, "status": "blocked_memory"}))
        return 2
    if (args.output / "report.json").exists():
        raise ValueError("report_exists_choose_new_output_directory")
    import torch
    torch.set_num_threads(2)
    if args.device == "mps":
        torch.mps.set_per_process_memory_fraction(.12 if args.model == "tiny" else .35)
    listener = LocalWhisper(checkpoint, adapter_path=args.adapter, device=args.device)
    result = review(song, args.audio, args.output, listener=listener,
                    ranking=ranking, max_lines=args.max_lines)
    result["base_model"] = model_name
    result["quality_validated"] = False
    result["adapter_loaded"] = bool(args.adapter)
    private_json(args.output / "report.json", result)
    print(json.dumps({"status": result["status"], "coverage": result["coverage"],
                      "model_id": result["model_id"], "adapter_loaded": bool(args.adapter),
                      "proposals": result["telemetry"]["proposal_count"],
                      "elapsed_seconds": result["elapsed_seconds"],
                      "report": str(args.output / "report.json"),
                      "quality_validated": False, "hosted_provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
