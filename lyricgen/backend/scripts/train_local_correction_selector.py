#!/usr/bin/env python3
"""Train CPU-only local review-risk model or rank a frozen snapshot. No API calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_correction_selector import load_artifact, rank_segments, train
from local_lora_probe import verify_manifest


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    training = modes.add_parser("train")
    training.add_argument("--corpus", type=Path, required=True)
    training.add_argument("--output", type=Path, required=True)
    inference = modes.add_parser("rank")
    inference.add_argument("--artifact", type=Path, required=True)
    inference.add_argument("--snapshot", type=Path, required=True)
    inference.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "train":
        verify_manifest(args.corpus)
        raw = args.corpus.read_bytes()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        artifact, report = train(rows, corpus_sha256=hashlib.sha256(raw).hexdigest())
        private_json(args.output / "model.json", artifact)
        private_json(args.output / "evaluation.json", report)
        print(json.dumps({"status": "trained_experimental", "model_id": artifact["model_id"],
                          "output": str(args.output), "evaluation_claim": report["evaluation_claim"]}))
    else:
        artifact = load_artifact(args.artifact)
        snapshot = json.loads(args.snapshot.read_text())
        result = {"job_id": snapshot.get("job_id"), "model_id": artifact["model_id"],
                  "automatic_apply_allowed": False,
                  "ranking": rank_segments(artifact, snapshot["segments"])}
        private_json(args.output, result)
        print(json.dumps({"output": str(args.output), "ranked_lines": len(result["ranking"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
