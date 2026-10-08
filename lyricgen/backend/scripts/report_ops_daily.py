#!/usr/bin/env python3
"""Reporte operativo diario de las campañas UMG (cierre del día, hora ART).

Solo lectura: abre las dos bases con ``default_transaction_read_only=on`` y
no importa ``database``.

- Base de trabajo (``STAGING_DATABASE_URL`` o ``DATABASE_URL``): jobs,
  ítems, versiones del editor, eventos del editor y lotes de entrega.
- Base de entregas (``DELIVERIES_DATABASE_URL``): las entregas del portal y
  los pedidos de cambio, que viven en la base de producción.

Qué informa:

1. Jobs por estado, con la edad del más viejo en cada uno:
   - letra lista sin abrir (ningún humano abrió el editor);
   - abiertos sin aprobar;
   - aprobados sin entregar al portal;
   - entregados sin respuesta de UMG;
   - con pedidos de cambio abiertos.
2. Aprobaciones de letra del día por operador y acumulado de la semana
   (lunes a domingo, ART) contra la meta.
3. Mediana de días upload → entregado de los jobs entregados en la semana.
4. Alertas:
   - backlog sin abrir > 40;
   - algún job sin abrir hace más de 3 días;
   - cero aprobaciones en un día hábil (lunes a viernes, fuera de ``OPS_HOLIDAYS``).

Uso:
    STAGING_DATABASE_URL=... DELIVERIES_DATABASE_URL=... \\
        python3.11 scripts/report_ops_daily.py [--date 2026-10-07] [--json-out f] [--html-out f] [--send]

``--send`` manda el HTML por Resend (``RESEND_API_KEY``, ``RESEND_FROM``,
``OPS_REPORT_EMAIL`` o, si falta, ``ALERT_EMAIL``), igual que el smoke diario.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import html
import json
import os
import statistics
import sys

ART = timezone(timedelta(hours=-3))
UMG_TENANTS = ("universal_music",)
# Cuentas de servicio: abren el editor y aprueban por API, no son revisión.
SERVICE_USERNAMES = ("batch-universal-staging",)
WEEKLY_GOAL = 100
BACKLOG_ALERT = 40
UNOPENED_MAX_DAYS = 3
# Días hábiles que no se trabajan en oct–dic 2026 (la tabla `holidays` de la
# base está vacía; se puede reemplazar con OPS_HOLIDAYS). Verificado el 7-oct:
# - 12-oct: Diversidad Cultural (Ley 27.399).
# - 9-nov: feriado nacional por la visita papal (Decreto 1103/2026, art. 2).
# - 23-nov: Soberanía Nacional, trasladado del viernes 20 (Ley 27.399).
# - 7-dic: día no laborable con fines turísticos (Resolución JGM 164/2025).
# - 8-dic y 25-dic: inamovibles.
DEFAULT_HOLIDAYS = ("2026-10-12", "2026-11-09", "2026-11-23", "2026-12-07", "2026-12-08", "2026-12-25")

STATES = (
    ("unopened", "Letra lista, sin abrir"),
    ("opened", "Abiertos, sin aprobar"),
    ("approved", "Aprobados, sin entregar"),
    ("delivered", "Entregados, sin respuesta de UMG"),
    ("change_request", "Con pedidos de cambio abiertos"),
)


def _connect(url: str):
    import psycopg2

    return psycopg2.connect(url, options="-c default_transaction_read_only=on -c statement_timeout=120000")


def _holidays() -> set[date]:
    raw = os.environ.get("OPS_HOLIDAYS")
    values = raw.split(",") if raw is not None else DEFAULT_HOLIDAYS
    return {date.fromisoformat(v.strip()) for v in values if v.strip()}


def business_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def fetch(work, deliveries) -> dict:
    """Todo lo que el reporte necesita, por job, en un dict serializable."""
    cur = work.cursor()
    cur.execute(
        """
        select j.job_id, j.status, j.campaign_id, c.name, i.uploaded_at, i.title, i.artist
          from jobs j
          join batch_campaigns c on c.id = j.campaign_id
          join batch_campaign_items i on i.id = j.campaign_item_id
         where j.tenant_id = any(%s) and c.status <> 'cancelled' and j.status <> 'discarded'
        """,
        (list(UMG_TENANTS),),
    )
    jobs = {
        row[0]: {"job_id": row[0], "status": row[1], "campaign_id": row[2], "campaign": row[3],
                 "uploaded_at": row[4], "title": row[5], "artist": row[6]}
        for row in cur.fetchall()
    }
    ids = list(jobs)
    if not ids:
        return {"jobs": jobs, "approvals": []}

    cur.execute("select job_id, min(created_at) from editor_versions where job_id = any(%s) group by 1", (ids,))
    for job_id, value in cur.fetchall():
        jobs[job_id]["lyrics_ready_at"] = value
    cur.execute(
        """
        select e.job_id, min(e.occurred_at) from product_events e join users u on u.id = e.user_id
         where e.job_id = any(%s) and e.name in ('editor_opened', 'editor_activity_heartbeat')
           and u.username <> all(%s)
         group by 1
        """,
        (ids, list(SERVICE_USERNAMES)),
    )
    for job_id, value in cur.fetchall():
        jobs[job_id]["opened_at"] = value
    cur.execute(
        """
        select distinct on (v.job_id) v.job_id, v.created_at, coalesce(u.username, '(sin usuario)')
          from editor_versions v left join users u on u.id = v.created_by
         where v.job_id = any(%s) and v.is_approved
         order by v.job_id, v.created_at
        """,
        (ids,),
    )
    approvals = []
    for job_id, at, username in cur.fetchall():
        jobs[job_id]["approved_at"] = at
        approvals.append({"job_id": job_id, "at": at, "operator": username})
    cur.execute(
        """
        select job_id, min(updated_at) from delivery_batch_items
         where job_id = any(%s) and status = 'sent' group by 1
        """,
        (ids,),
    )
    for job_id, value in cur.fetchall():
        jobs[job_id]["delivered_at"] = value
    # Regeneraciones: el job entregado puede ser un hijo del job de la campaña.
    cur.execute("select job_id, parent_job_id from jobs where parent_job_id = any(%s)", (ids,))
    children = dict(cur.fetchall())

    dcur = deliveries.cursor()
    portal_ids = ids + list(children)
    dcur.execute(
        """
        select d.job_id, min(d.added_at), min(d.approved_at),
               min(r.submitted_at) filter (where r.resolved_at is null),
               count(r.id) filter (where r.resolved_at is null)
          from deliveries d left join delivery_change_requests r on r.delivery_id = d.id
         where d.job_id = any(%s) and d.removed_at is null
         group by 1
        """,
        (portal_ids,),
    )
    for job_id, added, approved, open_since, open_count in dcur.fetchall():
        target = jobs.get(job_id) or jobs.get(children.get(job_id, ""))
        if target is None:
            continue
        if approved and (not target.get("client_approved_at") or approved < target["client_approved_at"]):
            target["client_approved_at"] = approved
        if open_count:
            target["open_change_requests"] = target.get("open_change_requests", 0) + open_count
            since = target.get("change_request_since")
            target["change_request_since"] = min(since, open_since) if since else open_since
        if added and not target.get("delivered_at"):
            target["delivered_at"] = added
    return {"jobs": jobs, "approvals": approvals}


def classify(job: dict) -> tuple[str | None, datetime | None]:
    """Estado operativo del job y desde cuándo está en él."""
    if job.get("open_change_requests"):
        return "change_request", job.get("change_request_since")
    if job.get("client_approved_at"):
        return None, None
    if job.get("delivered_at"):
        return "delivered", job["delivered_at"]
    if job.get("approved_at"):
        return "approved", job["approved_at"]
    if not job.get("lyrics_ready_at"):
        return None, None
    if job.get("opened_at"):
        return "opened", job["opened_at"]
    return "unopened", job["lyrics_ready_at"]


def build_report(data: dict, report_day: date, *, holidays: set[date]) -> dict:
    day_start = datetime.combine(report_day, time.min, ART)
    day_end = day_start + timedelta(days=1)
    week_start = day_start - timedelta(days=report_day.weekday())
    cutoff = min(day_end, datetime.now(ART))

    states = {key: {"count": 0, "oldest_days": None, "oldest_job": None} for key, _ in STATES}
    unopened_over = []
    for job in data["jobs"].values():
        state, since = classify(job)
        if state is None or since is None or since >= cutoff:
            continue
        age = (cutoff - since).total_seconds() / 86400
        bucket = states[state]
        bucket["count"] += 1
        if bucket["oldest_days"] is None or age > bucket["oldest_days"]:
            bucket["oldest_days"], bucket["oldest_job"] = round(age, 1), job["job_id"]
        if state == "unopened" and age > UNOPENED_MAX_DAYS:
            unopened_over.append(job["job_id"])

    today = Counter(a["operator"] for a in data["approvals"] if day_start <= a["at"] < day_end)
    week = Counter(a["operator"] for a in data["approvals"] if week_start <= a["at"] < day_end)

    delivered_week = [
        (job["delivered_at"] - job["uploaded_at"]).total_seconds() / 86400
        for job in data["jobs"].values()
        if job.get("delivered_at") and job.get("uploaded_at") and week_start <= job["delivered_at"] < day_end
    ]

    alerts = []
    if states["unopened"]["count"] > BACKLOG_ALERT:
        alerts.append(f"Backlog sin abrir: {states['unopened']['count']} jobs (umbral {BACKLOG_ALERT}).")
    if unopened_over:
        alerts.append(
            f"{len(unopened_over)} jobs sin abrir hace más de {UNOPENED_MAX_DAYS} días "
            f"(el más viejo: {states['unopened']['oldest_days']} días)."
        )
    if business_day(report_day, holidays) and not today:
        alerts.append("Cero aprobaciones en un día hábil.")

    return {
        "date": report_day.isoformat(),
        "generated_at": datetime.now(ART).isoformat(timespec="minutes"),
        "states": states,
        "approvals_today": dict(today.most_common()),
        "approvals_today_total": sum(today.values()),
        "approvals_week": dict(week.most_common()),
        "approvals_week_total": sum(week.values()),
        "weekly_goal": WEEKLY_GOAL,
        "week_start": week_start.date().isoformat(),
        "delivered_week": len(delivered_week),
        "upload_to_delivered_median_days": round(statistics.median(delivered_week), 1) if delivered_week else None,
        "alerts": alerts,
    }


def render_html(report: dict) -> str:
    esc = html.escape
    state_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{esc(label)}</td>"
        f"<td style='padding:4px 10px;text-align:right'>{report['states'][key]['count']}</td>"
        f"<td style='padding:4px 10px;text-align:right'>"
        f"{'' if report['states'][key]['oldest_days'] is None else report['states'][key]['oldest_days']}</td></tr>"
        for key, label in STATES
    )
    operators = sorted(set(report["approvals_week"]) | set(report["approvals_today"]))
    approval_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{esc(op)}</td>"
        f"<td style='padding:4px 10px;text-align:right'>{report['approvals_today'].get(op, 0)}</td>"
        f"<td style='padding:4px 10px;text-align:right'>{report['approvals_week'].get(op, 0)}</td></tr>"
        for op in operators
    ) or "<tr><td style='padding:4px 10px' colspan='3'>Sin aprobaciones esta semana</td></tr>"
    alerts = (
        "<div style='background:#fdecea;border:1px solid #e34948;padding:10px 14px;border-radius:6px'>"
        "<strong>Alertas</strong><ul style='margin:6px 0 0 18px;padding:0'>"
        + "".join(f"<li>{esc(a)}</li>" for a in report["alerts"]) + "</ul></div>"
        if report["alerts"] else "<p>Sin alertas.</p>"
    )
    median = report["upload_to_delivered_median_days"]
    return (
        "<div style='font-family:system-ui;color:#1a1a19;max-width:640px'>"
        f"<h2 style='margin-bottom:4px'>Operación UMG — {esc(report['date'])}</h2>"
        f"<p style='color:#6b6b66;margin-top:0'>Cierre del día (ART). Generado {esc(report['generated_at'])}.</p>"
        f"{alerts}"
        "<h3>Jobs por estado</h3><table style='border-collapse:collapse'>"
        "<tr style='background:#f3f3f0'><th style='padding:4px 10px;text-align:left'>Estado</th>"
        "<th style='padding:4px 10px'>Jobs</th><th style='padding:4px 10px'>Más viejo (días)</th></tr>"
        f"{state_rows}</table>"
        "<h3>Aprobaciones de letra</h3><table style='border-collapse:collapse'>"
        "<tr style='background:#f3f3f0'><th style='padding:4px 10px;text-align:left'>Operador</th>"
        "<th style='padding:4px 10px'>Hoy</th><th style='padding:4px 10px'>Semana</th></tr>"
        f"{approval_rows}"
        f"<tr style='border-top:1px solid #ccc'><td style='padding:4px 10px'><strong>Total</strong></td>"
        f"<td style='padding:4px 10px;text-align:right'><strong>{report['approvals_today_total']}</strong></td>"
        f"<td style='padding:4px 10px;text-align:right'><strong>{report['approvals_week_total']} / {report['weekly_goal']}</strong></td></tr>"
        "</table>"
        f"<p>Semana desde el {esc(report['week_start'])}: {report['delivered_week']} entregados, mediana upload → entregado "
        f"{'—' if median is None else f'{median} días'}.</p>"
        "<p style='color:#8a8984;font-size:12px'>Solo lectura sobre la base de trabajo y la de entregas. "
        "Script: <code>lyricgen/backend/scripts/report_ops_daily.py</code>.</p></div>"
    )


def send(report: dict, html_body: str) -> bool:
    import requests

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to = (os.environ.get("OPS_REPORT_EMAIL") or os.environ.get("ALERT_EMAIL") or "").strip()
    sender = os.environ.get("RESEND_FROM", "noreply@genly.pro").strip()
    if not (api_key and to):
        print("[ops] mail NO enviado: faltan RESEND_API_KEY y OPS_REPORT_EMAIL/ALERT_EMAIL", file=sys.stderr)
        return False
    flag = f"⚠️ {len(report['alerts'])} alerta(s) — " if report["alerts"] else ""
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": sender, "to": [t.strip() for t in to.split(",") if t.strip()],
              "subject": f"{flag}Operación UMG {report['date']}", "html": html_body},
        timeout=15,
    )
    response.raise_for_status()
    print(f"[ops] mail enviado: {response.json().get('id')}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default="", help="día a reportar (ART); vacío = hoy")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--html-out", default="")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    work_url = os.environ.get("STAGING_DATABASE_URL") or os.environ.get("DATABASE_URL")
    deliveries_url = os.environ.get("DELIVERIES_DATABASE_URL")
    if not (work_url and deliveries_url):
        print("Faltan STAGING_DATABASE_URL (o DATABASE_URL) y DELIVERIES_DATABASE_URL", file=sys.stderr)
        return 2
    report_day = date.fromisoformat(args.date) if args.date else datetime.now(ART).date()
    work, deliveries = _connect(work_url), _connect(deliveries_url)
    try:
        data = fetch(work, deliveries)
    finally:
        work.close()
        deliveries.close()
    report = build_report(data, report_day, holidays=_holidays())
    body = render_html(report)
    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
    if args.html_out:
        with open(args.html_out, "w") as handle:
            handle.write(body)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.send and not send(report, body):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
