"""Read-only count of UMG portal rows and objects due for retention cleanup."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from delivery_retention import cleanup_expired_deliveries  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(cleanup_expired_deliveries(dry_run=True), sort_keys=True))
