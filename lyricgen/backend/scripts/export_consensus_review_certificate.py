#!/usr/bin/env python3
"""Export hash-only editor observations and evaluate the frozen certificate gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from consensus_review_certificate import evaluate_consensus_review_gate  # noqa: E402
from database import ProductEvent, SessionLocal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        events = db.query(ProductEvent).filter(
            ProductEvent.name == "quality_consensus_observation",
        ).order_by(ProductEvent.created_at.asc(), ProductEvent.id.asc()).all()
        # Endpoint semantics allow one verdict per window.  Defensively keep
        # the latest row if historical imports contain a duplicate.
        by_window = {}
        for event in events:
            properties = dict(event.properties or {})
            window_id = str(properties.get("window_id") or "")
            if window_id:
                by_window[window_id] = properties
        rows = list(by_window.values())
    finally:
        db.close()
    evaluation = evaluate_consensus_review_gate(rows)
    payload = {
        **evaluation,
        "status": (
            "ready_for_signature"
            if evaluation["eligible_for_signed_certificate"] else "collecting"
        ),
        "observations": rows,
        "contains_raw_lyrics": False,
        "contains_audio": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": payload["status"],
        "reviewed_windows": payload["reviewed_windows"],
        "songs": payload["songs"],
        "incorrect": payload["incorrect"],
        "lower_95": payload["song_bootstrap_precision_lower_95"],
        "blockers": payload["blockers"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
