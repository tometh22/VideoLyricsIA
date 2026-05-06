"""CLI wrapper around storage.cleanup_old_inputs — delete user-uploaded MP3
inputs once they pass the retention window.

Designed for daily invocation by .github/workflows/daily-smoke.yml or any
cron. Idempotent — safe to run repeatedly.

Usage from the backend dir:

    python3 scripts/cleanup_old_inputs.py             # dry-run (default)
    python3 scripts/cleanup_old_inputs.py --apply     # actually delete
    python3 scripts/cleanup_old_inputs.py --apply --retention-days 30

Env required: R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT_URL /
R2_BUCKET (the same set the API uses).

Exit codes: 0 success, 1 unrecoverable error (missing env, R2 unreachable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--retention-days", type=int,
                   default=int(os.environ.get("INPUT_RETENTION_DAYS", "30")))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--prefix", default="inputs/")
    p.add_argument("--json", action="store_true",
                   help="Print full JSON report instead of human summary.")
    args = p.parse_args()

    backend = Path(__file__).resolve().parent.parent
    env_file = backend / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    import storage

    if not storage.is_enabled():
        print("R2 not configured (check R2_* env vars)", file=sys.stderr)
        return 1

    report = storage.cleanup_old_inputs(
        retention_days=args.retention_days,
        apply=args.apply,
        prefix=args.prefix,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        mode = "APPLY" if report["apply"] else "DRY-RUN"
        print(f"[{mode}] prefix={report['prefix']} retention={report['retention_days']}d cutoff={report['cutoff']}")
        print(f"[{mode}] scanned={report['scanned']} expired={report['expired']}")
        if not report["apply"]:
            mb = report["bytes_to_free_dryrun"] / 1024 / 1024
            print(f"[{mode}] {mb:.1f} MB would be freed")
            if report["sample"]:
                print(f"[{mode}] candidates (showing {len(report['sample'])}):")
                for s in report["sample"]:
                    print(f"  {s['key']}  {s['size_mb']:.1f} MB  age={s['age_days']}d")
        else:
            mb = report["bytes_freed"] / 1024 / 1024
            print(f"[APPLY] deleted={report['deleted']} freed={mb:.1f} MB")
            if report["errors"]:
                print(f"[APPLY] {len(report['errors'])} delete error(s):")
                for e in report["errors"][:5]:
                    print(f"  {e.get('Key')}: {e.get('Code')} {e.get('Message')}")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
