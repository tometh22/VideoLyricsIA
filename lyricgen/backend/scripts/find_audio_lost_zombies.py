"""Find jobs whose input_r2_key points at another job's directory.

These are "zombies" — variants created when /jobs/{id}/variant's
copy_object silently fell back to sharing the parent's key (bug fixed
in fix/audio-lost-variant-cleanup, 2026-05-27). If the parent's WAV
has since been GC'd, the variant gives 404 on /waveform and
/source-audio-url.

Usage:
    python -m scripts.find_audio_lost_zombies                 # report only
    python -m scripts.find_audio_lost_zombies --check-r2      # also verify each key in R2
    python -m scripts.find_audio_lost_zombies --mark-error    # mark confirmed zombies as error

The --mark-error mode is destructive (changes job.status). Read the
console output before running it.
"""
import argparse
import re
import sys
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-r2", action="store_true",
        help="Use storage.object_exists to verify each key actually 404s.",
    )
    parser.add_argument(
        "--mark-error", action="store_true",
        help="Mark zombies (confirmed by --check-r2) as status=error.",
    )
    parser.add_argument(
        "--tenant", default=None,
        help="Restrict to a single tenant_id.",
    )
    args = parser.parse_args()

    if args.mark_error and not args.check_r2:
        print("--mark-error requires --check-r2 to confirm the audio is actually gone.", file=sys.stderr)
        sys.exit(2)

    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from database import Job, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Job).filter(
            Job.input_r2_key.isnot(None),
            Job.input_r2_key != "",
        )
        if args.tenant:
            q = q.filter(Job.tenant_id == args.tenant)

        # Pattern: inputs/{tenant}/{job_id}/{filename}. The {job_id} in the
        # key must equal the row's own job_id, otherwise the key is
        # inherited from another job.
        pat = re.compile(r"^inputs/[^/]+/([^/]+)/")
        suspects = []
        for j in q.all():
            m = pat.match(j.input_r2_key or "")
            if not m:
                # Unusual key shape — log it but don't classify as zombie.
                continue
            referenced_job_id = m.group(1)
            if referenced_job_id != j.job_id:
                suspects.append((j, referenced_job_id))

        if not suspects:
            print("No zombies found. All non-null input_r2_key match the row's own job_id.")
            return

        # Group by referenced job_id so the operator sees clusters.
        by_parent = defaultdict(list)
        for j, parent_id in suspects:
            by_parent[parent_id].append(j)

        print(f"Found {len(suspects)} job(s) referencing {len(by_parent)} distinct parent prefix(es).\n")
        confirmed_zombies = []

        for parent_id, jobs in sorted(by_parent.items()):
            print(f"  Parent prefix points at job_id={parent_id}")
            parent = db.query(Job).filter(Job.job_id == parent_id).first()
            parent_status = parent.status if parent else "(parent not in DB)"
            print(f"    Parent status: {parent_status}")
            for j in jobs:
                line = f"      - {j.job_id}  status={j.status:<20}  tenant={j.tenant_id}  key={j.input_r2_key}"
                print(line)

            if args.check_r2:
                import storage
                # Check the first one; they all reference the same key.
                sample_key = jobs[0].input_r2_key
                exists = storage.object_exists(sample_key)
                if not exists:
                    print(f"    >>> R2 object {sample_key!r} returns 404 — CONFIRMED ZOMBIES")
                    confirmed_zombies.extend(jobs)
                else:
                    print(f"    R2 object exists — variants still functional, no action needed")
            print()

        if args.mark_error and confirmed_zombies:
            print(f"\n=== Marking {len(confirmed_zombies)} confirmed zombies as error ===")
            from jobs import update_job
            for j in confirmed_zombies:
                update_job(
                    j.job_id,
                    status="error",
                    error_message="Audio original eliminado — subí el archivo de nuevo.",
                )
                print(f"  Marked {j.job_id} as error")
            print("Done. Operator should notify affected tenants.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
