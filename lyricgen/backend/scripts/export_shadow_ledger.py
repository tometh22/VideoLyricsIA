#!/usr/bin/env python3
"""Export backend shadow decisions into a signed benchmark-v5 ledger.

Human correctness labels are optional while collecting data, but the release
gate rejects every unreviewed would-approve row. The exporter never reads or
writes lyric text/audio; it joins privacy-safe decision IDs only.
"""
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

from evidence_attestation import verify_artifact, write_json_exclusive  # noqa: E402

try:  # running as a file vs importing as ``scripts.export_shadow_ledger``
    from benchmark_v5_lib import shadow_ledger_attestation  # type: ignore  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - import shape only
    from scripts.benchmark_v5_lib import shadow_ledger_attestation  # noqa: E402
from quality_shadow import (  # noqa: E402
    EVENT_NAME, decision_id as canonical_decision_id, decision_identity,
)


def _is_hash(value: str, length: int) -> bool:
    return len(value) == length and all(char in "0123456789abcdef" for char in value)


def _reviews(path: Path | None) -> tuple[dict[str, dict], str | None]:
    if path is None:
        return {}, None
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    verified, reason = verify_artifact(payload, "BENCHMARK_REVIEW_PUBLIC_KEYS")
    if not verified:
        raise ValueError(f"review file is not authenticated: {reason}")
    if payload.get("schema") != "authenticated-shadow-reviews-v1" or payload.get(
        "source"
    ) != "authenticated_review_service_v1":
        raise ValueError("review file must come from authenticated_review_service_v1")
    rows = payload.get("reviews") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("review file must contain a reviews list")
    result: dict[str, dict] = {}
    receipt_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("decision_id"):
            raise ValueError("every review needs decision_id")
        if not isinstance(row.get("correct"), bool) or not isinstance(
            row.get("catastrophic"), bool,
        ):
            raise ValueError("every review needs boolean correct/catastrophic")
        if not str(row.get("reviewer_id") or "").strip():
            raise ValueError("every review needs reviewer_id")
        if not _is_hash(str(row.get("snapshot_sha256") or ""), 64):
            raise ValueError("every review needs the exact snapshot_sha256")
        try:
            reviewed_at = datetime.fromisoformat(
                str(row.get("reviewed_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError("every review needs timezone-aware reviewed_at") from None
        if reviewed_at.tzinfo is None or reviewed_at > datetime.now(timezone.utc):
            raise ValueError("reviewed_at must be timezone-aware and not in the future")
        receipt_id = str(row.get("review_receipt_id") or "")
        if not receipt_id or receipt_id in receipt_ids:
            raise ValueError("review_receipt_id must be present and unique")
        receipt_ids.add(receipt_id)
        decision_id = str(row["decision_id"])
        if decision_id in result:
            raise ValueError(f"duplicate review: {decision_id}")
        result[decision_id] = row
    return result, hashlib.sha256(raw).hexdigest()


def build_ledger(event_rows: list[Any], reviews: dict[str, dict], *,
                 candidate_release: str, candidate_config_sha256: str,
                 pipeline_config_fingerprint: str,
                 review_source_sha256: str | None = None) -> dict:
    terminal_events: dict[str, dict] = {}
    for event in event_rows:
        properties = dict(getattr(event, "properties", None) or {})
        if (
            properties.get("pipeline_release") != candidate_release
            or properties.get("pipeline_config_fingerprint")
            != pipeline_config_fingerprint
        ):
            continue
        identity = decision_identity(
            properties.get("job_id"), properties.get("revision"),
            properties.get("segments_hash"),
            properties.get("pipeline_release"),
            properties.get("pipeline_config_fingerprint"),
        )
        decision_id = canonical_decision_id(identity)
        if (
            properties.get("evaluation_stage") != "terminal"
        ):
            continue
        if properties.get("decision_id") != decision_id:
            raise ValueError("invalid canonical ID in matching terminal decision")
        review = reviews.get(decision_id)
        occurred = getattr(event, "occurred_at", None) or getattr(
            event, "created_at", None,
        )
        if occurred is None:
            raise ValueError(f"missing timestamp in terminal decision: {decision_id}")
        if review is not None:
            if review.get("snapshot_sha256") != identity["segments_hash"]:
                raise ValueError(f"review snapshot mismatch: {decision_id}")
            reviewed_at = datetime.fromisoformat(
                str(review["reviewed_at"]).replace("Z", "+00:00")
            )
            occurred_aware = occurred
            if occurred_aware.tzinfo is None:
                occurred_aware = occurred_aware.replace(tzinfo=timezone.utc)
            if reviewed_at.astimezone(timezone.utc) < occurred_aware.astimezone(timezone.utc):
                raise ValueError(f"review predates terminal decision: {decision_id}")
        row = {
            "decision_id": decision_id,
            "occurred_at": occurred.isoformat(),
            "eligible": bool(properties.get("eligible")),
            "would_approve": bool(properties.get("would_approve")),
            "reviewed": review is not None,
            "correct": review.get("correct") if review else None,
            "catastrophic": review.get("catastrophic") if review else None,
            "reviewer_id": str(review.get("reviewer_id")) if review else None,
            "candidate_release": candidate_release,
            "candidate_config_sha256": candidate_config_sha256,
            "pipeline_config_fingerprint": pipeline_config_fingerprint,
        }
        previous = terminal_events.get(decision_id)
        if previous is not None:
            prior_outcome = {
                key: value for key, value in previous.items()
                if key != "occurred_at"
            }
            current_outcome = {
                key: value for key, value in row.items()
                if key != "occurred_at"
            }
            if prior_outcome != current_outcome:
                raise ValueError(f"conflicting terminal decision: {decision_id}")
            if previous["occurred_at"] <= row["occurred_at"]:
                continue
        terminal_events[decision_id] = row
    return {
        "schema_version": 5,
        "candidate_release": candidate_release,
        "candidate_config_sha256": candidate_config_sha256,
        "pipeline_config_fingerprint": pipeline_config_fingerprint,
        "review_source_sha256": review_source_sha256,
        "decisions": sorted(
            terminal_events.values(),
            key=lambda row: (row["occurred_at"], row["decision_id"]),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-release", required=True)
    parser.add_argument("--candidate-config", required=True, type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        config_raw = args.candidate_config.read_bytes()
        config_payload = json.loads(config_raw.decode("utf-8"))
        config_sha256 = hashlib.sha256(config_raw).hexdigest()
        config_fingerprint = str(
            config_payload.get("pipeline_config_fingerprint") or ""
        )
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        parser.error(f"invalid --candidate-config: {exc}")
    if not _is_hash(config_fingerprint, 16):
        parser.error("candidate config must contain a 16-hex pipeline_config_fingerprint")
    private_key = os.environ.get("BENCHMARK_SHADOW_PRIVATE_KEY") or ""
    key_id = os.environ.get("BENCHMARK_SHADOW_KEY_ID") or ""
    if not private_key or not key_id:
        parser.error("shadow Ed25519 private key/key id are required")

    from database import ProductEvent, SessionLocal

    reviews, review_hash = _reviews(args.reviews)
    db = SessionLocal()
    try:
        events = db.query(ProductEvent).filter(
            ProductEvent.name == EVENT_NAME,
        ).order_by(ProductEvent.created_at.asc()).all()
    finally:
        db.close()
    ledger = build_ledger(
        events, reviews,
        candidate_release=args.candidate_release,
        candidate_config_sha256=config_sha256,
        pipeline_config_fingerprint=config_fingerprint,
        review_source_sha256=review_hash,
    )
    ledger["attestation"] = shadow_ledger_attestation(ledger, private_key, key_id)
    write_json_exclusive(args.output, ledger)
    print(f"Wrote {len(ledger['decisions'])} decisions to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
