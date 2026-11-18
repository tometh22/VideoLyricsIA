#!/usr/bin/env python3
"""Preflight by default; --execute performs one guarded, offline MPS LoRA step."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_lora_probe import (MODELS, MIN_AVAILABLE_GIB, available_memory_gib, cached_snapshot,
                              execute_probe, private_json, select_sample, verify_manifest, worker)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", choices=MODELS, default="target")
    parser.add_argument("--sample-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--worker-config", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_config:
        config = json.loads(args.worker_config.read_text())
        try:
            result = worker(config)
        except Exception as exc:
            result = {"status": "worker_failed", "error_type": type(exc).__name__,
                      "quality_evaluated": False, "production_ready": False}
            # Private traceback useful for environment failures; never print the sample.
            import traceback
            traceback.print_exc()
        private_json(Path(config["output"]) / "worker-report.json", result)
        return 0 if result["status"] == "smoke_completed" else 1
    if not args.manifest or not args.output or not 1 <= args.timeout_s <= 600:
        parser.error("--manifest, --output and timeout 1..600 required")
    if args.output.exists():
        parser.error("output must be a new directory")
    args.output.mkdir(mode=0o700, parents=True)
    report = {"model": MODELS[args.model], "kind": args.model, "training_started": False,
              "quality_evaluated": False, "production_ready": False,
              "target_model_measured": False, "network_model_loading": False}
    try:
        manifest_hash = verify_manifest(args.manifest)
        row = select_sample(args.manifest, args.sample_id)
        snapshot = cached_snapshot(MODELS[args.model])
        memory = available_memory_gib(subprocess.check_output(["vm_stat"], text=True))
        report.update(manifest_sha256=manifest_hash, sample_id=row["sample_id"],
                      audio_sha256=row["audio_sha256"], snapshot_revision=snapshot.name,
                      conservative_available_gib=memory, minimum_available_gib=MIN_AVAILABLE_GIB[args.model])
        if memory < MIN_AVAILABLE_GIB[args.model]:
            report["status"] = "blocked_available_memory"
        elif not args.execute:
            report["status"] = "preflight_ready"
        else:
            report["training_started"] = True
            report.update(execute_probe({"sample": row, "snapshot": str(snapshot), "kind": args.model,
                                         "output": str(args.output.resolve())}, Path(__file__).resolve(), args.timeout_s))
    except Exception as exc:
        report.update(status="blocked_preflight", error_type=type(exc).__name__, reason=str(exc))
    private_json(args.output / "report.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"preflight_ready", "smoke_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
