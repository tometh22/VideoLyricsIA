#!/usr/bin/env python3
"""Qué tienen en común las ediciones de timing grandes.

Solo lectura. Mismos 100 jobs de staging que las fases 1-2.

Conjuntos de líneas de máquina (emparejadas con la primera aprobación):
- ``end_gt_500``: fin movido más de 500 ms;
- ``start_edit``: inicio movido 50 ms o más;
- ``text_edit``: texto normalizado distinto;
- ``all_matched``: todas, como referencia.

e) % sin timestamps por palabra en la máquina, por origen.
f) % con edición de texto en la misma aprobación; y de las líneas con
   texto editado, % con además una edición de timing > 500 ms.
g) % de líneas cuyo texto normalizado aparece 2+ veces en la canción.
h) Tiempo del operador por línea. No hay eventos por línea: el tiempo
   activo (latidos, huecos <= 25 s) entre dos versiones guardadas
   consecutivas se reparte entre las líneas que cambiaron en esa versión, y
   cada una se lleva a su línea aprobada con el emparejador del AuditLog.

Uso:
    DATABASE_URL=... python3.11 scripts/diagnose_large_timing_edits.py --json-out /tmp/large.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timezone
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BACKEND / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate = _load("calibrate_display_timing")
minutes = _load("report_reviewer_minutes")

BIG_S = 0.5
EDIT_S = 0.05
HUMAN_REASONS = frozenset({"manual", "autosave", "approve", "restore", "conflict",
                           "change_request", "quality_proposal"})


def origin_group(origin: str) -> str:
    return "ctc" if origin == "ctc" else ("whisperx" if origin.startswith("whisperx") else "other")


def classify(r: dict) -> dict:
    start = abs(r["approved_start"] - r["machine_start"])
    end = abs(r["approved_end"] - r["machine_end"])
    return {
        "end_gt_500": end > BIG_S,
        "start_edit": start >= EDIT_S,
        "timing_gt_500": end > BIG_S or start > BIG_S,
        "timing_edit": end >= EDIT_S or start >= EDIT_S,
        "text_edit": r["text_changed"],
    }


def repeated_texts(segments) -> Counter:
    return Counter(
        calibrate.normalize_text(s.get("text")) for s in (segments or [])
        if isinstance(s, dict) and calibrate.normalize_text(s.get("text"))
    )


def share(rows: list[dict], predicate) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if predicate(r))
    return {"lines": n, "n": k, "share": round(k / n, 4) if n else None}


def line_seconds(conn, jobs: list[dict]) -> dict[tuple[str, int], float]:
    """Segundos activos atribuidos a cada línea aprobada (job_id, índice aprobado)."""
    cur = conn.cursor()
    job_ids = [j["job_id"] for j in jobs]
    cur.execute(
        """
        select job_id, revision, reason, is_approved, created_at, segments
          from editor_versions where job_id = any(%s) order by job_id, revision
        """,
        (job_ids,),
    )
    versions: dict[str, list[tuple]] = defaultdict(list)
    for row in cur.fetchall():
        versions[row[0]].append(row[1:])
    cur.execute(
        """
        select job_id, user_id, coalesce(occurred_at, created_at)
          from product_events
         where name = 'editor_activity_heartbeat' and job_id = any(%s)
        """,
        (job_ids,),
    )
    beats: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for job_id, user_id, when in cur.fetchall():
        if when is not None:
            beats[job_id][user_id].append(when if when.tzinfo else when.replace(tzinfo=timezone.utc))

    seconds: dict[tuple[str, int], float] = defaultdict(float)
    for job in jobs:
        rows = versions.get(job["job_id"], [])
        approved = next((v for v in sorted(rows, key=lambda v: v[3]) if v[2]), None)
        if approved is None:
            continue
        approved_segments = [dict(s) for s in approved[4] or [] if isinstance(s, dict)]
        chain = [v for v in rows if v[3] <= approved[3]]
        previous = None
        for revision, reason, _is_approved, created_at, segments in chain:
            current = [dict(s) for s in segments or [] if isinstance(s, dict)]
            if previous is not None and reason in HUMAN_REASONS:
                prev_at, prev_segments = previous
                changed = _changed_rows(prev_segments, current)
                if changed:
                    window = 0.0
                    for stamps in beats.get(job["job_id"], {}).values():
                        inside = sorted(t for t in stamps if prev_at < t <= created_at)
                        if inside:
                            window += minutes.active_seconds(inside, 25.0)
                    to_approved = _map_to(current, approved_segments)
                    targets = [to_approved[i] for i in changed if i in to_approved]
                    for target in targets:
                        seconds[(job["job_id"], target)] += window / len(targets)
            previous = (created_at, current)
    return seconds


def _changed_rows(before: list[dict], after: list[dict]) -> list[int]:
    matched, _removed, inserted, complete = calibrate._match_rows(before, after)
    if not complete:
        return []
    changed = list(inserted)
    for b, a, _ in matched:
        old, new = before[b], after[a]
        if (calibrate.normalize_text(old.get("text")) != calibrate.normalize_text(new.get("text"))
                or abs(calibrate._finite_time(old.get("start")) - calibrate._finite_time(new.get("start"))) >= EDIT_S
                or abs(calibrate._finite_time(old.get("end")) - calibrate._finite_time(new.get("end"))) >= EDIT_S):
            changed.append(a)
    return changed


def _map_to(rows: list[dict], approved: list[dict]) -> dict[int, int]:
    matched, _removed, _inserted, complete = calibrate._match_rows(rows, approved)
    return {b: a for b, a, _ in matched} if complete else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]
    args.gold_dir = ""

    jobs, lines = calibrate.collect(args)
    jobs = [j for j in jobs if j["dataset"] == "staging"]
    repeated = {j["job_id"]: repeated_texts(j["machine"]) for j in jobs}
    approved_index: dict[tuple[str, int], int] = {}
    for job in jobs:
        machine = [dict(s) for s in job["machine"] or [] if isinstance(s, dict)]
        approved = [dict(s) for s in job["approved"] or [] if isinstance(s, dict)]
        for b, a, _ in (calibrate._match_rows(machine, approved)[0]):
            approved_index[(job["job_id"], b)] = a

    rows = []
    for r in lines:
        if r["dataset"] != "staging" or not r["alignment_complete"] or not r["matched"]:
            continue
        flags = classify(r)
        text = calibrate.normalize_text(r["segment"].get("text"))
        rows.append({
            **flags,
            "job_id": r["job_id"],
            "origin": origin_group(r["origin"]),
            "has_words": r["has_words"],
            "repeated": repeated[r["job_id"]][text] >= 2 if text else False,
            "approved_index": approved_index.get((r["job_id"], r["index"])),
        })

    sets = {
        "end_gt_500": [r for r in rows if r["end_gt_500"]],
        "start_edit": [r for r in rows if r["start_edit"]],
        "text_edit": [r for r in rows if r["text_edit"]],
        "all_matched": rows,
    }
    report: dict = {"lines": {k: len(v) for k, v in sets.items()}}
    report["e_without_word_timestamps"] = {
        name: {
            origin: share([r for r in subset if origin == "all" or r["origin"] == origin],
                          lambda r: not r["has_words"])
            for origin in ("all", "ctc", "whisperx", "other")
        }
        for name, subset in sets.items()
    }
    report["f_with_text_edit"] = {
        name: share(subset, lambda r: r["text_edit"]) for name, subset in sets.items()
    }
    report["f_text_edits_with_timing_gt_500"] = share(sets["text_edit"], lambda r: r["timing_gt_500"])
    report["g_repeated_text"] = {
        name: share(subset, lambda r: r["repeated"]) for name, subset in sets.items()
    }

    # i) ¿El timing aprobado cae dentro de la ventana de máquina ± margen?
    # Es el techo de cualquier realineado acotado a esa ventana.
    staging_lines = [r for r in lines if r["dataset"] == "staging"
                     and r["alignment_complete"] and r["matched"]]

    def window_share(subset, margin):
        inside = sum(
            1 for r in subset
            if r["approved_start"] >= r["machine_start"] - margin
            and r["approved_end"] <= r["machine_end"] + margin
        )
        return round(inside / len(subset), 4) if subset else None

    big = [r for r in staging_lines if classify(r)["timing_gt_500"]]
    report["i_approved_inside_machine_window"] = {
        name: {"lines": len(subset), **{f"margin_{m}s": window_share(subset, m) for m in (1, 2, 4)}}
        for name, subset in (
            ("timing_gt_500_with_text", [r for r in big if r["text_changed"]]),
            ("timing_gt_500_without_text", [r for r in big if not r["text_changed"]]),
        )
    }

    conn = None
    if os.environ.get("DATABASE_URL"):
        import psycopg2

        conn = psycopg2.connect(os.environ["DATABASE_URL"],
                                options="-c default_transaction_read_only=on -c statement_timeout=300000")
    try:
        seconds = line_seconds(conn, jobs) if conn is not None else {}
    finally:
        if conn is not None:
            conn.close()

    def seconds_for(subset):
        values = [seconds.get((r["job_id"], r["approved_index"]), 0.0)
                  for r in subset if r["approved_index"] is not None]
        nonzero = [v for v in values if v > 0]
        return {
            "lines": len(values),
            "with_attributed_time": len(nonzero),
            "median_s": round(statistics.median(nonzero), 1) if nonzero else None,
            "mean_s": round(statistics.fmean(nonzero), 1) if nonzero else None,
            "p75_s": round(sorted(nonzero)[int(0.75 * (len(nonzero) - 1))], 1) if nonzero else None,
            "total_min": round(sum(values) / 60, 1),
        }

    categories = {
        "timing_gt_500_without_text": [r for r in rows if r["timing_gt_500"] and not r["text_edit"]],
        "timing_gt_500_with_text": [r for r in rows if r["timing_gt_500"] and r["text_edit"]],
        "text_only": [r for r in rows if r["text_edit"] and not r["timing_edit"]],
        "timing_50_500_only": [r for r in rows if r["timing_edit"] and not r["timing_gt_500"] and not r["text_edit"]],
        "untouched": [r for r in rows if not r["timing_edit"] and not r["text_edit"]],
    }
    report["h_seconds_per_line"] = {name: seconds_for(subset) for name, subset in categories.items()}
    report["h_total_attributed_min"] = round(sum(seconds.values()) / 60, 1)

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
