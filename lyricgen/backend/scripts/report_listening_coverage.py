#!/usr/bin/env python3
"""Cobertura de escucha antes de aprobar: ¿el operador oyó cada línea?

Solo lectura. Cruza el evento ``editor_audio_played`` (tramos del audio que
sonaron de verdad en el editor, pares ``[inicio_ms, fin_ms]``) con la PRIMERA
versión aprobada por un humano de cada canción (``editor_versions`` con
``is_approved`` y ``reason = 'approve'``).

Por canción:
- % de líneas escuchadas: líneas de la versión aprobada cuyo tramo
  ``[start, end]`` quedó cubierto al menos en ``--line-min-fraction`` (50 %
  por defecto) por audio reproducido antes de la aprobación.
- % de líneas tocadas: líneas con cualquier solapamiento.
- % de la canción reproducida: unión de los tramos sobre la duración del
  audio (la que reporta el navegador; si falta, el fin de la última línea).

Sólo cuentan los tramos del operador que aprobó, registrados hasta la
aprobación (más ``--grace-s`` de margen: el envío "approve" sale en el mismo
clic que la aprobación). Se usa ``occurred_at`` (hora del cliente acotada por
el servidor) o, en filas viejas, ``created_at``.

Estado por canción:
- ``measured``: hay tramos del aprobador antes de aprobar.
- ``no_playback``: el aprobador tuvo el editor abierto (heartbeats) pero no
  sonó nada registrado → cuenta como 0 %.
- ``no_editor_activity``: aprobada sin telemetría del editor; fuera de los
  promedios.

Correr con ``--since`` posterior al deploy del evento: antes de eso todas las
canciones salen ``no_playback``. Nunca lee ni imprime texto de la letra: de
cada línea sólo usa ``start``/``end``.

Uso:
    DATABASE_URL=... python3.11 scripts/report_listening_coverage.py \\
        --since 2026-10-09 [--tenant universal_music] [--json | --csv]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

EVENT_NAME = "editor_audio_played"
HEARTBEAT_NAME = "editor_activity_heartbeat"
DEFAULT_LINE_MIN_FRACTION = 0.5
DEFAULT_GRACE_S = 5.0


# ---------------------------------------------------------------------------
# Matemática de cobertura (pura, testeada)
# ---------------------------------------------------------------------------

def merge_intervals(ranges) -> list[tuple[int, int]]:
    """Unión de intervalos ``[inicio_ms, fin_ms]``; descarta los inválidos."""
    clean = []
    for pair in ranges or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = pair
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        clean.append((int(round(start)), int(round(end))))
    clean.sort()
    merged: list[list[int]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def line_spans(segments) -> list[tuple[int, int]]:
    """``(start_ms, end_ms)`` de cada línea con duración; ignora el texto."""
    spans = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        spans.append((int(round(start * 1000)), int(round(end * 1000))))
    return spans


def covered_ms(span: tuple[int, int], merged: list[tuple[int, int]]) -> int:
    """Milisegundos de ``span`` cubiertos por intervalos ya fusionados."""
    start, end = span
    total = 0
    for range_start, range_end in merged:
        if range_end <= start:
            continue
        if range_start >= end:
            break
        total += min(end, range_end) - max(start, range_start)
    return total


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole > 0 else None


def job_coverage(
    segments,
    ranges,
    *,
    audio_duration_ms: int | None = None,
    line_min_fraction: float = DEFAULT_LINE_MIN_FRACTION,
) -> dict:
    """Cobertura de una canción: líneas escuchadas y % de canción reproducida."""
    spans = line_spans(segments)
    merged = merge_intervals(ranges)
    heard = touched = 0
    for span in spans:
        covered = covered_ms(span, merged)
        if covered > 0:
            touched += 1
        if covered >= line_min_fraction * (span[1] - span[0]):
            heard += 1
    duration_ms = audio_duration_ms if audio_duration_ms and audio_duration_ms > 0 else (
        max((end for _, end in spans), default=0)
    )
    played_ms = sum(
        max(0, min(end, duration_ms) - start) for start, end in merged if start < duration_ms
    ) if duration_ms else sum(end - start for start, end in merged)
    return {
        "lines": len(spans),
        "lines_heard": heard,
        "lines_touched": touched,
        "lines_heard_pct": _pct(heard, len(spans)),
        "lines_touched_pct": _pct(touched, len(spans)),
        "played_ms": played_ms,
        "duration_ms": duration_ms or None,
        "song_played_pct": _pct(played_ms, duration_ms) if duration_ms else None,
    }


def summarize_operators(rows: list[dict]) -> list[dict]:
    """Agrega por operador. ``no_editor_activity`` queda fuera de los promedios."""
    by_operator: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_operator[row["operator"]].append(row)
    summary = []
    for operator, items in sorted(by_operator.items()):
        counted = [r for r in items if r["status"] in {"measured", "no_playback"}]
        lines_pct = [r["lines_heard_pct"] for r in counted if r["lines_heard_pct"] is not None]
        song_pct = [r["song_played_pct"] for r in counted if r["song_played_pct"] is not None]
        lines_total = sum(r["lines"] for r in counted)
        summary.append({
            "operator": operator,
            "jobs": len(items),
            "jobs_measured": sum(1 for r in items if r["status"] == "measured"),
            "jobs_no_playback": sum(1 for r in items if r["status"] == "no_playback"),
            "jobs_no_editor_activity": sum(1 for r in items if r["status"] == "no_editor_activity"),
            "median_lines_heard_pct": round(statistics.median(lines_pct), 1) if lines_pct else None,
            "pooled_lines_heard_pct": _pct(sum(r["lines_heard"] for r in counted), lines_total),
            "median_song_played_pct": round(statistics.median(song_pct), 1) if song_pct else None,
            "jobs_lines_heard_ge_90": sum(1 for v in lines_pct if v >= 90),
            "jobs_lines_heard_lt_50": sum(1 for v in lines_pct if v < 50),
        })
    return summary


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Lectura (solo SELECT)
# ---------------------------------------------------------------------------

def collect(db, *, since: datetime, tenant: str, line_min_fraction: float,
            grace_s: float, limit: int) -> list[dict]:
    from database import EditorVersion, Job, ProductEvent, User

    approved_q = (
        db.query(EditorVersion.job_id)
        .filter(EditorVersion.is_approved.is_(True))
        .filter(EditorVersion.reason == "approve")
        .filter(EditorVersion.created_by.isnot(None))
        .filter(EditorVersion.created_at >= since)
    )
    if tenant:
        approved_q = approved_q.filter(EditorVersion.tenant_id == tenant)
    job_ids = sorted({row.job_id for row in approved_q.distinct().limit(limit).all()})
    if not job_ids:
        return []

    # Primera aprobación humana de cada canción (de toda la historia: si fue
    # antes de --since, la canción no entra).
    first_approval: dict[str, object] = {}
    for version in (
        db.query(
            EditorVersion.job_id, EditorVersion.revision, EditorVersion.segments,
            EditorVersion.created_by, EditorVersion.created_at,
        )
        .filter(EditorVersion.job_id.in_(job_ids))
        .filter(EditorVersion.is_approved.is_(True))
        .filter(EditorVersion.reason == "approve")
        .filter(EditorVersion.created_by.isnot(None))
        .order_by(EditorVersion.created_at.asc(), EditorVersion.revision.asc())
        .all()
    ):
        first_approval.setdefault(version.job_id, version)
    first_approval = {
        job_id: version for job_id, version in first_approval.items()
        if _aware(version.created_at) >= since
    }
    if not first_approval:
        return []
    job_ids = sorted(first_approval)

    approver_ids = {version.created_by for version in first_approval.values()}
    usernames = {
        row.id: row.username
        for row in db.query(User.id, User.username).filter(User.id.in_(approver_ids)).all()
    }
    tenants = {
        row.job_id: row.tenant_id
        for row in db.query(Job.job_id, Job.tenant_id).filter(Job.job_id.in_(job_ids)).all()
    }

    ranges: dict[str, list] = defaultdict(list)
    durations: dict[str, int] = {}
    events_counted: dict[str, int] = defaultdict(int)
    heartbeats: dict[str, int] = defaultdict(int)
    for event in (
        db.query(
            ProductEvent.job_id, ProductEvent.user_id, ProductEvent.name,
            ProductEvent.occurred_at, ProductEvent.created_at, ProductEvent.properties,
        )
        .filter(ProductEvent.job_id.in_(job_ids))
        .filter(ProductEvent.name.in_([EVENT_NAME, HEARTBEAT_NAME]))
        .all()
    ):
        approval = first_approval[event.job_id]
        if event.user_id != approval.created_by:
            continue
        when = _aware(event.occurred_at) or _aware(event.created_at)
        if when is None or when > _aware(approval.created_at) + timedelta(seconds=grace_s):
            continue
        if event.name == HEARTBEAT_NAME:
            heartbeats[event.job_id] += 1
            continue
        properties = event.properties or {}
        ranges[event.job_id].extend(properties.get("ranges") or [])
        events_counted[event.job_id] += 1
        duration = properties.get("audio_duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
            durations[event.job_id] = max(durations.get(event.job_id, 0), duration)

    rows = []
    for job_id in job_ids:
        approval = first_approval[job_id]
        coverage = job_coverage(
            approval.segments, ranges.get(job_id, []),
            audio_duration_ms=durations.get(job_id),
            line_min_fraction=line_min_fraction,
        )
        if events_counted.get(job_id):
            status = "measured"
        elif heartbeats.get(job_id):
            status = "no_playback"
        else:
            status = "no_editor_activity"
        rows.append({
            "job_id": job_id,
            "tenant_id": tenants.get(job_id),
            "operator": usernames.get(approval.created_by, f"user:{approval.created_by}"),
            "approved_at": _aware(approval.created_at).isoformat(),
            "approved_revision": approval.revision,
            "status": status,
            "playback_events": events_counted.get(job_id, 0),
            "editor_heartbeats": heartbeats.get(job_id, 0),
            **coverage,
        })
    return rows


def _print_text(summary: list[dict], rows: list[dict]) -> None:
    def fmt(value):
        return "-" if value is None else value

    print("Por operador (no_editor_activity fuera de los promedios)")
    print(f"{'operador':<28}{'jobs':>6}{'med':>6}{'sin':>6}{'s/ed':>6}"
          f"{'med%lín':>9}{'pool%lín':>10}{'med%canc':>10}{'≥90':>6}{'<50':>6}")
    for item in summary:
        print(
            f"{item['operator']:<28}{item['jobs']:>6}{item['jobs_measured']:>6}"
            f"{item['jobs_no_playback']:>6}{item['jobs_no_editor_activity']:>6}"
            f"{fmt(item['median_lines_heard_pct']):>9}{fmt(item['pooled_lines_heard_pct']):>10}"
            f"{fmt(item['median_song_played_pct']):>10}"
            f"{item['jobs_lines_heard_ge_90']:>6}{item['jobs_lines_heard_lt_50']:>6}"
        )
    print()
    print("Por canción")
    print(f"{'job':<14}{'operador':<28}{'estado':<20}{'líneas':>7}{'%oídas':>8}"
          f"{'%tocadas':>9}{'%canción':>9}")
    for row in rows:
        print(
            f"{row['job_id']:<14}{row['operator']:<28}{row['status']:<20}{row['lines']:>7}"
            f"{fmt(row['lines_heard_pct']):>8}{fmt(row['lines_touched_pct']):>9}"
            f"{fmt(row['song_played_pct']):>9}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="", help="ISO date/datetime; por defecto 7 días atrás")
    parser.add_argument("--tenant", default="")
    parser.add_argument("--line-min-fraction", type=float, default=DEFAULT_LINE_MIN_FRACTION)
    parser.add_argument("--grace-s", type=float, default=DEFAULT_GRACE_S)
    parser.add_argument("--limit", type=int, default=2000, help="máximo de canciones")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--csv", action="store_true", help="una fila por canción")
    args = parser.parse_args()

    since = (
        _aware(datetime.fromisoformat(args.since))
        if args.since else datetime.now(timezone.utc) - timedelta(days=7)
    )

    from database import SessionLocal

    db = SessionLocal()
    try:
        rows = collect(
            db, since=since, tenant=args.tenant,
            line_min_fraction=args.line_min_fraction, grace_s=args.grace_s,
            limit=args.limit,
        )
    finally:
        db.rollback()
        db.close()
    summary = summarize_operators(rows)

    if args.json:
        print(json.dumps({
            "since": since.isoformat(),
            "line_min_fraction": args.line_min_fraction,
            "grace_s": args.grace_s,
            "operators": summary,
            "jobs": rows,
        }, ensure_ascii=False, indent=2))
    elif args.csv:
        if rows:
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    else:
        _print_text(summary, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
