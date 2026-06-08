#!/usr/bin/env python3
"""Harvest user lyric-timing corrections into a training corpus.

WHY
---
This is Fase C1 of the Rotor-parity plan: build the dataset that lets us learn
from what users fix in the editor — the same "thousands of hand-corrected
timings" that are Rotor's moat. The capture ALREADY happens in production: the
`/jobs/{id}/save-segments` endpoint writes a per-line diff (prev→new start/end/
text) to `AuditLog` under action `lyrics.segments_diff` every time a user edits
the timeline. This script turns those diffs into clean training pairs.

WHAT IT PRODUCES
----------------
One JSONL row per corrected line:
    {job_id, audio_key, text, auto_start, auto_end, human_start, human_end,
     d_start, d_end}
where `auto_*` is the model's original value (the FIRST `prev_*` ever logged for
that line — i.e. before any human touched it) and `human_*` is the latest value
the user settled on. Lines a user moved are the high-signal corrections; the
final `segments_json` of a job is the human-approved ground truth.

These pairs feed: (C2) aggregate offset/bias analysis + per-audio cached
timelines, and eventually (C3) fine-tuning an alignment / residual-correction
model.

USAGE
-----
    cd lyricgen/backend
    export DATABASE_URL=...            # staging or prod (read-only queries)
    python scripts/export_lyric_corrections.py --out corrections.jsonl
    python scripts/export_lyric_corrections.py --stats   # just print aggregates

NOTE: read-only. Never writes to the DB.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def harvest(session) -> list[dict]:
    """Reconstruct (auto → human) per-line corrections from the audit diffs."""
    from database import AuditLog, Job

    rows = (session.query(AuditLog)
            .filter(AuditLog.action == "lyrics.segments_diff")
            .order_by(AuditLog.created_at.asc())
            .all())

    # job_id → line_id → {auto_*: first prev seen, human_*: last new seen, text}
    per_job: dict[str, dict] = {}
    for r in rows:
        detail = r.detail if isinstance(r.detail, dict) else {}
        job_id = detail.get("job_id")
        if not job_id:
            continue
        lines = per_job.setdefault(job_id, {})
        for ch in (detail.get("changed") or []):
            lid = ch.get("id")
            if lid is None:
                continue
            slot = lines.setdefault(lid, {})
            # auto = the earliest prev_* we ever saw (set once)
            if "auto_start" not in slot:
                slot["auto_start"] = _num(ch.get("prev_start"))
                slot["auto_end"] = _num(ch.get("prev_end"))
                slot["auto_text"] = ch.get("prev_text")
            # human = the latest new_* (overwrite each pass)
            slot["human_start"] = _num(ch.get("new_start"))
            slot["human_end"] = _num(ch.get("new_end"))
            slot["human_text"] = ch.get("new_text")

    # join audio identity from Job
    job_ids = list(per_job.keys())
    audio_by_job: dict[str, str] = {}
    if job_ids:
        for j in session.query(Job).filter(Job.job_id.in_(job_ids)).all():
            audio_by_job[j.job_id] = (getattr(j, "input_r2_key", None) or "")

    out: list[dict] = []
    for job_id, lines in per_job.items():
        for lid, s in lines.items():
            a0, h0 = s.get("auto_start"), s.get("human_start")
            if a0 is None or h0 is None:
                continue
            out.append({
                "job_id": job_id,
                "audio_key": audio_by_job.get(job_id, ""),
                "line_id": lid,
                "text": (s.get("human_text") or s.get("auto_text") or "")[:200],
                "auto_start": a0, "auto_end": s.get("auto_end"),
                "human_start": h0, "human_end": s.get("human_end"),
                "d_start": round(h0 - a0, 3),
                "d_end": (round(s["human_end"] - s["auto_end"], 3)
                          if s.get("human_end") is not None and s.get("auto_end") is not None else None),
            })
    return out


def print_stats(corr: list[dict]) -> None:
    if not corr:
        print("No corrections found yet.")
        return
    ds = [c["d_start"] for c in corr if c.get("d_start") is not None]
    jobs = {c["job_id"] for c in corr}
    print(f"corrections: {len(corr)} lines across {len(jobs)} jobs")
    if ds:
        ds_sorted = sorted(ds)
        print(f"  Δstart  mean={statistics.mean(ds):+.2f}s  median={statistics.median(ds):+.2f}s  "
              f"min={ds_sorted[0]:+.2f}s  max={ds_sorted[-1]:+.2f}s")
        # aggregate bias: are users systematically nudging one direction?
        late = sum(1 for d in ds if d > 0.15)   # auto was EARLY → user pushed later
        early = sum(1 for d in ds if d < -0.15)  # auto was LATE → user pulled earlier
        print(f"  bias: {late} lines auto-too-early, {early} lines auto-too-late, "
              f"{len(ds) - late - early} ~unchanged")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, help="write JSONL here")
    ap.add_argument("--stats", action="store_true", help="print aggregates only")
    args = ap.parse_args()

    from database import SessionLocal
    session = SessionLocal()
    try:
        corr = harvest(session)
    finally:
        session.close()

    if args.out:
        with args.out.open("w") as f:
            for c in corr:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"wrote {len(corr)} correction rows → {args.out}")
    print_stats(corr)


if __name__ == "__main__":
    main()
