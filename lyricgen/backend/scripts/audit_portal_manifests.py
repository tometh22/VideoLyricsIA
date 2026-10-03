"""Read-only audit of active UMG Argentina/Chile published file manifests."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database import Delivery, DeliveriesSessionLocal  # noqa: E402
from delivery_integrity import inspect_delivery  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal", choices=("argentina", "chile"))
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    db = DeliveriesSessionLocal()
    counts = {"ok": 0, "failed": 0, "unknown": 0}
    try:
        query = db.query(Delivery).filter(Delivery.removed_at.is_(None))
        if args.portal:
            query = query.filter(Delivery.portal_id == args.portal)
        for delivery in query.order_by(Delivery.id).limit(args.limit):
            result = inspect_delivery(delivery)
            counts[result["status"]] += 1
            print(json.dumps(result, sort_keys=True))
        print(json.dumps({"summary": counts}, sort_keys=True))
        return 1 if counts["failed"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
