#!/usr/bin/env python3
"""Train an offline Genly ASR candidate; explicit --execute, no publication."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_asr_training import load_partitions, train_local
from local_lora_probe import available_memory_gib, cached_snapshot, private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=["openai/whisper-base", "openai/whisper-tiny"], default="openai/whisper-base")
    parser.add_argument("--max-steps", type=int, default=612)
    parser.add_argument("--evaluate-every", type=int, default=153)
    parser.add_argument("--validation-samples", type=int, default=24)
    parser.add_argument("--replay-samples", type=int, default=24)
    parser.add_argument("--timeout-s", type=int, default=2700)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.max_steps <= 5000 and 1 <= args.evaluate_every <= args.max_steps
            and 1 <= args.validation_samples <= 1000 and 1 <= args.replay_samples <= 1000
            and 1 <= args.timeout_s <= 10800):
        parser.error("invalid training bounds")
    snapshot = cached_snapshot(args.model)
    partitions = load_partitions(args.manifest, validation_samples=args.validation_samples, replay_samples=args.replay_samples)
    available = available_memory_gib(subprocess.check_output(["vm_stat"], text=True))
    summary = {"status": "preflight_ready" if available >= 1.8 else "blocked_available_memory",
               "model": args.model, "snapshot_revision": snapshot.name, "available_gib": available,
               "required_gib": 1.8, "train_samples": min(args.max_steps, len(partitions["train"])),
               "validation_samples": len(partitions["validation"]), "replay_samples": len(partitions["replay"]),
               "available_samples": partitions["available_samples"], "evaluation_claim": "development_only"}
    print(json.dumps(summary), flush=True)
    if not args.execute or summary["status"] != "preflight_ready":
        return 0 if summary["status"] == "preflight_ready" else 2
    if args.output.exists():
        parser.error("output must be a new private directory")
    args.output.mkdir(mode=0o700, parents=True)
    config = {"manifest": str(args.manifest.resolve()), "output": str(args.output.resolve()),
              "snapshot": str(snapshot), "model_id": args.model, "max_steps": args.max_steps,
              "evaluate_every": args.evaluate_every, "validation_samples": args.validation_samples,
              "replay_samples": args.replay_samples, "timeout_s": args.timeout_s}
    private_json(args.output / "configuration.json", config)
    try:
        train_local(config)
    except Exception as exc:
        private_json(args.output / "failure.json", {"status": "training_failed", "error_type": type(exc).__name__,
                                                     "reason": str(exc), "production_ready": False})
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
