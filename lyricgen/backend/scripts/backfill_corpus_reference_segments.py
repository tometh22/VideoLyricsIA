#!/usr/bin/env python3
"""One-off: seed CorpusSong.reference_segments from each corpus song's
already-reviewed editor_documents transcription (see corpus_reference.py
for the matching + conversion logic and the "why").

Run once after deploying migration 310138eb0e54 (Railway shell, where
DATABASE_URL points at the target DB):

    python scripts/backfill_corpus_reference_segments.py            # dry run
    python scripts/backfill_corpus_reference_segments.py --apply

Safe to re-run: idempotent, recomputes the same result every time. The
same operation is also reachable without shell access via
POST /admin/corpus/songs/backfill-references (admin auth required).
"""
import argparse
import json
import os
import sys

# Make backend importable when run as scripts/backfill_corpus_reference_segments.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from corpus_reference import backfill_reference_segments
from database import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="persist changes (default: dry run, no writes)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = backfill_reference_segments(db, dry_run=not args.apply)
    finally:
        db.close()

    print(json.dumps(stats, indent=2, ensure_ascii=False))
    if not args.apply:
        print("\nDRY RUN — no changes were saved. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
