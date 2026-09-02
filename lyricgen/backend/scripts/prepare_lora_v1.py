#!/usr/bin/env python3
"""Prepare the authorized LoRA-v1 catalogue manifest.

This is a local, SELECT/read-only materialization step.  It never uploads
audio and never treats the canonical 23-song evaluation labels as training
labels: they stay in the manifest only to make leakage audits reproducible.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from lora_v1 import prepare_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--historical-pairs", type=Path, action="append", default=[])
    parser.add_argument("--expected-samples", type=int, default=498)
    parser.add_argument("--authorization-reference", default=None)
    args = parser.parse_args()
    report = prepare_manifest(
        args.golden.resolve(), args.output.resolve(),
        historical_paths=[path.resolve() for path in args.historical_pairs],
        expected_samples=args.expected_samples,
        authorization_reference=args.authorization_reference,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
