#!/usr/bin/env python3
"""Línea de base de revisión humana: minutos, ediciones y adelanto de inicios.

Solo lectura. Abre la base con ``default_transaction_read_only=on`` y no
importa ``database`` (así corre igual contra staging y producción sin cargar
el modelo completo).

Qué mide sobre los últimos N jobs aprobados (por fecha de la PRIMERA versión
``is_approved`` del editor):

1. Minutos activos por job: latidos ``editor_activity_heartbeat`` sumados con
   el mismo criterio que ``report_reviewer_minutes.active_seconds`` (huecos
   <= 25 s, todas las sesiones, por revisor y después sumados por job). Se
   cuentan hasta la primera aprobación; lo posterior se informa aparte.
2. Ediciones por job: diff entre la versión de máquina y la primera versión
   aprobada, con el mismo emparejador de líneas que el AuditLog
   (``training_corpus._match_rows``). Texto, timing, altas, bajas, reorden.
   La versión de máquina es la última ``transcription``/``migration`` previa
   a la primera versión humana: así las revisiones ``migration`` (la
   canonicalización que corre al abrir el editor) no cuentan como ediciones.
   También se informa el diff crudo contra la versión 0.
3. Adelanto de inicios: (a) en la versión de máquina, cuánto antes de su
   primera palabra arranca cada línea (los dos adelantos —WhisperX
   ``LYRIC_LEAD_IN_MS`` y ``lead_in.polish`` con ``LYRIC_LEAD_IN_S``— mueven
   el inicio de la línea y nunca las palabras); (b) cuánto corrió el operador
   el inicio de cada línea emparejada con el mismo texto, separado por
   ``render_params.lyric_transition`` = "fade" y por ``timing_source``.

Uso:
    DATABASE_URL=postgresql://... python3.11 scripts/report_review_baseline.py \\
        --label staging --limit 100 --json-out /tmp/baseline-staging.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import statistics
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from training_corpus import _match_rows, _finite_time, _text  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "report_reviewer_minutes", BACKEND / "scripts" / "report_reviewer_minutes.py",
)
_minutes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_minutes)
active_seconds = _minutes.active_seconds

MACHINE_REASONS = frozenset({"transcription", "migration"})
# Cuenta de lote: no es un revisor (ver campaign_review_history).
AUTOMATION_USERNAMES = frozenset({"batch-universal-staging"})
APPROVAL_SLACK = timedelta(seconds=60)
# Tenants de pruebas automáticas (preflight, golden render, e2e, smoke): sus
# aprobaciones no son revisión humana y en staging son la mayoría.
DEFAULT_EXCLUDE_TENANT_RE = (
    r"^(preflight_|golden_render_bot$|e2e|genly_edit_smoke|__internal|dk_|drain_)"
)

# Copia de main._LIVE_MARKER_RE / main._looks_live; un test verifica que
# coincidan. No se importa main porque arrastra la app entera y su entorno.
_LIVE_MARKER_RE = re.compile(
    r"\b(live|en\s+vivo|vivo|ac[uú]stic[oa]|unplugged|directo|concert|"
    r"session(?:es)?|sesi[oó]n)\b", re.IGNORECASE)


def looks_live(*texts) -> bool:
    return any(
        t and _LIVE_MARKER_RE.search(re.sub(r"[_-]+", " ", str(t)))
        for t in texts
    )


def live_class(transcription_quality, *titles) -> tuple[bool, str]:
    """(es_vivo, origen): ``metric`` si el job lo guardó, ``inferred`` si no."""
    metrics = (transcription_quality or {}).get("metrics") or {}
    flag = metrics.get("is_live") if isinstance(metrics, dict) else None
    if flag is not None:
        return bool(flag), "metric"
    return bool(looks_live(*titles)), "inferred"


def pick_versions(versions: list[dict]) -> dict:
    """Elige v0, la versión de máquina y la primera aprobada.

    ``versions``: dicts con revision, reason, is_approved, created_at,
    segments; en cualquier orden.
    """
    ordered = sorted(versions, key=lambda v: (v["revision"], v["created_at"]))
    v0 = next((v for v in ordered if v["reason"] == "transcription"), None)
    approved = next(
        (v for v in sorted(ordered, key=lambda v: v["created_at"]) if v["is_approved"]),
        None,
    )
    first_human = next(
        (v for v in ordered if v["reason"] not in MACHINE_REASONS), None,
    )
    limit = first_human["revision"] if first_human else float("inf")
    machine_candidates = [
        v for v in ordered
        if v["reason"] in MACHINE_REASONS and v["revision"] < limit
    ]
    machine = machine_candidates[-1] if machine_candidates else None
    migrations = [v for v in ordered if v["reason"] == "migration"]
    return {
        "v0": v0,
        "machine": machine,
        "approved": approved,
        "migrations_total": len(migrations),
        "migrations_after_human": sum(
            1 for v in migrations if first_human and v["revision"] > first_human["revision"]
        ),
    }


def line_edits(before, after, *, timing_threshold_s: float) -> dict:
    """Cuenta líneas editadas entre dos snapshots de segmentos."""
    before = [dict(r) for r in (before or []) if isinstance(r, dict)]
    after = [dict(r) for r in (after or []) if isinstance(r, dict)]
    matched, removed, inserted, complete = _match_rows(before, after)
    before_rank = {pair: i for i, pair in enumerate(sorted((b, a) for b, a, _ in matched))}
    after_rank = {
        pair: i for i, pair in enumerate(sorted(((b, a) for b, a, _ in matched), key=lambda p: p[1]))
    }
    text = timing = start = end = reorder = 0
    for b, a, _row_id in matched:
        old, new = before[b], after[a]
        text_changed = _text(old.get("text")) != _text(new.get("text"))
        start_changed = abs(_finite_time(new.get("start")) - _finite_time(old.get("start"))) >= timing_threshold_s
        end_changed = abs(_finite_time(new.get("end")) - _finite_time(old.get("end"))) >= timing_threshold_s
        text += text_changed
        start += start_changed
        end += end_changed
        timing += start_changed or end_changed
        reorder += before_rank[(b, a)] != after_rank[(b, a)]
    return {
        "complete": bool(complete),
        "lines_before": len(before),
        "lines_after": len(after),
        "text": text,
        "timing": timing,
        "start": start,
        "end": end,
        "inserted": len(inserted),
        "deleted": len(removed),
        "reordered": reorder,
        "any": bool(text or timing or inserted or removed or reorder),
    }


def boundary_shifts(before, after, key: str = "start") -> list[float]:
    """Corrimiento de ``start``/``end`` (después − antes, s) de líneas con el mismo texto."""
    before = [dict(r) for r in (before or []) if isinstance(r, dict)]
    after = [dict(r) for r in (after or []) if isinstance(r, dict)]
    matched, _removed, _inserted, complete = _match_rows(before, after)
    if not complete:
        return []
    return [
        round(_finite_time(after[a].get(key)) - _finite_time(before[b].get(key)), 4)
        for b, a, _ in matched
        if _text(before[b].get("text")) == _text(after[a].get("text"))
    ]


def start_shifts(before, after) -> list[float]:
    return boundary_shifts(before, after, "start")


def word_leads(segments) -> list[float]:
    """Por línea con palabras: primera palabra − inicio de línea (s)."""
    leads = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        words = [w for w in (seg.get("words") or []) if isinstance(w, dict) and w.get("start") is not None]
        if not words:
            continue
        first = min(_finite_time(w.get("start")) for w in words)
        leads.append(round(first - _finite_time(seg.get("start")), 4))
    return leads


def distribution(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pct(fraction):
        return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]

    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 4),
        "p10": pct(0.10), "p25": pct(0.25), "median": pct(0.50),
        "p75": pct(0.75), "p90": pct(0.90),
    }


LEAD_BINS = (-1e9, -0.005, 0.005, 0.03, 0.06, 0.10, 0.14, 0.18, 0.25, 0.5, 1e9)
LEAD_LABELS = (
    "<0 (palabra antes de la línea)", "≈0", "0,005–0,03", "0,03–0,06",
    "0,06–0,10", "0,10–0,14", "0,14–0,18", "0,18–0,25", "0,25–0,5", "≥0,5",
)


END_BINS = (-1e9, -1.0, -0.5, -0.3, -0.15, -0.05, 0.05, 0.15, 0.3, 0.5, 1.0, 1e9)
END_LABELS = (
    "≤−1", "−1…−0,5", "−0,5…−0,3", "−0,3…−0,15", "−0,15…−0,05", "≈0",
    "0,05…0,15", "0,15…0,3", "0,3…0,5", "0,5…1", "≥1",
)


def histogram(values: list[float], bins=LEAD_BINS, labels=LEAD_LABELS) -> dict:
    counts = Counter()
    for value in values:
        for low, high, label in zip(bins, bins[1:], labels):
            if low <= value < high:
                counts[label] += 1
                break
    total = len(values) or 1
    return {label: {"n": counts[label], "share": round(counts[label] / total, 3)} for label in labels}


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def summarize_jobs(rows: list[dict]) -> dict:
    timed = [r for r in rows if r["heartbeats"] > 0]
    minutes = [r["active_minutes"] for r in timed]
    timed_total = [r for r in rows if r["heartbeats"] or r["post_approval_minutes"]]
    comparable = [r for r in rows if r["edits"] and r["edits"]["complete"]]
    lines = sum(r["edits"]["lines_after"] for r in comparable) or 1

    def share(predicate):
        return round(sum(1 for r in comparable if predicate(r)) / len(comparable), 3) if comparable else None

    return {
        "jobs": len(rows),
        "jobs_with_heartbeats": len(timed),
        "median_minutes": round(statistics.median(minutes), 2) if minutes else None,
        "p90_minutes": round(_percentile(minutes, 0.9), 2) if minutes else None,
        "median_minutes_per_line": (
            round(statistics.median(r["active_minutes"] / r["lines"] for r in timed if r["lines"]), 3)
            if any(r["lines"] for r in timed) else None
        ),
        "post_approval_minutes_total": round(sum(r["post_approval_minutes"] for r in rows), 1),
        # Antes + después de la primera aprobación. En flujos donde el cliente
        # aprueba primero y el operador corrige después (prod Chile, ago-sep)
        # la revisión real queda casi toda después de la aprobación.
        "median_total_minutes": (
            round(statistics.median(r["active_minutes"] + r["post_approval_minutes"] for r in timed_total), 2)
            if timed_total else None
        ),
        "p90_total_minutes": (
            round(_percentile([r["active_minutes"] + r["post_approval_minutes"] for r in timed_total], 0.9), 2)
            if timed_total else None
        ),
        "jobs_comparable": len(comparable),
        "zero_edit_share": share(lambda r: not r["edits"]["any"]),
        "zero_edit_share_raw_v0": (
            round(sum(1 for r in comparable if r["edits_raw_v0"] and not r["edits_raw_v0"]["any"]) / len(comparable), 3)
            if comparable else None
        ),
        "zero_text_edit_share": share(lambda r: r["edits"]["text"] == 0 and not r["edits"]["inserted"] and not r["edits"]["deleted"]),
        "zero_timing_edit_share": share(lambda r: r["edits"]["timing"] == 0),
        "edited_lines_share": {
            key: round(sum(r["edits"][key] for r in comparable) / lines, 3)
            for key in ("text", "timing", "start", "end", "inserted", "deleted", "reordered")
        },
        "median_edits_per_job": {
            key: statistics.median(r["edits"][key] for r in comparable) if comparable else None
            for key in ("text", "timing", "inserted", "deleted")
        },
        "jobs_with_migration": sum(1 for r in rows if r["migrations_total"]),
        "jobs_with_migration_after_human": sum(1 for r in rows if r["migrations_after_human"]),
    }


def collect(conn, *, limit: int, timing_threshold_s: float,
            tenants: list[str] | None = None,
            exclude_tenant_re: str = DEFAULT_EXCLUDE_TENANT_RE) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        select v.job_id, min(v.created_at) as first_approved_at
          from editor_versions v join jobs j on j.job_id = v.job_id
         where v.is_approved
           and (%s::text[] is null or j.tenant_id = any(%s::text[]))
           and (%s = '' or j.tenant_id !~ %s)
         group by v.job_id order by first_approved_at desc limit %s
        """,
        (tenants or None, tenants or None, exclude_tenant_re, exclude_tenant_re, limit),
    )
    approvals = {job_id: at for job_id, at in cur.fetchall()}
    job_ids = list(approvals)
    if not job_ids:
        return {"rows": [], "leads": {}, "shifts": {}, "end_shifts": {}}

    cur.execute(
        """
        select j.job_id, j.tenant_id, j.filename, j.song_title, j.artist,
               j.timing_source, j.transcription_quality,
               j.render_params->>'lyric_transition', j.workload_class
          from jobs j where j.job_id = any(%s)
        """,
        (job_ids,),
    )
    jobs = {row[0]: row for row in cur.fetchall()}

    cur.execute(
        """
        select v.job_id, v.revision, v.reason, v.is_approved, v.created_at,
               v.segments, u.username
          from editor_versions v left join users u on u.id = v.created_by
         where v.job_id = any(%s)
        """,
        (job_ids,),
    )
    versions: dict[str, list[dict]] = defaultdict(list)
    for job_id, revision, reason, approved, created_at, segments, username in cur.fetchall():
        versions[job_id].append({
            "revision": revision, "reason": reason, "is_approved": approved,
            "created_at": created_at, "segments": segments, "username": username,
        })

    cur.execute(
        """
        select job_id, user_id, coalesce(occurred_at, created_at)
          from product_events
         where name = 'editor_activity_heartbeat' and job_id = any(%s)
        """,
        (job_ids,),
    )
    beats: dict[str, dict[int | None, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    for job_id, user_id, when in cur.fetchall():
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        beats[job_id][user_id].append(when)

    rows = []
    leads_by_source: dict[str, list[float]] = defaultdict(list)
    shifts: dict[str, list[float]] = defaultdict(list)
    end_shifts: dict[str, list[float]] = defaultdict(list)
    for job_id in job_ids:
        job = jobs.get(job_id)
        if job is None:
            continue
        (_, tenant, filename, title, artist, timing_source, quality,
         transition, workload) = job
        chosen = pick_versions(versions.get(job_id, []))
        cutoff = approvals[job_id] + APPROVAL_SLACK
        before_s = after_s = 0.0
        heartbeat_count = 0
        for stamps in beats.get(job_id, {}).values():
            pre = [t for t in stamps if t <= cutoff]
            post = [t for t in stamps if t > cutoff]
            heartbeat_count += len(pre)
            before_s += active_seconds(pre, 25.0) if pre else 0.0
            after_s += active_seconds(post, 25.0) if post else 0.0
        live, live_source = live_class(quality, title, filename)
        machine, approved, v0 = chosen["machine"], chosen["approved"], chosen["v0"]
        edits = edits_raw = None
        if machine and approved:
            edits = line_edits(machine["segments"], approved["segments"], timing_threshold_s=timing_threshold_s)
        if v0 and approved:
            edits_raw = line_edits(v0["segments"], approved["segments"], timing_threshold_s=timing_threshold_s)
        source = str(timing_source or "unknown")
        fade = "fade" if transition == "fade" else "no_fade"
        if machine:
            leads_by_source[source].extend(word_leads(machine["segments"]))
        if machine and approved:
            moved = start_shifts(machine["segments"], approved["segments"])
            shifts[f"{fade}|all"].extend(moved)
            shifts[f"{fade}|{source}"].extend(moved)
            end_shifts["all"].extend(boundary_shifts(machine["segments"], approved["segments"], "end"))
        rows.append({
            "job_id": job_id,
            "tenant": tenant,
            "workload": workload,
            "title": title or filename,
            "live": live,
            "live_source": live_source,
            "timing_source": source,
            "lyric_transition": transition,
            "approved_at": approvals[job_id].isoformat(),
            "approved_by": approved["username"] if approved else None,
            "approved_by_automation": bool(approved and str(approved["username"] or "").lower() in AUTOMATION_USERNAMES),
            "heartbeats": heartbeat_count,
            "active_minutes": round(before_s / 60.0, 2),
            "post_approval_minutes": round(after_s / 60.0, 2),
            "lines": len(approved["segments"] or []) if approved else 0,
            "has_transcription_v0": v0 is not None,
            "machine_reason": machine["reason"] if machine else None,
            "migrations_total": chosen["migrations_total"],
            "migrations_after_human": chosen["migrations_after_human"],
            "edits": edits,
            "edits_raw_v0": edits_raw,
        })
    return {"rows": rows, "leads": leads_by_source, "shifts": shifts, "end_shifts": end_shifts}


def build_report(collected: dict, *, label: str, timing_threshold_s: float) -> dict:
    rows = collected["rows"]
    by_live = {
        "studio": [r for r in rows if not r["live"]],
        "live": [r for r in rows if r["live"]],
    }
    all_leads = [v for values in collected["leads"].values() for v in values]
    return {
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timing_threshold_s": timing_threshold_s,
        "window": {
            "first_approved_at_min": min((r["approved_at"] for r in rows), default=None),
            "first_approved_at_max": max((r["approved_at"] for r in rows), default=None),
        },
        "live_source_counts": dict(Counter(r["live_source"] for r in rows)),
        "approved_by_automation": sum(1 for r in rows if r["approved_by_automation"]),
        "tenants": dict(Counter(str(r["tenant"]) for r in rows)),
        "all": summarize_jobs(rows),
        "studio": summarize_jobs(by_live["studio"]),
        "live": summarize_jobs(by_live["live"]),
        "lead": {
            "machine_word_lead_all": {**distribution(all_leads), "histogram": histogram(all_leads)},
            "machine_word_lead_by_timing_source": {
                source: {**distribution(values), "histogram": histogram(values)}
                for source, values in sorted(collected["leads"].items())
            },
            "operator_start_shift": {
                key: {
                    **distribution(values),
                    "moved_share": round(sum(1 for v in values if abs(v) >= timing_threshold_s) / len(values), 3) if values else None,
                    "moved_only": distribution([v for v in values if abs(v) >= timing_threshold_s]),
                }
                for key, values in sorted(collected["shifts"].items())
            },
            "operator_end_shift": {
                key: {
                    **distribution(values),
                    "moved_share": round(sum(1 for v in values if abs(v) >= timing_threshold_s) / len(values), 3) if values else None,
                    "moved_only": distribution([v for v in values if abs(v) >= timing_threshold_s]),
                    "moved_only_histogram": histogram(
                        [v for v in values if abs(v) >= timing_threshold_s],
                        bins=END_BINS, labels=END_LABELS,
                    ),
                }
                for key, values in sorted(collected.get("end_shifts", {}).items())
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default=os.environ.get("BASELINE_LABEL", "db"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[], help="repetible; vacío = todos")
    parser.add_argument("--exclude-tenant-regex", default=DEFAULT_EXCLUDE_TENANT_RE,
                        help="'' para no excluir")
    parser.add_argument("--timing-threshold-s", type=float, default=0.05)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--rows-out", default="", help="JSONL por job (incluye títulos: no commitear)")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL requerido", file=sys.stderr)
        return 2
    import psycopg2

    conn = psycopg2.connect(url, options="-c default_transaction_read_only=on -c statement_timeout=300000")
    try:
        collected = collect(
            conn, limit=args.limit, timing_threshold_s=args.timing_threshold_s,
            tenants=args.tenant, exclude_tenant_re=args.exclude_tenant_regex,
        )
    finally:
        conn.close()
    report = build_report(collected, label=args.label, timing_threshold_s=args.timing_threshold_s)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    if args.rows_out:
        with open(args.rows_out, "w") as handle:
            for row in collected["rows"]:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
