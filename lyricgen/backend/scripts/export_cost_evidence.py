#!/usr/bin/env python3
"""Validate and sign a reconciled FinOps cost allocation for one output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from evidence_attestation import sign_artifact, write_json_exclusive  # noqa: E402


def _sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def build_cost_evidence(payload: dict[str, Any], *, source_root: Path) -> dict[str, Any]:
    required = (
        "case_id", "system", "release", "config_sha256", "audio_sha256",
        "pricing_version", "billing_period", "invoice_snapshot_id",
        "reconciliation_id", "reconciled_by", "reconciled_at",
    )
    if payload.get("schema") != "finops-reconciliation-input-v1":
        raise ValueError("invalid reconciliation input schema")
    if any(not str(payload.get(field) or "").strip() for field in required):
        raise ValueError("reconciliation identity is incomplete")
    try:
        reconciled_at = datetime.fromisoformat(
            str(payload["reconciled_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("reconciled_at must be timezone-aware ISO-8601") from None
    if reconciled_at.tzinfo is None or reconciled_at > datetime.now(timezone.utc):
        raise ValueError("reconciled_at must be timezone-aware and not in the future")
    billing_period = str(payload["billing_period"])
    if (
        len(billing_period) != 7 or billing_period[4] != "-"
        or not billing_period[:4].isdigit() or not billing_period[5:].isdigit()
        or not 1 <= int(billing_period[5:]) <= 12
    ):
        raise ValueError("billing_period must be YYYY-MM")
    if not _sha(payload["config_sha256"]) or not _sha(payload["audio_sha256"]):
        raise ValueError("config/audio hashes must be SHA-256")
    sources = payload.get("source_artifacts")
    if not isinstance(sources, list) or not sources:
        raise ValueError("hashed billing source artifacts are required")
    source_root = source_root.resolve()
    source_hashes: set[str] = set()
    receipt_index: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("source") or "").strip():
            raise ValueError("invalid billing source descriptor")
        relative = Path(str(source.get("path") or ""))
        resolved = (source_root / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(source_root):
            raise ValueError("billing source path escapes reconciliation directory")
        try:
            raw = resolved.read_bytes()
            source_payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid billing source artifact: {exc}") from None
        actual_hash = hashlib.sha256(raw).hexdigest()
        if source.get("sha256") != actual_hash:
            raise ValueError("billing source artifact hash mismatch")
        if (
            source_payload.get("schema") != "provider-billing-receipts-v1"
            or source_payload.get("currency") != "USD"
            or source_payload.get("billing_period") != payload.get("billing_period")
            or source_payload.get("invoice_snapshot_id")
            != payload.get("invoice_snapshot_id")
        ):
            raise ValueError("billing source identity mismatch")
        receipts = source_payload.get("receipts")
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("billing source has no receipts")
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ValueError("invalid provider receipt")
            key = (
                str(receipt.get("provider") or ""),
                str(receipt.get("request_id") or ""),
            )
            if not all(key) or key in receipt_index:
                raise ValueError("provider receipts must be uniquely identified")
            receipt_index[key] = receipt
        source_hashes.add(actual_hash)
    lines = payload.get("line_items")
    if not isinstance(lines, list) or not lines:
        raise ValueError("line_items are required")
    seen_requests: set[tuple[str, str]] = set()
    total = 0.0
    for item in lines:
        if not isinstance(item, dict):
            raise ValueError("invalid cost line item")
        identity = (str(item.get("provider") or ""), str(item.get("request_id") or ""))
        if not all(identity) or identity in seen_requests:
            raise ValueError("provider request IDs must be present and unique")
        seen_requests.add(identity)
        if item.get("currency") != "USD" or not str(item.get("sku") or "").strip():
            raise ValueError("each line needs SKU and USD currency")
        if not str(item.get("unit_type") or "").strip():
            raise ValueError("each line needs unit_type")
        if item.get("source_receipt_sha256") not in source_hashes:
            raise ValueError("line item is not bound to a billing source artifact")
        receipt = receipt_index.get(identity)
        if receipt is None or receipt.get("sku") != item.get("sku"):
            raise ValueError("line item has no matching provider billing receipt")
        try:
            units = float(item.get("units"))
            cost = float(item.get("cost_usd"))
        except (TypeError, ValueError):
            raise ValueError("units/cost must be numeric") from None
        if units < 0 or cost < 0:
            raise ValueError("units/cost cannot be negative")
        try:
            receipt_units = float(receipt.get("units"))
            receipt_cost = float(receipt.get("cost_usd"))
        except (TypeError, ValueError):
            raise ValueError("provider receipt units/cost must be numeric") from None
        if (
            receipt.get("unit_type") != item.get("unit_type")
            or receipt.get("currency") != item.get("currency")
            or abs(receipt_units - units) > 1e-8
            or abs(receipt_cost - cost) > 1e-8
        ):
            raise ValueError("line item amount does not match provider receipt")
        total += cost
    declared = float(payload.get("total_usd", -1))
    if abs(total - declared) > 1e-8:
        raise ValueError("line item sum does not match total_usd")
    return {
        key: value for key, value in payload.items() if key != "schema"
    } | {
        "schema": "reconciled-cost-ledger-v1",
        "currency": "USD",
        "reconciled": True,
        "cost_complete": True,
        "input_sha256": hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    private_key = os.environ.get("BENCHMARK_FINOPS_PRIVATE_KEY") or ""
    key_id = os.environ.get("BENCHMARK_FINOPS_KEY_ID") or ""
    if not private_key or not key_id:
        parser.error("FinOps private key and key ID are required")
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        artifact = sign_artifact(
            build_cost_evidence(payload, source_root=args.input.parent),
            private_key, key_id,
        )
        write_json_exclusive(args.output, artifact)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
