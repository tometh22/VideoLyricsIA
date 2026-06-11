"""Insights de comportamiento para el panel CEO (super-admin) — 2026-06-10.

admin_metrics.py responde "¿cómo va el negocio?"; este módulo responde
"¿QUÉ hace cada usuario adentro del producto?": qué features elige
(animaciones, tipografías, estilos — agregación de Job.render_params, que
hasta hoy nadie leía), cuánto retrabaja, dónde falla, y cómo se mueve por
el wizard (funnel de ui_events). Tres niveles con la misma gramática:
app (sin filtros) → tenant → usuario, vía los parámetros tenant_id/user_id.

Convenciones heredadas de admin.py/admin_metrics.py:
- Agregación en Python sobre queries de columnas seleccionadas — sin
  operadores JSON de SQL, para que el mismo código corra en SQLite
  (tests) y Postgres (prod).
- _aware() en toda comparación de datetimes (SQLite devuelve naive).
- Jobs bg_preview_* excluidos siempre (auxiliares del wizard, no videos).
- Retrabajo por lyrics.segments_diff = JOBS distintos, no eventos (el
  autosave dispara miles de eventos por job — bug "Retrabajos: 1011").
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database import (
    AIProvenance,
    AssetUsage,
    AuditLog,
    BackgroundAsset,
    Job,
    UiEvent,
    User,
    UserSession,
)
from provenance import cost_for_record
from error_taxonomy import classify_error
from auth import telemetry_enabled

logger = logging.getLogger("genly.admin_insights")

_FAILED_STATUSES = ("error", "validation_failed")
_IN_PROGRESS_STATUSES = (
    "processing", "queued", "transcribed_pending", "transcribed",
    "pending_review", "editing", "awaiting_upload",
)

# Features de render_params cuya distribución de valores mostramos. Fuente
# de verdad: la whitelist heredable de /retry y /variant en main.py — si se
# agrega un param nuevo allá, sumarlo acá para que aparezca en adoption.
_DISTRIBUTION_KEYS = (
    "lyrics_animation",
    "line_transition",
    "effect",
    "movement_style",
    "text_case",
    "text_contrast",
    "title_template",
)
# Flags = params que importan por presencia (el usuario personalizó algo),
# no por su valor exacto.
_FLAG_KEYS = (
    "custom_colors",
    "lyric_color",
    "lyric_sung_color",
    "background_hint",
    "bg_verbatim",
    "concept",
    "title_song_break",
)
_FONT_TOP_N = 10

# Pasos del wizard (UploadZone.WIZARD_STEPS). El funnel de ui_events cuenta
# cuántas sesiones de creación alcanzan cada uno.
_WIZARD_STEPS = (1, 2, 3, 4, 5, 6)
# Gap que parte una "sesión de wizard" — mismo criterio de 30 min que las
# sesiones de heartbeat (UserSession).
_WIZARD_SESSION_GAP = timedelta(minutes=30)


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    """SQLite (tests) devuelve datetimes naive aunque la columna sea
    timezone=True; Postgres (prod) devuelve aware. Normalizamos a UTC
    para que las comparaciones/restas funcionen igual en ambos."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 1)


def _real_jobs_filter():
    return ~Job.status.like("bg_preview%")


def _scoped_job_query(db: Session, since, tenant_id=None, user_id=None, *cols):
    q = db.query(*cols).filter(Job.created_at >= since, _real_jobs_filter())
    if tenant_id:
        q = q.filter(Job.tenant_id == tenant_id)
    if user_id:
        q = q.filter(Job.user_id == user_id)
    return q


# ─────────────────────────────────────────────────────────────────────────
# Fuente del fondo por job (compartido con /admin/activity/{user_id})
# ─────────────────────────────────────────────────────────────────────────

def background_source_map(db: Session, job_ids) -> dict:
    """job_id → "library_as_is" | "library_variation" | "ai_video" | "ai_image".

    Prioridad: la biblioteca gana (si hubo AssetUsage el usuario eligió un
    asset; la variación además llama a Veo, pero la decisión de producto
    fue "partió de la biblioteca"). Jobs sin rastro en ninguna tabla no
    aparecen en el dict (el caller decide el bucket "other").
    """
    if not job_ids:
        return {}
    job_ids = list(job_ids)
    out = {}
    # IN acotado: el caller ya viene de una ventana temporal (≤ miles de ids).
    for jid, mode in (
        db.query(AssetUsage.job_id, AssetUsage.mode)
        .filter(AssetUsage.job_id.in_(job_ids))
        .all()
    ):
        if jid:
            out[jid] = "library_variation" if mode == "variation" else "library_as_is"
    for jid, step in (
        db.query(AIProvenance.job_id, AIProvenance.step)
        .filter(AIProvenance.job_id.in_(job_ids), AIProvenance.step.in_(("video_bg", "image_bg")))
        .all()
    ):
        if jid and jid not in out:
            out[jid] = "ai_video" if step == "video_bg" else "ai_image"
    return out


def job_choices(job) -> dict | None:
    """Resumen legible de la config elegida por el usuario para UN job.

    None cuando el job no tiene render_params (jobs históricos previos al
    merge de params, o que murieron antes del worker).
    """
    params = job.render_params
    if not params:
        return None
    return {
        "font": params.get("font") or None,
        "lyrics_animation": params.get("lyrics_animation") or None,
        "line_transition": params.get("line_transition") or None,
        "effect": params.get("effect") or None,
        "movement_style": params.get("movement_style") or None,
        "text_case": params.get("text_case") or None,
        "title_template": params.get("title_template") or None,
        "style": job.style,
        "delivery_profile": job.delivery_profile,
        "has_custom_colors": bool(params.get("custom_colors")),
        "has_lyric_color": bool(params.get("lyric_color")),
        "background_hint": bool(params.get("background_hint")),
    }


# ─────────────────────────────────────────────────────────────────────────
# 1. Adoption — qué features se usan (render_params agregado)
# ─────────────────────────────────────────────────────────────────────────

def insights_adoption(db: Session, days: int = 30, tenant_id=None, user_id=None) -> dict:
    """Distribución de elecciones del usuario sobre los jobs de la ventana.

    "(default)" agrupa jobs con params pero sin valor para esa feature —
    el usuario no la tocó y corrió el default del wizard. Así cada
    distribución suma jobs_with_params y los % son comparables.
    """
    since = _utcnow() - timedelta(days=days)
    rows = _scoped_job_query(
        db, since, tenant_id, user_id,
        Job.job_id, Job.style, Job.delivery_profile, Job.render_params,
    ).all()

    total_jobs = len(rows)
    features = {k: defaultdict(int) for k in _DISTRIBUTION_KEYS}
    style_dist = defaultdict(int)
    delivery_dist = defaultdict(int)
    font_counts = defaultdict(int)
    flags = {k: 0 for k in _FLAG_KEYS}
    jobs_with_params = 0

    for r in rows:
        style_dist[r.style or "(sin estilo)"] += 1
        delivery_dist[r.delivery_profile or "youtube"] += 1
        params = r.render_params
        if not params:
            continue
        jobs_with_params += 1
        for key in _DISTRIBUTION_KEYS:
            val = params.get(key)
            features[key][str(val) if val not in (None, "") else "(default)"] += 1
        font = params.get("font")
        if font:
            font_counts[str(font)] += 1
        for key in _FLAG_KEYS:
            if params.get(key):
                flags[key] += 1

    # Fuente del fondo, sobre TODOS los jobs de la ventana (incluye los sin
    # params: la biblioteca/provenance no dependen de render_params).
    source_map = background_source_map(db, [r.job_id for r in rows])
    background_source = defaultdict(int)
    for r in rows:
        background_source[source_map.get(r.job_id, "other")] += 1

    top_fonts = sorted(font_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_FONT_TOP_N]

    return {
        "days": days,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "total_jobs": total_jobs,
        "jobs_with_params": jobs_with_params,
        "features": {
            **{k: dict(v) for k, v in features.items()},
            "style": dict(style_dist),
            "delivery_profile": dict(delivery_dist),
        },
        "font": [{"value": v, "count": c} for v, c in top_fonts],
        "flags": flags,
        "background_source": dict(background_source),
    }


# ─────────────────────────────────────────────────────────────────────────
# 2. Overview — uso/calidad/costo + ranking para el drill-down
# ─────────────────────────────────────────────────────────────────────────

def insights_overview(db: Session, days: int = 30, tenant_id=None) -> dict:
    """KPIs del nivel app o tenant + listas rankeadas de tenants/usuarios.

    `tenants` solo a nivel app (tenant_id=None). `users` siempre: top 50 a
    nivel app, todos los del tenant a nivel tenant — alimenta el click de
    drill-down del frontend.
    """
    now = _utcnow()
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)

    # --- Jobs por usuario (un GROUP BY cubre KPIs, tenants y users) -------
    job_rows = _scoped_job_query(
        db, since, tenant_id, None,
        Job.user_id,
        Job.tenant_id,
        func.count(Job.id).label("total"),
        func.sum(case((Job.status == "done", 1), else_=0)).label("done"),
        func.sum(case(((Job.approved_at.isnot(None)) & (Job.status == "done"), 1), else_=0)).label("approved"),
        func.sum(case((Job.status.in_(_FAILED_STATUSES), 1), else_=0)).label("failed"),
        func.sum(case((Job.status.in_(_IN_PROGRESS_STATUSES), 1), else_=0)).label("in_progress"),
        func.sum(case((Job.parent_job_id.isnot(None), 1), else_=0)).label("variants"),
        func.sum(Job.edit_count).label("edits_total"),
        func.max(func.coalesce(Job.last_user_activity_at, Job.created_at)).label("last_activity"),
    ).group_by(Job.user_id, Job.tenant_id).all()

    prev_q = db.query(func.count(Job.id)).filter(
        Job.created_at >= prev_since, Job.created_at < since, _real_jobs_filter(),
    )
    if tenant_id:
        prev_q = prev_q.filter(Job.tenant_id == tenant_id)
    jobs_prev = int(prev_q.scalar() or 0)

    # --- Retrabajo desde AuditLog (retries + jobs corregidos) -------------
    audit_q = (
        db.query(AuditLog.user_id, AuditLog.action, AuditLog.detail)
        .filter(
            AuditLog.action.in_(("job.retry", "lyrics.segments_diff")),
            AuditLog.created_at >= since,
            AuditLog.user_id.isnot(None),
        )
    )
    if tenant_id:
        audit_q = audit_q.join(User, User.id == AuditLog.user_id).filter(User.tenant_id == tenant_id)
    retries_by_user = defaultdict(int)
    corrected_by_user = defaultdict(set)
    for uid, action, detail in audit_q.all():
        if action == "job.retry":
            retries_by_user[uid] += 1
        else:
            corrected_by_user[uid].add((detail or {}).get("job_id") or "?")

    # --- Costo IA por usuario ----------------------------------------------
    prov_q = (
        db.query(
            Job.user_id,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
    )
    if tenant_id:
        prov_q = prov_q.filter(Job.tenant_id == tenant_id)
    cost_by_user = defaultdict(float)
    for uid, tool, provider, calls in prov_q.group_by(
        Job.user_id, AIProvenance.tool_name, AIProvenance.tool_provider
    ).all():
        cost_by_user[uid] += int(calls or 0) * cost_for_record(tool, provider)

    # --- Errores recientes + categorías ------------------------------------
    err_q = (
        db.query(Job.job_id, Job.user_id, Job.artist, Job.song_title, Job.error,
                 Job.error_category, Job.created_at)
        .filter(Job.status.in_(_FAILED_STATUSES), Job.created_at >= since)
    )
    if tenant_id:
        err_q = err_q.filter(Job.tenant_id == tenant_id)
    errors_by_category = defaultdict(int)
    recent_errors = []
    for r in err_q.order_by(Job.created_at.desc()).limit(200).all():
        category = r.error_category or classify_error(r.error)
        errors_by_category[category] += 1
        if len(recent_errors) < 15:
            recent_errors.append({
                "job_id": r.job_id,
                "user_id": r.user_id,
                "artist": r.artist,
                "song_title": r.song_title,
                "category": category,
                "error": (r.error or "")[:300],
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

    # --- Online ahora (solo con telemetría) ---------------------------------
    online_users = set()
    telemetry_on = telemetry_enabled()
    if telemetry_on:
        # Mismo cutoff de 3 min que /admin/activity. last_seen_at está
        # indexado, así que el filtro es barato.
        cutoff = now - timedelta(minutes=3)
        online_users = {
            uid for (uid,) in db.query(UserSession.user_id)
            .filter(UserSession.last_seen_at >= cutoff).all()
        }

    # --- Identidades --------------------------------------------------------
    user_ids = (
        {r.user_id for r in job_rows}
        | set(retries_by_user)
        | set(corrected_by_user)
        | {e["user_id"] for e in recent_errors if e["user_id"] is not None}
    )
    users_by_id = {
        u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    for e in recent_errors:
        u = users_by_id.get(e["user_id"])
        e["username"] = u.username if u else None

    # --- Por usuario ---------------------------------------------------------
    per_user = {}
    for r in job_rows:
        u = users_by_id.get(r.user_id)
        last = _aware(r.last_activity)
        per_user[r.user_id] = {
            "user_id": r.user_id,
            "username": u.username if u else f"user:{r.user_id}",
            "tenant_id": r.tenant_id or (u.tenant_id if u else None),
            "jobs": int(r.total or 0),
            "done": int(r.done or 0),
            "approved": int(r.approved or 0),
            "failed": int(r.failed or 0),
            "in_progress": int(r.in_progress or 0),
            "variants": int(r.variants or 0),
            "edits_total": int(r.edits_total or 0),
            "retries": retries_by_user.get(r.user_id, 0),
            "corrected_jobs": len(corrected_by_user.get(r.user_id, ())),
            "ai_cost_usd": round(cost_by_user.get(r.user_id, 0.0), 2),
            "last_activity": last.isoformat() if last else None,
            "online": r.user_id in online_users,
        }
    # Usuarios con retrabajo pero sin jobs creados en la ventana (p. ej.
    # solo corrigieron letra de un job viejo) igual aparecen.
    for uid in (set(retries_by_user) | set(corrected_by_user)) - set(per_user):
        u = users_by_id.get(uid)
        if tenant_id and (not u or u.tenant_id != tenant_id):
            continue
        per_user[uid] = {
            "user_id": uid,
            "username": u.username if u else f"user:{uid}",
            "tenant_id": u.tenant_id if u else None,
            "jobs": 0, "done": 0, "approved": 0, "failed": 0, "in_progress": 0,
            "variants": 0, "edits_total": 0,
            "retries": retries_by_user.get(uid, 0),
            "corrected_jobs": len(corrected_by_user.get(uid, ())),
            "ai_cost_usd": round(cost_by_user.get(uid, 0.0), 2),
            "last_activity": None,
            "online": uid in online_users,
        }

    for row in per_user.values():
        row["rework_events"] = (
            row["variants"] + row["edits_total"] + row["retries"] + row["corrected_jobs"]
        )

    users_list = sorted(per_user.values(), key=lambda r: r["last_activity"] or "", reverse=True)
    if not tenant_id:
        users_list = users_list[:50]

    # --- Por tenant (solo nivel app) ----------------------------------------
    tenants_list = None
    if not tenant_id:
        by_tenant = defaultdict(lambda: {
            "jobs": 0, "done": 0, "failed": 0, "rework_events": 0,
            "ai_cost_usd": 0.0, "users": set(), "last_activity": None,
        })
        for row in per_user.values():
            t = by_tenant[row["tenant_id"] or "default"]
            t["jobs"] += row["jobs"]
            t["done"] += row["done"]
            t["failed"] += row["failed"]
            t["rework_events"] += row["rework_events"]
            t["ai_cost_usd"] += row["ai_cost_usd"]
            if row["jobs"] or row["rework_events"]:
                t["users"].add(row["user_id"])
            if row["last_activity"] and (t["last_activity"] is None or row["last_activity"] > t["last_activity"]):
                t["last_activity"] = row["last_activity"]
        tenants_list = sorted(
            (
                {
                    "tenant_id": tid,
                    "jobs": agg["jobs"],
                    "done": agg["done"],
                    "failed": agg["failed"],
                    "rework_events": agg["rework_events"],
                    "active_users": len(agg["users"]),
                    "ai_cost_usd": round(agg["ai_cost_usd"], 2),
                    "last_activity": agg["last_activity"],
                }
                for tid, agg in by_tenant.items()
            ),
            key=lambda t: t["last_activity"] or "",
            reverse=True,
        )

    # --- KPIs agregados -------------------------------------------------------
    jobs_total = sum(r["jobs"] for r in per_user.values())
    jobs_done = sum(r["done"] for r in per_user.values())
    jobs_approved = sum(r["approved"] for r in per_user.values())
    jobs_failed = sum(r["failed"] for r in per_user.values())
    in_progress = sum(r["in_progress"] for r in per_user.values())
    rework_events = sum(r["rework_events"] for r in per_user.values())
    wow_delta = round((jobs_total - jobs_prev) / jobs_prev, 2) if jobs_prev else None

    return {
        "days": days,
        "tenant_id": tenant_id,
        "kpis": {
            "jobs_total": jobs_total,
            "jobs_done": jobs_done,
            "jobs_approved": jobs_approved,
            "jobs_failed": jobs_failed,
            "in_progress": in_progress,
            "approval_rate": round(jobs_approved / jobs_done, 2) if jobs_done else None,
            "active_users": sum(1 for r in per_user.values() if r["jobs"] > 0),
            "jobs_prev_window": jobs_prev,
            "wow_delta": wow_delta,
            "rework_events": rework_events,
            "edits_total": sum(r["edits_total"] for r in per_user.values()),
            "retries": sum(r["retries"] for r in per_user.values()),
            "corrected_jobs": sum(r["corrected_jobs"] for r in per_user.values()),
            "ai_cost_usd": round(sum(r["ai_cost_usd"] for r in per_user.values()), 2),
        },
        "errors_by_category": dict(errors_by_category),
        "recent_errors": recent_errors,
        "tenants": tenants_list,
        "users": users_list,
        "telemetry_enabled": telemetry_on,
    }


# ─────────────────────────────────────────────────────────────────────────
# 3. Wizard — funnel de comportamiento pre-generate (ui_events)
# ─────────────────────────────────────────────────────────────────────────

def insights_wizard(db: Session, days: int = 30, tenant_id=None, user_id=None) -> dict:
    """Funnel del wizard a partir de ui_events.

    Sesioniza los eventos por usuario con gap de 30 min (mismo criterio que
    UserSession) y mide: hasta qué paso llega cada sesión, dónde se
    abandona (último paso sin generate), conversión a generate y p50/p95
    del tiempo primera-acción → generate.
    """
    since = _utcnow() - timedelta(days=days)
    q = db.query(UiEvent.user_id, UiEvent.event_type, UiEvent.event_data, UiEvent.created_at) \
        .filter(UiEvent.created_at >= since)
    if tenant_id:
        q = q.filter(UiEvent.tenant_id == tenant_id)
    if user_id:
        q = q.filter(UiEvent.user_id == user_id)
    rows = q.order_by(UiEvent.user_id, UiEvent.created_at).all()

    if not rows:
        return {
            "days": days,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "empty": True,
            "telemetry_enabled": telemetry_enabled(),
        }

    event_counts = defaultdict(int)
    scene_modes = defaultdict(int)
    library = {"selects": 0, "filters": 0, "modes": defaultdict(int)}

    # Sesionización: lista de sesiones, cada una {max_step, generated, start, gen_at}
    sessions = []
    current = None
    prev_user = None
    prev_time = None

    for uid, etype, data, created_at in rows:
        created_at = _aware(created_at)
        event_counts[etype] += 1
        data = data or {}

        if etype == "wizard.scene_mode":
            scene_modes[str(data.get("mode") or "?")] += 1
        elif etype == "wizard.library_select":
            library["selects"] += 1
        elif etype == "wizard.library_filter":
            library["filters"] += 1
        elif etype == "wizard.library_mode":
            library["modes"][str(data.get("mode") or "?")] += 1

        new_session = (
            uid != prev_user
            or prev_time is None
            or (created_at - prev_time) > _WIZARD_SESSION_GAP
        )
        if new_session:
            current = {"max_step": 1, "generated": False, "start": created_at, "gen_at": None}
            sessions.append(current)
        prev_user, prev_time = uid, created_at

        if etype == "wizard.step":
            try:
                step_to = int(data.get("step_to") or 0)
            except (TypeError, ValueError):
                step_to = 0
            if step_to in _WIZARD_STEPS:
                current["max_step"] = max(current["max_step"], step_to)
        elif etype == "wizard.generate" and not current["generated"]:
            current["generated"] = True
            current["gen_at"] = created_at
            current["max_step"] = max(current["max_step"], 5)

    funnel = {step: 0 for step in _WIZARD_STEPS}
    abandon_by_step = defaultdict(int)
    times_to_generate = []
    generated = 0
    for s in sessions:
        for step in _WIZARD_STEPS:
            if s["max_step"] >= step:
                funnel[step] += 1
        if s["generated"]:
            generated += 1
            if s["gen_at"] and s["start"]:
                times_to_generate.append((s["gen_at"] - s["start"]).total_seconds())
        else:
            abandon_by_step[s["max_step"]] += 1

    times_to_generate.sort()
    total_sessions = len(sessions)

    return {
        "days": days,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "empty": False,
        "sessions_total": total_sessions,
        "sessions_generated": generated,
        "conversion": round(generated / total_sessions, 2) if total_sessions else None,
        "funnel": [{"step": step, "reached": funnel[step]} for step in _WIZARD_STEPS],
        "abandon_by_step": {str(k): v for k, v in sorted(abandon_by_step.items())},
        "scene_modes": dict(scene_modes),
        "library": {
            "selects": library["selects"],
            "filters": library["filters"],
            "modes": dict(library["modes"]),
        },
        "p50_to_generate_s": _percentile(times_to_generate, 50),
        "p95_to_generate_s": _percentile(times_to_generate, 95),
        "event_counts": dict(event_counts),
        "telemetry_enabled": telemetry_enabled(),
    }


# ─────────────────────────────────────────────────────────────────────────
# 4. Extras del drill-down de usuario (consumido por /admin/activity/{id})
# ─────────────────────────────────────────────────────────────────────────

def user_detail_extras(db: Session, user_id: int, since) -> dict:
    """Sesiones, logins y uso de biblioteca para el perfil de usuario.

    Aditivo al response histórico de /admin/activity/{user_id} — el
    frontend nuevo (UserProfileView) lee estas claves; el viejo las
    ignoraba.
    """
    from database import LoginSession

    sessions = None
    if telemetry_enabled():
        sessions = [
            s.to_dict()
            for s in db.query(UserSession)
            .filter(UserSession.user_id == user_id, UserSession.last_seen_at >= since)
            .order_by(UserSession.started_at.desc())
            .limit(50)
            .all()
        ]

    logins = [
        {
            "ip_address": s.ip_address,
            "user_agent": (s.user_agent or "")[:200],
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
            "revoked": s.revoked_at is not None,
        }
        for s in db.query(LoginSession)
        .filter(LoginSession.user_id == user_id)
        .order_by(LoginSession.created_at.desc())
        .limit(10)
        .all()
    ]

    library_usage = [
        {
            "asset_id": u.asset_id,
            "name": name,
            "mode": u.mode,
            "job_id": u.job_id,
            "used_at": u.used_at.isoformat() if u.used_at else None,
        }
        for u, name in db.query(AssetUsage, BackgroundAsset.name)
        .outerjoin(BackgroundAsset, BackgroundAsset.id == AssetUsage.asset_id)
        .filter(AssetUsage.user_id == user_id, AssetUsage.used_at >= since)
        .order_by(AssetUsage.used_at.desc())
        .limit(100)
        .all()
    ]

    return {"sessions": sessions, "logins": logins, "library_usage": library_usage}


# ─────────────────────────────────────────────────────────────────────────
# 5. Profundidad (iteración "quiero más detalle en todo", 2026-06-11)
# ─────────────────────────────────────────────────────────────────────────

# Features drilleables: las distribuciones de adoption + las columnas
# directas del Job. Whitelist explícita — el endpoint recibe strings del
# frontend y NUNCA deben tocar atributos arbitrarios.
_DRILLABLE_PARAM_FEATURES = set(_DISTRIBUTION_KEYS) | {"font"}
_DRILLABLE_COLUMN_FEATURES = {"style", "delivery_profile"}


def insights_feature_detail(db: Session, feature: str, value: str,
                            days: int = 30, tenant_id=None) -> dict:
    """¿QUIÉNES usan esta feature con este valor, y en qué videos?

    El drill de las barras de adopción: click en "Karaoke" → usuarios
    que la usan (con conteo) + los jobs concretos. value="(default)"
    matchea jobs con params pero sin valor para esa key.
    """
    if feature not in _DRILLABLE_PARAM_FEATURES | _DRILLABLE_COLUMN_FEATURES:
        return {"error": f"feature no drilleable: {feature}", "users": [], "jobs": []}

    since = _utcnow() - timedelta(days=days)
    rows = _scoped_job_query(
        db, since, tenant_id, None,
        Job.job_id, Job.user_id, Job.artist, Job.song_title, Job.status,
        Job.created_at, Job.style, Job.delivery_profile, Job.render_params,
    ).order_by(Job.created_at.desc()).all()

    def _matches(r):
        if feature in _DRILLABLE_COLUMN_FEATURES:
            actual = r.style if feature == "style" else r.delivery_profile
            return (actual or "") == value
        params = r.render_params
        if not params:
            return False
        actual = params.get(feature)
        if value == "(default)":
            return actual in (None, "")
        return str(actual) == value

    matched = [r for r in rows if _matches(r)]
    by_user = defaultdict(lambda: {"count": 0, "last": None})
    for r in matched:
        agg = by_user[r.user_id]
        agg["count"] += 1
        created = _aware(r.created_at)
        if created and (agg["last"] is None or created > agg["last"]):
            agg["last"] = created
    users_by_id = {
        u.id: u.username
        for u in db.query(User).filter(User.id.in_(by_user.keys())).all()
    } if by_user else {}

    return {
        "feature": feature,
        "value": value,
        "days": days,
        "tenant_id": tenant_id,
        "total_jobs": len(matched),
        "users": sorted(
            (
                {
                    "user_id": uid,
                    "username": users_by_id.get(uid, f"user:{uid}"),
                    "count": agg["count"],
                    "last_used": agg["last"].isoformat() if agg["last"] else None,
                }
                for uid, agg in by_user.items()
            ),
            key=lambda r: -r["count"],
        ),
        "jobs": [
            {
                "job_id": r.job_id,
                "user_id": r.user_id,
                "username": users_by_id.get(r.user_id, f"user:{r.user_id}"),
                "artist": r.artist,
                "song_title": r.song_title,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in matched[:50]
        ],
    }


def insights_job_detail(db: Session, job_id: str) -> dict | None:
    """La ficha completa de UN video: parámetros elegidos, cada llamada IA
    con su costo, eventos de auditoría y error. El fondo del drill-down
    (perfil de usuario → video → ficha)."""
    job = db.query(Job).filter(Job.job_id == job_id).first()
    if not job:
        return None
    user = db.query(User).filter(User.id == job.user_id).first()

    prov = (
        db.query(AIProvenance)
        .filter(AIProvenance.job_id == job_id)
        .order_by(AIProvenance.created_at)
        .all()
    )
    calls = [
        {
            "step": p.step,
            "tool_name": p.tool_name,
            "tool_provider": p.tool_provider,
            "duration_ms": p.duration_ms,
            "cost_usd": round(cost_for_record(p.tool_name, p.tool_provider), 4),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in prov
    ]

    events = [
        {
            "action": e.action,
            "detail": e.detail,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in db.query(AuditLog)
        .filter(AuditLog.detail.isnot(None), AuditLog.created_at >= (_aware(job.created_at) or _utcnow()) - timedelta(days=1))
        .order_by(AuditLog.created_at)
        .limit(2000)
        .all()
        if (e.detail or {}).get("job_id") == job_id
    ]

    choices = job_choices(job) or {}
    src_map = background_source_map(db, [job_id])
    choices["background_source"] = src_map.get(job_id, "other")

    return {
        "job_id": job.job_id,
        "username": user.username if user else None,
        "user_id": job.user_id,
        "tenant_id": job.tenant_id,
        "artist": job.artist,
        "song_title": job.song_title,
        "status": job.status,
        "current_step": job.current_step,
        "error": (job.error or "")[:500] or None,
        "error_category": job.error_category,
        "edit_count": job.edit_count or 0,
        "parent_job_id": job.parent_job_id,
        "delivery_profile": job.delivery_profile,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "approved_at": job.approved_at.isoformat() if job.approved_at else None,
        "choices": choices,
        "render_params": {
            k: v for k, v in (job.render_params or {}).items()
            if v not in (None, "")
        },
        "ai_calls": calls,
        "ai_cost_usd": round(sum(c["cost_usd"] for c in calls), 2),
        "events": events,
    }


def insights_user_events(db: Session, user_id: int, days: int = 30) -> dict:
    """Sesiones de wizard de UN usuario, reconstruidas desde ui_events:
    qué pasos recorrió, qué fondos tocó, dónde abandonó. La lupa de
    comportamiento del perfil."""
    since = _utcnow() - timedelta(days=days)
    rows = (
        db.query(UiEvent.event_type, UiEvent.event_data, UiEvent.created_at)
        .filter(UiEvent.user_id == user_id, UiEvent.created_at >= since)
        .order_by(UiEvent.created_at)
        .limit(2000)
        .all()
    )
    sessions = []
    current = None
    prev_time = None
    for etype, data, created_at in rows:
        created_at = _aware(created_at)
        if prev_time is None or (created_at - prev_time) > _WIZARD_SESSION_GAP:
            current = {
                "started_at": created_at.isoformat(),
                "events": [],
                "generated": False,
            }
            sessions.append(current)
        prev_time = created_at
        current["events"].append({
            "type": etype,
            "data": data or {},
            "at": created_at.isoformat(),
        })
        if etype == "wizard.generate":
            current["generated"] = True
    sessions.reverse()  # más recientes primero
    return {
        "user_id": user_id,
        "days": days,
        "telemetry_enabled": telemetry_enabled(),
        "sessions": sessions[:30],
        "total_events": len(rows),
    }
