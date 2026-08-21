#!/usr/bin/env python3
"""Validate or score a fail-closed transcription-quality v5 benchmark.

Examples:
    python scripts/benchmark_v5.py validate --manifest benchmark/v5/manifest.json
    python scripts/benchmark_v5.py score --manifest benchmark/v5/manifest.json
    python scripts/benchmark_v5.py score --manifest manifest.json --output report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from benchmark_v5_lib import (  # noqa: E402
    BenchmarkValidationError,
    format_validation,
    score_manifest,
    validate_manifest,
)
from evidence_attestation import sign_artifact  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "score", "gate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--manifest", required=True, type=Path)
        if command in {"score", "gate"}:
            subparser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    errors = validate_manifest(args.manifest)
    if errors:
        print("Benchmark v5 rejected (fail-closed):", file=sys.stderr)
        print(format_validation(errors), file=sys.stderr)
        return 1
    if args.command == "validate":
        print("Benchmark v5 valid")
        return 0
    try:
        report = score_manifest(args.manifest)
    except BenchmarkValidationError as exc:
        print("Benchmark v5 rejected (fail-closed):", file=sys.stderr)
        print(format_validation(exc.errors), file=sys.stderr)
        return 1
    if args.command == "gate" and report["release_gate"]["decision"] == "GO":
        private_key = os.environ.get("BENCHMARK_RELEASE_PRIVATE_KEY") or ""
        key_id = os.environ.get("BENCHMARK_RELEASE_KEY_ID") or ""
        if not private_key or not key_id:
            print(
                "Benchmark v5 GO report requires release signing key/key ID.",
                file=sys.stderr,
            )
            return 1
        report = sign_artifact(report, private_key, key_id)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(payload, end="")
    if args.command == "gate" and report["release_gate"]["decision"] != "GO":
        print(
            "Benchmark v5 release gate: NO-GO (see release_gate.blockers)",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
