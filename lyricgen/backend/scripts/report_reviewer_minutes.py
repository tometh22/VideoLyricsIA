#!/usr/bin/env python3
"""Minutos de revisor por canción, agregados sobre TODAS las sesiones.

Métrica norte del dashboard. Se calcula sobre los latidos que el editor ya
emite (``editor_activity_heartbeat``, uno cada 15 s mientras hay actividad y
con corte si no hubo input en 60 s), sin telemetría nueva y de forma
retroactiva sobre todo el histórico.

Por qué existe, y por qué NO usa ``correction_observations.active_edit_ms``:
esa derivación (``correction_learning.derive_server_active_edit_ms``) filtra a
una sola ``session_id`` y exige que los ``activity_seq`` aceptados sean
exactamente 1..N contiguos. Un revisor que recarga la página, reabre el editor
o vuelve al día siguiente genera varias sesiones, y entonces la derivación tira
casi todo. Medido en producción el 2026-09-02: una canción con 183 latidos
repartidos en 5 sesiones (~46 min de trabajo) quedó registrada como 9,0 s, y
otra con 3 latidos en 2 sesiones como 3,2 s. Sub-reporta por dos a tres órdenes
de magnitud, así que no sirve como métrica norte.

Acá se suman los huecos entre latidos consecutivos del mismo revisor sobre la
misma canción, sin importar la sesión, contando sólo huecos ``<= max-gap``
(por defecto 25 s, apenas por encima del intervalo de 15 s del editor). Cada
latido aislado aporta el intervalo nominal. Es una cota inferior del tiempo
activo: no cuenta pensar con el editor cerrado.

Uso:
    python3.11 scripts/report_reviewer_minutes.py --since 2026-09-01 --csv
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HEARTBEAT_INTERVAL_S = 15.0

# El flag is_live sólo existe en releases nuevos; en producción hay que caer al
# nombre del archivo o el corte estudio/vivo queda vacío y la métrica norte
# pierde justo la dimensión que importa.
_LIVE_RE = re.compile(r"\b(live|en vivo|en directo|unplugged|ac[uú]stico)\b", re.I)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def active_seconds(timestamps: list[datetime], max_gap_s: float) -> float:
    """Suma de huecos <= max_gap entre latidos consecutivos, sin importar sesión."""
    if not timestamps:
        return 0.0
    ordered = sorted(timestamps)
    total = 0.0
    for left, right in zip(ordered, ordered[1:]):
        gap = (right - left).total_seconds()
        if 0 < gap <= max_gap_s:
            total += gap
    # Un único latido (o latidos todos separados) igual representa actividad:
    # se le acredita un intervalo nominal, nunca más.
    return total if total > 0 else HEARTBEAT_INTERVAL_S


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="", help="ISO date/datetime; por defecto 30 días atrás")
    parser.add_argument("--tenant", default="")
    parser.add_argument("--max-gap-s", type=float, default=25.0)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.since else datetime.now(timezone.utc) - timedelta(days=30)
    )

    from database import Job, ProductEvent, SessionLocal

    db = SessionLocal()
    try:
        events = (
            db.query(ProductEvent)
            .filter(ProductEvent.name == "editor_activity_heartbeat")
            .filter(ProductEvent.created_at >= since)
            .order_by(ProductEvent.created_at.asc())
            .limit(200_000)
            .all()
        )
        by_job: dict[tuple[str, int | None], list[datetime]] = defaultdict(list)
        sessions: dict[str, set[str]] = defaultdict(set)
        for event in events:
            job_id = str(event.job_id or "")
            if not job_id:
                continue
            when = event.occurred_at or event.created_at
            if when is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            by_job[(job_id, event.user_id)].append(when.astimezone(timezone.utc))
            sessions[job_id].add(str((event.properties or {}).get("session_id") or ""))

        job_ids = sorted({key[0] for key in by_job})
        # Se seleccionan columnas sueltas a propósito: cargar la entidad Job
        # completa rompe contra bases con un esquema anterior al del modelo
        # (producción corre un release más viejo que staging).
        jobs = {
            row.job_id: row
            for row in db.query(
                Job.job_id, Job.tenant_id, Job.filename,
                Job.segments_json, Job.transcription_quality,
            ).filter(Job.job_id.in_(job_ids)).all()
        } if job_ids else {}

        rows = []
        for (job_id, user_id), stamps in by_job.items():
            job = jobs.get(job_id)
            if job is None:
                continue
            if args.tenant and str(job.tenant_id or "") != args.tenant:
                continue
            quality = job.transcription_quality or {}
            live_flag = (quality.get("metrics") or {}).get("is_live")
            live = (
                bool(live_flag) if live_flag is not None
                else bool(_LIVE_RE.search(str(job.filename or "")))
            )
            seconds = active_seconds(stamps, args.max_gap_s)
            segments = job.segments_json if isinstance(job.segments_json, list) else []
            rows.append({
                "job_id": job_id,
                "tenant": str(job.tenant_id or ""),
                "filename": (job.filename or "")[:52],
                "reviewer_user_id": user_id,
                "live": live,
                "lines": len(segments),
                "heartbeats": len(stamps),
                "sessions": len(sessions.get(job_id, set())),
                "active_minutes": round(seconds / 60.0, 2),
                "span_minutes": round(
                    (max(stamps) - min(stamps)).total_seconds() / 60.0, 2,
                ),
                "minutes_per_line": (
                    round(seconds / 60.0 / len(segments), 3) if segments else None
                ),
            })
    finally:
        db.close()

    rows.sort(key=lambda row: -row["active_minutes"])
    rows = rows[: args.limit]

    def aggregate(subset: list[dict]) -> dict:
        minutes = [row["active_minutes"] for row in subset]
        return {
            "songs": len(subset),
            "median_minutes": round(statistics.median(minutes), 2) if minutes else None,
            "p90_minutes": round(_percentile(minutes, 0.9), 2) if minutes else None,
            "total_minutes": round(sum(minutes), 1),
        }

    summary = {
        "since": since.isoformat(),
        "max_gap_s": args.max_gap_s,
        "all": aggregate(rows),
        "studio": aggregate([row for row in rows if not row["live"]]),
        "live": aggregate([row for row in rows if row["live"]]),
    }

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys())) if rows else None
        if writer:
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    elif args.json:
        print(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            kind = "vivo  " if row["live"] else "estudio"
            print(
                f"{row['active_minutes']:7.2f} min  {kind}  lineas={row['lines']:3}  "
                f"latidos={row['heartbeats']:4}  sesiones={row['sessions']:2}  "
                f"{row['job_id']}  {row['filename']}"
            )
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
