#!/usr/bin/env python3
"""Download approved jobs from prod into a local benchmark dataset.

The dataset is a list of (audio, ground-truth segments) tuples used by
`run_pipeline_local.py` and `score_benchmark.py` to measure how well a
pipeline iteration recovers what the operator hand-corrected into the
production segments_json.

Why this matters: any AI-quality improvement claim ("WER drops 40%",
"timing tightens to <200ms") needs a fixed reference dataset to prove
itself, otherwise the measurement is anecdote. The 5-10 jobs the
operator has already approved (status=done or pending_review with a
later approval audit entry) are perfect ground truth — they represent
what a human deemed shippable.

Usage:
    cd lyricgen/backend
    source venv/bin/activate
    export DATABASE_URL='postgresql://...'  # prod or staging DB
    export R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT_URL R2_BUCKET
    # Edit scripts/benchmark_jobs.txt with one job_id per line, then:
    python scripts/build_benchmark_dataset.py

    # Custom path / single job:
    python scripts/build_benchmark_dataset.py --jobs job_id_1 job_id_2
    python scripts/build_benchmark_dataset.py --list /path/to/jobs.txt

Output layout (relative to scripts/, ignored by git):
    benchmark/dataset/
        <job_id>/
            audio.<ext>            # original upload, raw bytes from R2
            ground_truth.json      # job_row.segments_json (operator-approved)
            metadata.json          # artist, song_title, render_params, etc.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

DEFAULT_LIST = HERE / "benchmark_jobs.txt"
OUT_ROOT = HERE.parent / "benchmark" / "dataset"


def _read_jobs_file(path: Path) -> list[str]:
    if not path.exists():
        print(f"[ERR] jobs list not found: {path}", file=sys.stderr)
        print(f"      Create it with one job_id per line, or use --jobs.", file=sys.stderr)
        sys.exit(2)
    out = []
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _fetch_job(db, job_id: str) -> dict | None:
    """Return a flat dict of the fields we need for benchmarking, or
    None if the job is unusable (missing audio, no segments, etc.)."""
    from database import Job
    row = db.query(Job).filter(Job.job_id == job_id).first()
    if row is None:
        print(f"  ⚠ {job_id}: not found in DB")
        return None
    if not row.input_r2_key:
        print(f"  ⚠ {job_id}: no input_r2_key (no R2 audio to download)")
        return None
    if not row.segments_json or not isinstance(row.segments_json, list) or len(row.segments_json) == 0:
        print(f"  ⚠ {job_id}: segments_json empty or invalid — needs operator approval first")
        return None
    return {
        "job_id": row.job_id,
        "tenant_id": row.tenant_id,
        "artist": row.artist or "",
        "song_title": row.song_title or "",
        "filename": row.filename or "audio.wav",
        "status": row.status,
        "input_r2_key": row.input_r2_key,
        "segments_json": row.segments_json,
        "render_params": row.render_params or {},
        "delivery_profile": row.delivery_profile or "youtube",
    }


def _download_audio(input_r2_key: str, dest_dir: Path) -> Path | None:
    """Stream the input audio from R2 to dest_dir, preserving the
    original extension. Returns the local path or None on failure."""
    import storage
    ext = Path(input_r2_key).suffix or ".wav"
    local = dest_dir / f"audio{ext}"
    if local.exists() and local.stat().st_size > 0:
        print(f"      audio already present ({local.stat().st_size // 1024} KB), skip download")
        return local
    ok = storage.download_object(input_r2_key, str(local))
    if not ok:
        print(f"  ⚠ R2 download failed for {input_r2_key}")
        return None
    return local


def build_dataset(job_ids: list[str]) -> None:
    from database import SessionLocal

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Building dataset under: {OUT_ROOT}")
    print(f"Jobs requested: {len(job_ids)}")
    print()

    db = SessionLocal()
    try:
        ok_count = 0
        skip_count = 0
        for job_id in job_ids:
            print(f"[{job_id}]")
            job = _fetch_job(db, job_id)
            if job is None:
                skip_count += 1
                continue
            dest = OUT_ROOT / job_id
            dest.mkdir(parents=True, exist_ok=True)

            audio_path = _download_audio(job["input_r2_key"], dest)
            if audio_path is None:
                skip_count += 1
                continue

            # Persist ground truth (the segments the operator approved)
            (dest / "ground_truth.json").write_text(
                json.dumps(job["segments_json"], ensure_ascii=False, indent=2)
            )
            # Strip segments_json from metadata — kept separate to avoid
            # accidentally treating the meta file as ground truth.
            meta = {k: v for k, v in job.items() if k != "segments_json"}
            (dest / "metadata.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str)
            )
            print(f"  ✓ {job['artist']} - {job['song_title']}  "
                  f"({len(job['segments_json'])} segments, "
                  f"audio {audio_path.stat().st_size // 1024} KB)")
            ok_count += 1
    finally:
        db.close()

    print()
    print(f"Done: {ok_count} ok, {skip_count} skipped")
    print(f"Run the baseline pipeline next:")
    print(f"    python scripts/run_pipeline_local.py")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jobs", nargs="*", help="job_id(s) directly on the command line")
    p.add_argument("--list", type=Path, default=DEFAULT_LIST, help=f"path to text file with job_ids (default {DEFAULT_LIST})")
    args = p.parse_args()

    if args.jobs:
        job_ids = args.jobs
    else:
        job_ids = _read_jobs_file(args.list)

    if not job_ids:
        print("No job_ids to process. Pass --jobs or populate benchmark_jobs.txt.", file=sys.stderr)
        sys.exit(2)

    build_dataset(job_ids)


if __name__ == "__main__":
    main()
