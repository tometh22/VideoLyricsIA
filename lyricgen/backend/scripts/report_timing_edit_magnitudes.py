#!/usr/bin/env python3
"""Magnitud de las ediciones de timing: insumo para una tolerancia de revisión.

Solo lectura. Usa los mismos datos que ``calibrate_display_timing.py``:
la versión de máquina contra la aprobada, por línea emparejada.

- Edición de inicio/fin = el borde aprobado difiere del de máquina en
  >= 50 ms (mismo umbral que la fase 1).
- Para cada borde: % de las ediciones por debajo de 100/150/200/300/500 ms.
- % de líneas editadas cuya única edición fue de timing y menor a 150 ms
  (sin cambio de texto, sin alta ni baja).
- % de jobs cuyas ediciones de timing son todas menores a cada umbral (los
  que quedarían sin edición de timing si se aceptara esa tolerancia).

Cohortes: ``agosto`` = umg-gold-v1 (aprobados en portal en jul-ago),
``septiembre`` = los jobs de staging de la fase 1 (primera aprobación
10-sep → 4-oct).

Uso:
    DATABASE_URL=... python3.11 scripts/report_timing_edit_magnitudes.py \\
        --gold-dir .../umg-gold-v1/cases --json-out /tmp/magnitudes.json
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)

EDIT_THRESHOLD_S = 0.05
CUTS_MS = (100, 150, 200, 300, 500)
SMALL_MS = 150
COHORT = {"gold": "agosto", "staging": "septiembre"}


def line_edit(r: dict) -> dict:
    """Ediciones de una línea de máquina contra su par aprobado."""
    if r["deleted"] or not r["matched"]:
        return {"kind": "deleted" if r["deleted"] else "unmatched"}
    start = abs(r["approved_start"] - r["machine_start"])
    end = abs(r["approved_end"] - r["machine_end"])
    return {
        "kind": "matched",
        "text": r["text_changed"],
        "start": start if start >= EDIT_THRESHOLD_S else None,
        "end": end if end >= EDIT_THRESHOLD_S else None,
    }


def shares_below(values: list[float]) -> dict:
    n = len(values)
    return {
        "edits": n,
        **{f"below_{cut}ms": round(sum(1 for v in values if v < cut / 1000) / n, 4) if n else None
           for cut in CUTS_MS},
    }


def summarize(lines: list[dict]) -> dict:
    starts, ends = [], []
    edited_lines = 0
    only_small_timing = 0
    timing_edited_lines = 0
    timing_only_small_of_timing = 0
    by_job: dict[str, list[float]] = defaultdict(list)
    jobs = set()
    for r in lines:
        if not r["alignment_complete"]:
            continue
        jobs.add(r["job_id"])
        e = line_edit(r)
        if e["kind"] == "deleted":
            edited_lines += 1
            continue
        if e["kind"] != "matched":
            continue
        moved = [m for m in (e["start"], e["end"]) if m is not None]
        if e["start"] is not None:
            starts.append(e["start"])
        if e["end"] is not None:
            ends.append(e["end"])
        by_job[r["job_id"]].extend(moved)
        if e["text"] or moved:
            edited_lines += 1
        if moved:
            timing_edited_lines += 1
            if not e["text"] and max(moved) < SMALL_MS / 1000:
                only_small_timing += 1
                timing_only_small_of_timing += 1
    job_count = len(jobs)
    return {
        "jobs": job_count,
        "start": shares_below(starts),
        "end": shares_below(ends),
        "edited_lines_excluding_inserted": edited_lines,
        "only_edit_was_timing_below_150ms": {
            "lines": only_small_timing,
            "share_of_edited_lines": round(only_small_timing / edited_lines, 4) if edited_lines else None,
            "share_of_timing_edited_lines": (
                round(timing_only_small_of_timing / timing_edited_lines, 4)
                if timing_edited_lines else None
            ),
        },
        "jobs_whose_timing_edits_are_all_below": {
            f"{cut}ms": round(sum(
                1 for job in jobs if all(v < cut / 1000 for v in by_job.get(job, []))
            ) / job_count, 4) if job_count else None
            for cut in CUTS_MS
        },
        "jobs_without_timing_edits": round(
            sum(1 for job in jobs if not by_job.get(job)) / job_count, 4) if job_count else None,
    }


def inserted_lines(jobs: list[dict]) -> int:
    """Altas: líneas aprobadas sin línea de máquina (no tienen borde que medir)."""
    total = 0
    for job in jobs:
        machine = [dict(r) for r in (job["machine"] or []) if isinstance(r, dict)]
        approved = [dict(r) for r in (job["approved"] or []) if isinstance(r, dict)]
        _matched, _removed, inserted, complete = calibrate._match_rows(machine, approved)
        if complete:
            total += len(inserted)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold-dir", default="")
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]

    jobs, lines = calibrate.collect(args)
    report = {}
    for dataset, label in COHORT.items():
        subset = [r for r in lines if r["dataset"] == dataset]
        if not subset:
            continue
        report[label] = {
            **summarize(subset),
            "inserted_lines": inserted_lines([j for j in jobs if j["dataset"] == dataset]),
        }
        exact = [r for r in subset if r["machine_quality"] != "estimated"]
        if len(exact) != len(subset):
            report[f"{label}_sin_maquina_estimada"] = summarize(exact)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
