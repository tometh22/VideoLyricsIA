"""Bulk re-upload of lost input audios.

Given a local directory of WAV/MP3 files and a tenant_id, finds all
jobs whose input_r2_key is dead (file gone from R2) and re-uploads
the matching local file via the public `POST /jobs/{id}/restore-audio`
endpoint.

Match heuristic: a local file matches a job iff its basename (after
`_safe_basename`-style sanitization) equals `Job.filename` in DB.
Multiple jobs can share the same filename (re-renders of the same
song) — all of them get the same WAV uploaded (R2 storage is cheap).

USAGE
-----

This runs FROM YOUR LAPTOP (or any machine with the WAVs + network
access to the API), NOT from inside the container — the container
doesn't have the WAV files. It only needs:
  - Python 3.10+
  - `requests` and `python-jose[cryptography]` (or just `requests` +
    a pre-fetched bearer token).
  - The WAVs in a folder.

    cd lyricgen/backend
    pip install requests
    python scripts/bulk_restore_audios.py \\
        --api https://api.genly.pro \\
        --token <BEARER_TOKEN> \\
        --tenant agus.cafisi \\
        --dir ~/Downloads/agus-wavs \\
        --dry-run

Drops --dry-run to actually upload. The script will:
  1. Login (or use --token if provided) and call /jobs to list the
     tenant's jobs.
  2. For each job whose status is done/pending_review and whose
     filename matches a file in --dir, POST /jobs/{id}/restore-audio.
  3. Skip jobs without a matching local file (prints a list at the end).
  4. Skip jobs whose audio is already alive (the endpoint is
     idempotent — re-uploading is also fine, but we save bandwidth).

To get a token:

    curl -s -X POST https://api.genly.pro/auth/login \\
      -H 'Content-Type: application/json' \\
      -d '{"username":"<USER>","password":"<PASSWORD>"}'  | jq -r .access_token

Output is human-readable with per-job results + a final summary.
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(2)


def _safe_basename(filename: str) -> str:
    """Mirror of lyricgen/backend/main.py _safe_basename for client-side
    matching. Strips directory components, control chars; doesn't
    truncate (we just want canonical comparison)."""
    base = os.path.basename(filename or "")
    # Replace controls + dangerous chars with _
    out = []
    for ch in base:
        if ord(ch) < 32 or ch in '/\\:*?"<>|':
            out.append("_")
        else:
            out.append(ch)
    return "".join(out).strip()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="https://api.genly.pro", help="API base URL.")
    p.add_argument("--token", required=True, help="Bearer access token (from /auth/login).")
    p.add_argument("--tenant", required=True, help="Tenant ID (e.g. 'agus.cafisi').")
    p.add_argument("--dir", required=True, help="Directory containing the .wav/.mp3 files.")
    p.add_argument("--dry-run", action="store_true", help="Show plan, don't upload.")
    p.add_argument("--limit", type=int, default=200, help="Max jobs to scan per tenant.")
    args = p.parse_args()

    api = args.api.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"}

    # 1. Index local files by sanitized basename.
    local_dir = Path(args.dir).expanduser().resolve()
    if not local_dir.is_dir():
        print(f"ERROR: --dir not found: {local_dir}", file=sys.stderr)
        return 2

    extensions = (".wav", ".mp3", ".m4a", ".flac")
    local_files: dict[str, Path] = {}
    for f in local_dir.iterdir():
        if not f.is_file():
            continue
        if not f.name.lower().endswith(extensions):
            continue
        key = _safe_basename(f.name)
        local_files[key] = f
        # Also index by lowercase fallback in case of casing drift.
        local_files.setdefault(key.lower(), f)

    print(f"Indexed {len({v for v in local_files.values()})} local audio file(s) in {local_dir}")

    # 2. Fetch jobs from API. We use the existing /jobs endpoint
    #    (returns the caller's own jobs) — the bearer token must
    #    belong to the tenant we're restoring for.
    r = requests.get(f"{api}/jobs", headers=headers, timeout=30)
    r.raise_for_status()
    all_jobs = r.json()
    if not isinstance(all_jobs, list):
        # /jobs might wrap in {jobs: [...]}; handle both shapes.
        all_jobs = all_jobs.get("jobs") if isinstance(all_jobs, dict) else []

    # The /jobs endpoint is ALREADY tenant-scoped server-side via
    # _job_scope(current_user) — it returns only the caller's own
    # tenant's jobs. So we don't need a client-side tenant filter (and
    # the response doesn't include tenant_id per-row, which made the
    # old client-side filter drop every job to 0). Just use what we got.
    # The --tenant arg stays as a documentation / sanity-check parameter:
    # if the token belongs to a different tenant, the operator gets back
    # the wrong jobs and would notice immediately in the dry-run output.
    jobs_tenant = all_jobs[:args.limit]
    print(f"Fetched {len(jobs_tenant)} job(s) for the token's tenant "
          f"(expected --tenant {args.tenant})")
    print()

    # 3. Per job: decide what to do.
    counts = {"would-upload": 0, "uploaded": 0, "failed": 0, "no-match": 0, "skip-alive": 0}
    failures: list[tuple[str, str]] = []
    no_match: list[str] = []

    for j in jobs_tenant:
        job_id = j.get("job_id") or j.get("id")
        if not job_id:
            continue
        filename = j.get("filename") or ""
        status = j.get("status") or ""

        # Match local file by filename.
        key = _safe_basename(filename)
        local_file = local_files.get(key) or local_files.get(key.lower())
        if not local_file:
            counts["no-match"] += 1
            no_match.append(f"{job_id} | {status:<20} | {filename}")
            continue

        # Probe the current source-audio-url to see if it's already alive.
        # If we get 200 and response has source="input" + fallback=false,
        # the audio is already good — skip.
        try:
            probe = requests.get(
                f"{api}/jobs/{job_id}/source-audio-url",
                headers=headers, timeout=15,
            )
            if probe.status_code == 200:
                data = probe.json()
                if data.get("source") == "input" and not data.get("fallback"):
                    counts["skip-alive"] += 1
                    print(f"  ✅ alive   {job_id} | {filename[:50]}")
                    continue
        except Exception:
            # Probe failure isn't fatal — proceed with restore.
            pass

        if args.dry_run:
            counts["would-upload"] += 1
            print(f"  🔄 plan    {job_id} | {filename[:50]}  ← {local_file.name}")
            continue

        # Actually upload.
        try:
            with local_file.open("rb") as fh:
                up = requests.post(
                    f"{api}/jobs/{job_id}/restore-audio",
                    headers=headers,
                    files={"audio": (local_file.name, fh, "audio/wav")},
                    timeout=180,
                )
            if up.status_code == 200:
                counts["uploaded"] += 1
                data = up.json()
                print(f"  ✅ upload  {job_id} | {filename[:50]}  ({data.get('size_mb')} MB)")
            else:
                counts["failed"] += 1
                detail = ""
                try:
                    detail = up.json().get("detail", "")
                except Exception:
                    detail = up.text[:200]
                failures.append((job_id, f"HTTP {up.status_code}: {detail}"))
                print(f"  ❌ fail    {job_id} | HTTP {up.status_code} | {detail}")
        except Exception as e:
            counts["failed"] += 1
            failures.append((job_id, str(e)))
            print(f"  ❌ fail    {job_id} | {e}")

    print()
    print("=" * 70)
    print("Summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    if no_match:
        print()
        print(f"Jobs with no matching local file ({len(no_match)}):")
        for line in no_match:
            print(f"  {line}")
        print("→ Either drop the WAV into --dir with the matching name, or")
        print("  the original WAV is unrecoverable and these jobs are stuck")
        print("  with the MP4 fallback for editor preview only.")

    if failures:
        print()
        print(f"Upload failures ({len(failures)}):")
        for job_id, err in failures:
            print(f"  {job_id}: {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
