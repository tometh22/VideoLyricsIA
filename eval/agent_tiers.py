"""Layer-D tier eligibility, deterministic audit routing and dashboard replay.

This module does not mutate lyrics and cannot enable production by itself.  It
turns a category-level D1 score into an auditable routing policy.  Runtime
activation still requires a signed certificate and an explicit product
decision outside this evaluation harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from eval.canonical import read_json, write_json


VALID_CATEGORIES = {"text", "timing", "vocalization"}
VALID_RESOLVERS = {"auto", "agent", "agus"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_policy(score_report: dict[str, Any], activated_at: str) -> dict[str, Any]:
    activation = _parse_time(activated_at)
    categories = {}
    for category in sorted(VALID_CATEGORIES):
        result = (score_report.get("categories") or {}).get(category) or {}
        eligible = result.get("gate") == "GO_TIER_AGENT"
        categories[category] = {
            "d1_gate": result.get("gate", "MISSING"),
            "agent_eligible": eligible,
            "agent_enabled": False,
            "auto_enabled": False,
            "live_enabled": False,
            "requires_signed_runtime_certificate": True,
        }
    digest = hashlib.sha256(
        json.dumps(score_report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "mode": "layer_d_tier_policy",
        "activated_at": activation.isoformat(),
        "source_score_sha256": digest,
        "audit": {"first_14_days": 0.20, "after_14_days": 0.10},
        "categories": categories,
        "runtime_activation_performed": False,
        "auto_promotion_requires_tomi_decision": True,
        "live_forced_to_agus": True,
    }


def audit_rate(activated_at: str, now: str) -> float:
    age_days = (_parse_time(now) - _parse_time(activated_at)).total_seconds() / 86400
    return 0.20 if age_days < 14 else 0.10


def audit_selected(event_id: str, rate: float) -> bool:
    bucket = int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16], 16) / float(16**16)
    return bucket < rate


def route_event(event: dict[str, Any], policy: dict[str, Any], now: str) -> dict[str, Any]:
    category = str(event.get("category") or "")
    if category not in VALID_CATEGORIES:
        raise ValueError("invalid category")
    event_id = str(event.get("event_id") or "")
    if not event_id:
        raise ValueError("event_id required")
    category_policy = policy["categories"][category]
    if event.get("is_live"):
        return {"event_id": event_id, "resolver": "agus", "audit_required": False, "reason": "live_excluded"}
    if category_policy.get("agent_enabled") and category_policy.get("agent_eligible"):
        rate = audit_rate(policy["activated_at"], now)
        return {
            "event_id": event_id,
            "resolver": "agent",
            "audit_required": audit_selected(event_id, rate),
            "audit_rate": rate,
            "reason": "category_certified",
        }
    return {"event_id": event_id, "resolver": "agus", "audit_required": False, "reason": "category_not_enabled"}


def dashboard(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(events)
    invalid = [row for row in rows if row.get("resolved_by") not in VALID_RESOLVERS]
    if invalid:
        raise ValueError("resolved_by must be auto, agent or agus")
    by_resolver = {resolver: sum(row.get("resolved_by") == resolver for row in rows) for resolver in sorted(VALID_RESOLVERS)}
    audited = [row for row in rows if row.get("resolved_by") == "agent" and row.get("audit_verdict") in {"confirmed", "reverted"}]
    by_song: dict[str, float] = defaultdict(float)
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        by_song[str(row.get("song_id") or "unknown")] += max(0.0, float(row.get("human_seconds") or 0.0))
        by_category[str(row.get("category") or "unknown")][str(row["resolved_by"])] += 1
    total = len(rows)
    return {
        "schema_version": 1,
        "events": total,
        "resolution_share": {
            resolver: {"count": count, "rate": count / max(1, total)}
            for resolver, count in by_resolver.items()
        },
        "human_minutes_per_song": {
            song_id: seconds / 60.0 for song_id, seconds in sorted(by_song.items())
        },
        "agent_audit": {
            "audited": len(audited),
            "reverted": sum(row.get("audit_verdict") == "reverted" for row in audited),
            "reversal_rate": sum(row.get("audit_verdict") == "reverted" for row in audited) / max(1, len(audited)),
        },
        "by_category": {category: dict(sorted(values.items())) for category, values in sorted(by_category.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    policy_parser = sub.add_parser("policy")
    policy_parser.add_argument("--score", type=Path, default=Path("eval/runs/agent_corrector/report.json"))
    policy_parser.add_argument("--activated-at", required=True)
    policy_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector/tier_policy.json"))
    dashboard_parser = sub.add_parser("dashboard")
    dashboard_parser.add_argument("--events", type=Path, required=True)
    dashboard_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector/dashboard.json"))
    args = parser.parse_args()
    if args.command == "policy":
        result = build_policy(read_json(args.score.resolve()), args.activated_at)
    else:
        result = dashboard(_read_jsonl(args.events.resolve()))
    write_json(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
