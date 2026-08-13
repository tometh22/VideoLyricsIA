"""CLI wrapper around storage.cleanup_old_inputs — delete user-uploaded MP3
inputs once they pass the retention window.

HARDENED 2026-05-27 after the agus.cafisi incident (26 input audios lost
between 5/11 and 5/18, no audit trail at the time).
  - Default retention bumped 30 → 365 d (R2 storage is cheap, recovery
    is expensive).
  - `--apply` now requires the env var ALLOW_INPUT_CLEANUP=true.
    Without it the script refuses to delete (still does dry-run).

Idempotent — safe to run repeatedly.

Usage from the backend dir:

    python3 scripts/cleanup_old_inputs.py             # dry-run (default)
    ALLOW_INPUT_CLEANUP=true python3 scripts/cleanup_old_inputs.py --apply
    python3 scripts/cleanup_old_inputs.py --apply --retention-days 365

Env required: R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT_URL /
R2_BUCKET (the same set the API uses).

To actually delete: ALLOW_INPUT_CLEANUP=true must be set in the
environment. This is the safety lock against accidental sweeps.

Exit codes: 0 success, 1 unrecoverable error (missing env, R2 unreachable),
2 safety lock not unset.
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
                   default=int(os.environ.get("INPUT_RETENTION_DAYS", "365")))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--prefix", default="inputs/")
    p.add_argument("--json", action="store_true",
                   help="Print full JSON report instead of human summary.")
    args = p.parse_args()

    # Safety lock: --apply needs the env var to be set explicitly to "true".
    # If you forget to set it, the script refuses to delete but still
    # produces a dry-run report. Same gate as /admin/cleanup-inputs.
    if args.apply and os.environ.get("ALLOW_INPUT_CLEANUP", "").strip().lower() != "true":
        print(
            "ERROR: --apply is locked. Set ALLOW_INPUT_CLEANUP=true to delete.",
            file=sys.stderr,
        )
        print(
            "  Example: ALLOW_INPUT_CLEANUP=true python3 scripts/cleanup_old_inputs.py --apply",
            file=sys.stderr,
        )
        print(
            "  This safety lock was added 2026-05-27 after the agus.cafisi incident.",
            file=sys.stderr,
        )
        return 2

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
