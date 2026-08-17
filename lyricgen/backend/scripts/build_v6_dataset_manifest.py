#!/usr/bin/env python3
"""Build a signed, offline-only quality-v6 dataset manifest.

The input is a JSON object containing ``dataset_id``, ``contract`` and
``entries``.  Strict mode refuses incomplete rights, leaking identities or an
undersized corpus.  ``--draft`` may publish an explicitly unsigned draft for
curation, but that draft is rejected by every calibration/training consumer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from evidence_attestation import sign_artifact, write_json_exclusive  # noqa: E402
from quality_v6_calibration import (  # noqa: E402
    DATASET_SCHEMA,
    POLICY_VERSION,
    dataset_adequacy,
    summarize_dataset,
    validate_dataset_manifest,
)


def _read_json(path: Path) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid inventory {path}: {exc}") from exc


def build_manifest(inventory: Any, *, draft: bool) -> dict[str, Any]:
    if not isinstance(inventory, dict):
        raise ValueError("inventory root must be an object")
    entries = inventory.get("entries")
    if not isinstance(entries, list):
        raise ValueError("inventory.entries must be a list")
    allowed = {"dataset_id", "contract", "entries"}
    unknown = sorted(set(inventory) - allowed)
    if unknown:
        raise ValueError(f"unknown inventory fields: {', '.join(unknown)}")
    manifest = {
        "schema": DATASET_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": "draft" if draft else "ready",
        "dataset_id": inventory.get("dataset_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contract": inventory.get("contract"),
        "summary": summarize_dataset(entries),
        "entries": entries,
    }
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--draft", action="store_true",
        help="write an unsigned curation draft; it cannot train or calibrate",
    )
    parser.add_argument(
        "--private-key-env", default="QUALITY_V6_DATASET_PRIVATE_KEY",
        help="environment variable containing a base64 Ed25519 private key",
    )
    parser.add_argument(
        "--key-id-env", default="QUALITY_V6_DATASET_KEY_ID",
        help="environment variable containing the trusted signing key ID",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_manifest(_read_json(args.inventory), draft=args.draft)
    except ValueError as exc:
        print(f"Dataset v6 rejected: {exc}", file=sys.stderr)
        return 1

    errors = validate_dataset_manifest(
        manifest, require_signature=False, require_adequate=not args.draft,
    )
    adequacy = dataset_adequacy(manifest["summary"])
    if errors and not args.draft:
        print("Dataset v6 rejected (fail-closed):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if args.draft:
        manifest["draft_validation"] = {
            "adequate": adequacy["passed"],
            "blockers": sorted(set(errors + adequacy["blockers"])),
        }
    else:
        private_key = os.environ.get(args.private_key_env, "").strip()
        key_id = os.environ.get(args.key_id_env, "").strip()
        if not private_key or not key_id:
            print(
                f"Dataset v6 signing requires {args.private_key_env} and {args.key_id_env}.",
                file=sys.stderr,
            )
            return 1
        manifest = sign_artifact(manifest, private_key, key_id)

    try:
        write_json_exclusive(args.output, manifest)
    except FileExistsError:
        print(f"Refusing to overwrite immutable manifest: {args.output}", file=sys.stderr)
        return 1
    print(json.dumps({
        "output": str(args.output),
        "status": manifest["status"],
        "adequate": adequacy["passed"],
        "signed": "attestation" in manifest,
        "summary": manifest["summary"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
