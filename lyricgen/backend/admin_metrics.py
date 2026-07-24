"""Métricas de decisión para el panel admin — Fases 1+2 (2026-06-11).

El panel existente (admin.py) responde "¿cuánto?"; este módulo responde
las preguntas tier-1: "¿mejor o peor que antes?" (series temporales con
deltas), "¿dónde se traba el flujo?" (funnel con p50/p95), "¿cuánto
ganamos de verdad?" (margen por tenant: costo IA de Provenance × precio
del plan) y "¿qué cuentas están en riesgo?" (health score + alertas de
negocio diarias).

Todo se computa de datos YA registrados (Job, AuditLog, AIProvenance) —
cero instrumentación nueva, cero migraciones.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import AIProvenance, AuditLog, Job, User
from provenance import cost_for_record

logger = logging.getLogger("genly.admin_metrics")

_FAILED_STATUSES = ("error", "validation_failed", "transcription_failed")


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    """SQLite (tests) devuelve datetimes naive aunque la columna sea
    timezone=True; Postgres (prod) devuelve aware. Normalizamos a UTC
    para que las comparaciones/restas funcionen igual en ambos."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _day_key(dt) -> str:
    return dt.strftime("%Y-%m-%d")


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 1)


# ─────────────────────────────────────────────────────────────────────────
# Fase 1a — Series temporales por día y tenant
# ─────────────────────────────────────────────────────────────────────────

def metrics_timeseries(db: Session, days: int = 28) -> dict:
    """Por día y tenant: jobs creados, aprobados, edit-requests y costo IA.

    El frontend deriva los deltas WoW de acá (suma 7d actuales vs 7d
    previos) — por eso default 28 días: dos ventanas de comparación con
    margen.
    """
    since = _utcnow() - timedelta(days=days)
    series = defaultdict(lambda: defaultdict(lambda: {
        "created": 0, "approved": 0, "edit_requests": 0, "ai_cost_usd": 0.0,
    }))

    for tenant, created_at in (
        db.query(Job.tenant_id, Job.created_at).filter(Job.created_at >= since).all()
    ):
        series[_day_key(created_at)][tenant]["created"] += 1

    for tenant, approved_at in (
        db.query(Job.tenant_id, Job.approved_at)
        .filter(Job.approved_at.isnot(None), Job.approved_at >= since)
        .all()
    ):
        series[_day_key(approved_at)][tenant]["approved"] += 1

    # Incidente prod 2026-06-11 (Sentry AttributeError "no attribute
    # 'astext'"): detail es nuestro TypeDecorator JSONB y al indexarlo el
    # comparator NO expone .astext (eso es del tipo nativo de
    # sqlalchemy.dialects.postgresql) → el primer hit a /metrics/timeseries
    # en prod tiró 500 y los KPIs de Rendimiento quedaron en "—".
    # as_string() es el accessor portable del comparator JSON genérico; y
    # ante CUALQUIER sorpresa de dialecto caemos al camino Python (mismo
    # resultado, más lento) en vez de romper el endpoint.
    def _edit_rows_python():
        return [
            (r.created_at, _tenant_for_job(db, (r.detail or {}).get("job_id")))
            for r in db.query(AuditLog)
            .filter(AuditLog.action == "job.edit_request", AuditLog.created_at >= since)
            .all()
        ]

    if db.bind.dialect.name == "postgresql":
        try:
            edit_rows = (
                db.query(AuditLog.created_at, Job.tenant_id)
                .join(User, User.id == AuditLog.user_id)
                .outerjoin(Job, Job.job_id == func.coalesce(AuditLog.detail["job_id"].as_string(), ""))
                .filter(AuditLog.action == "job.edit_request", AuditLog.created_at >= since)
                .all()
            )
        except Exception as e:
            logger.warning("[METRICS] timeseries: SQL JSON path falló (%s) — fallback Python", e)
            edit_rows = _edit_rows_python()
    else:
        edit_rows = _edit_rows_python()
    for created_at, tenant in edit_rows:
        if tenant:
            series[_day_key(created_at)][tenant]["edit_requests"] += 1

    prov_rows = (
        db.query(AIProvenance.created_at, AIProvenance.tool_name,
                 AIProvenance.tool_provider, Job.tenant_id)
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
        .all()
    )
    for created_at, tool, provider, tenant in prov_rows:
        series[_day_key(created_at)][tenant]["ai_cost_usd"] = round(
            series[_day_key(created_at)][tenant]["ai_cost_usd"]
            + cost_for_record(tool, provider), 4,
        )

    return {"days": days, "series": {d: dict(t) for d, t in sorted(series.items())}}


def _tenant_for_job(db: Session, job_id) -> str | None:
    if not job_id:
        return None
    row = db.query(Job.tenant_id).filter(Job.job_id == job_id).first()
    return row[0] if row else None


# ─────────────────────────────────────────────────────────────────────────
# Fase 1b — Funnel operativo con tiempos p50/p95
# ─────────────────────────────────────────────────────────────────────────

def metrics_funnel(db: Session, days: int = 7) -> dict:
    """Etapas del flujo por job, con conversión y duración p50/p95.

    Timestamps disponibles SIN migrar nada:
      t0 created_at (Job) · t1 primer lyrics.segments_diff (AuditLog) ·
      t2 último segments_diff · t3 fin de la última llamada IA
      (AIProvenance) · t4 approved_at (Job) · t5 primer job.download.
    Etapas: hasta_review (t1-t0), review (t2-t1), render (t3-t2),
    aprobación (t4-t3), descarga (t5-t4).
    """
    since = _utcnow() - timedelta(days=days)
    jobs = {
        j.job_id: {"t0": _aware(j.created_at), "t4": _aware(j.approved_at), "tenant": j.tenant_id,
                   "status": j.status}
        for j in db.query(Job).filter(Job.created_at >= since).all()
    }
    if not jobs:
        return {"days": days, "total_jobs": 0, "stages": []}

    diffs = defaultdict(list)
    downloads = {}
    for r in (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(("lyrics.segments_diff", "job.download")),
                AuditLog.created_at >= since)
        .all()
    ):
        jid = (r.detail or {}).get("job_id")
        if jid not in jobs:
            continue
        if r.action == "lyrics.segments_diff":
            diffs[jid].append(_aware(r.created_at))
        elif jid not in downloads:
            downloads[jid] = _aware(r.created_at)

    prov_end = {}
    for jid, created_at in (
        db.query(AIProvenance.job_id, func.max(AIProvenance.created_at))
        .filter(AIProvenance.created_at >= since)
        .group_by(AIProvenance.job_id)
        .all()
    ):
        if jid in jobs:
            prov_end[jid] = _aware(created_at)

    stage_defs = [
        ("hasta_review", "t0", "t1"), ("review", "t1", "t2"),
        ("render", "t2", "t3"), ("aprobacion", "t3", "t4"),
        ("descarga", "t4", "t5"),
    ]
    for jid, j in jobs.items():
        ts = sorted(diffs.get(jid, []))
        j["t1"] = ts[0] if ts else None
        j["t2"] = ts[-1] if ts else None
        j["t3"] = prov_end.get(jid)
        j["t5"] = downloads.get(jid)

    stages = []
    for name, a, b in stage_defs:
        durations = sorted(
            (j[b] - j[a]).total_seconds()
            for j in jobs.values()
            if j.get(a) and j.get(b) and j[b] >= j[a]
        )
        stages.append({
            "stage": name,
            "reached": len(durations),
            "p50_s": _percentile(durations, 50),
            "p95_s": _percentile(durations, 95),
        })

    failed = sum(1 for j in jobs.values() if j["status"] in _FAILED_STATUSES)
    return {"days": days, "total_jobs": len(jobs), "failed": failed, "stages": stages}


# ─────────────────────────────────────────────────────────────────────────
# Fase 1c — Unit economics por tenant
# ─────────────────────────────────────────────────────────────────────────

def metrics_economics(db: Session, days: int = 28) -> dict:
    """Margen real por tenant: precio del plan × videos aprobados, menos
    el costo IA TOTAL del tenant (incluye retrabajos — ahí vive la
    diferencia entre el margen teórico y el real)."""
    from auth import PLANS
    since = _utcnow() - timedelta(days=days)

    cost_by_tenant = defaultdict(float)
    for tool, provider, tenant, calls in (
        db.query(AIProvenance.tool_name, AIProvenance.tool_provider,
                 Job.tenant_id, func.count(AIProvenance.id))
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
        .group_by(AIProvenance.tool_name, AIProvenance.tool_provider, Job.tenant_id)
        .all()
    ):
        cost_by_tenant[tenant] += int(calls or 0) * cost_for_record(tool, provider)

    approved_by_tenant = dict(
        db.query(Job.tenant_id, func.count(Job.job_id))
        .filter(Job.approved_at.isnot(None), Job.approved_at >= since)
        .group_by(Job.tenant_id)
        .all()
    )
    # Precio del plan: tomamos el plan más caro entre los usuarios del
    # tenant (los tenants B2B tienen un plan homogéneo; el max evita que
    # una cuenta interna de prueba con plan free pise el precio real).
    plan_price_by_tenant = {}
    for tenant, plan_id in db.query(User.tenant_id, User.plan_id).all():
        price = (PLANS.get(plan_id or "") or {}).get("price_per_video", 0) or 0
        plan_price_by_tenant[tenant] = max(plan_price_by_tenant.get(tenant, 0), price)

    tenants = sorted(set(cost_by_tenant) | set(approved_by_tenant))
    rows = []
    for t in tenants:
        approved = int(approved_by_tenant.get(t, 0))
        cost = round(cost_by_tenant.get(t, 0.0), 2)
        price = plan_price_by_tenant.get(t, 0)
        revenue = round(approved * price, 2)
        rows.append({
            "tenant_id": t,
            "approved_videos": approved,
            "ai_cost_usd": cost,
            "price_per_video": price,
            "revenue_usd": revenue,
            "margin_usd": round(revenue - cost, 2),
            "cost_per_approved_usd": round(cost / approved, 2) if approved else None,
        })
    rows.sort(key=lambda r: -(r["revenue_usd"]))
    return {"days": days, "tenants": rows}


# ─────────────────────────────────────────────────────────────────────────
# Fase 2 — Health score por tenant + alertas de negocio
# ─────────────────────────────────────────────────────────────────────────

def metrics_health(db: Session) -> dict:
    """Score 0-100 por tenant activo (últimos 28d), con componentes.

    Componentes (pesos):
      40 uso        — jobs últimos 7d vs 7d previos (caída penaliza)
      25 calidad    — first-pass approval (aprobados sin edit_request)
      20 retrabajo  — edit_requests / jobs (más retrabajo penaliza)
      15 errores    — jobs fallidos / jobs
    """
    now = _utcnow()
    w_now, w_prev = now - timedelta(days=7), now - timedelta(days=14)
    since28 = now - timedelta(days=28)

    jobs = db.query(Job).filter(Job.created_at >= since28).all()
    by_tenant = defaultdict(list)
    for j in jobs:
        by_tenant[j.tenant_id].append(j)

    edits_by_job = set()
    for r in (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.edit_request", AuditLog.created_at >= since28)
        .all()
    ):
        jid = (r.detail or {}).get("job_id")
        if jid:
            edits_by_job.add(jid)

    tenants = []
    for tenant, tjobs in sorted(by_tenant.items()):
        cur = sum(1 for j in tjobs if _aware(j.created_at) >= w_now)
        prev = sum(1 for j in tjobs if w_prev <= _aware(j.created_at) < w_now)
        usage_delta = None if prev == 0 else round((cur - prev) / prev, 2)
        usage_score = 40 if prev == 0 and cur > 0 else (
            0 if (prev == 0 and cur == 0)
            else max(0, min(40, 40 * (1 + min(0.0, usage_delta or 0.0))))
        )
        approved = [j for j in tjobs if j.approved_at is not None]
        first_pass = sum(1 for j in approved if j.job_id not in edits_by_job)
        fp_rate = (first_pass / len(approved)) if approved else None
        quality_score = 25 * fp_rate if fp_rate is not None else 12.5
        rework_rate = (sum(1 for j in tjobs if j.job_id in edits_by_job) / len(tjobs)) if tjobs else 0
        rework_score = max(0.0, 20 * (1 - min(1.0, rework_rate * 2)))
        failed = sum(1 for j in tjobs if j.status in _FAILED_STATUSES)
        error_rate = failed / len(tjobs) if tjobs else 0
        error_score = max(0.0, 15 * (1 - min(1.0, error_rate * 5)))

        score = round(usage_score + quality_score + rework_score + error_score)
        tenants.append({
            "tenant_id": tenant,
            "score": score,
            "status": "verde" if score >= 70 else ("amarillo" if score >= 45 else "rojo"),
            "jobs_7d": cur, "jobs_prev_7d": prev,
            "usage_delta_wow": usage_delta,
            "first_pass_rate": round(fp_rate, 2) if fp_rate is not None else None,
            "rework_rate": round(rework_rate, 2),
            "error_rate": round(error_rate, 2),
        })
    tenants.sort(key=lambda t: t["score"])
    return {"tenants": tenants}


# Umbrales de las alertas de negocio. Sintonizados conservadores: mejor
# una alerta tardía que un canal que nadie lee por ruidoso.
USAGE_DROP_THRESHOLD = -0.4   # caída WoW > 40%
USAGE_DROP_MIN_PREV = 5       # ...con volumen previo mínimo (evita 2→1)
REWORK_RATE_THRESHOLD = 0.5
ERROR_RATE_THRESHOLD = 0.2
ERROR_RATE_MIN_JOBS = 5


def run_business_alerts(db: Session) -> list[dict]:
    """Evalúa el health de cada tenant y alerta por Sentry (agrupado por
    tenant+tipo, estilo #630) cuando cruza umbrales. Corre diaria desde
    el thread en main.py. Devuelve las alertas emitidas (para tests)."""
    health = metrics_health(db)
    fired = []
    for t in health["tenants"]:
        checks = []
        if (t["usage_delta_wow"] is not None
                and t["jobs_prev_7d"] >= USAGE_DROP_MIN_PREV
                and t["usage_delta_wow"] <= USAGE_DROP_THRESHOLD):
            checks.append(("usage-drop",
                           f"uso cayó {abs(t['usage_delta_wow']):.0%} WoW "
                           f"({t['jobs_prev_7d']}→{t['jobs_7d']} jobs)"))
        if t["rework_rate"] >= REWORK_RATE_THRESHOLD:
            checks.append(("rework-spike",
                           f"tasa de retrabajo {t['rework_rate']:.0%}"))
        if (t["error_rate"] >= ERROR_RATE_THRESHOLD
                and (t["jobs_7d"] + t["jobs_prev_7d"]) >= ERROR_RATE_MIN_JOBS):
            checks.append(("error-rate",
                           f"tasa de error {t['error_rate']:.0%}"))
        for kind, detail in checks:
            fired.append({"tenant": t["tenant_id"], "kind": kind, "detail": detail})
            logger.warning("[BUSINESS-ALERT] tenant=%s %s: %s",
                           t["tenant_id"], kind, detail)
            try:
                import sentry_sdk
                with sentry_sdk.push_scope() as scope:
                    scope.fingerprint = ["business-alert", t["tenant_id"], kind]
                    scope.set_tag("tenant_id", t["tenant_id"])
                    scope.set_extra("health", t)
                    sentry_sdk.capture_message(
                        f"[BUSINESS-ALERT] {t['tenant_id']}: {detail}",
                        level="warning",
                    )
            except Exception:
                pass
    return fired
