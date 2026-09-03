"""Página de status pública (`/status`) + banner de incidente en la home.

Responde UNA pregunta que hoy no tiene respuesta: "¿es mi problema o es de
ellos?". Sin eso el cliente asume que es suyo, prueba tres veces, y recién
después escribe — con el incidente ya viejo y la confianza gastada.

Dos fuentes, deliberadamente separadas
--------------------------------------
1. **El relato humano** (`StatusIncident` + `StatusIncidentUpdate`): lo que
   un operador redacta y publica. Es lo único que puede explicar un
   incidente de un proveedor externo (Replicate encolando, Veo sin cuota,
   Railway con un deploy trabado) que las sondas ven perfectamente verde.
2. **La sonda** (`derive_components` sobre `health_snapshot()`): DB, Redis,
   consumidores de cada cola, R2, breaker de Veo. Es lo único que detecta
   un problema a las 4 AM cuando no hay nadie para redactar nada.

Ninguna deriva de la otra. El indicador general es el peor de los dos.

Lo que la sonda NO reporta al público
-------------------------------------
`health_snapshot` marca `degraded` por skew de release entre API y workers
(`mixed_worker_releases`, `worker_fleet_incoherent`, …). Eso pasa en CADA
deploy por diseño — la API nueva arranca mientras la flota vieja todavía
sirve — y no cambia nada para el usuario. Publicarlo pintaría la página de
amarillo en cada merge, y una alarma que grita en falso de rutina enseña a
ignorarla. Por eso los componentes se derivan de campos concretos (`db`,
`redis`, `fleet_missing_queues`, `r2_probe_error`) y no del `status`
agregado ni de `degraded_reason`.

Límite honesto de una página de status propia
---------------------------------------------
Vive en la misma infraestructura que mide, así que no puede reportar su
propia caída total. Se mitiga con la asimetría real del stack: el frontend
está en Vercel y la API en Railway, así que una caída de Railway deja la
página EN PIE mostrando "no podemos contactar la API" — que es exactamente
la señal que el cliente vino a buscar. Para el caso en que se caigan las
dos, la sonda externa de `docs/STATUS_PAGE_SETUP.md` (GitHub Actions cada
5 min, infra de terceros) sigue siendo el backstop y manda el mail.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import require_admin
from database import (
    AuditLog,
    StatusComponentEvent,
    StatusIncident,
    StatusIncidentUpdate,
    get_db,
)

logger = logging.getLogger("genly.status")

# `/status/{job_id}` YA existe en main.py (polling del estado de UN job), así
# que la página no puede colgar de `/status` sin quedar a merced del orden de
# registro de rutas: `/status/summary` matchearía `{job_id}="summary"` con
# sólo mover un include_router. El prefijo propio elimina la ambigüedad.
# La URL que ve el usuario sigue siendo `/status` — eso es una ruta del
# frontend, otro namespace.
router = APIRouter(prefix="/service-status", tags=["status"])
admin_router = APIRouter(prefix="/admin/status", tags=["status-admin"])


# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------

# Estados de componente, de mejor a peor. El orden ES la semántica: el
# indicador general y el peor-estado-del-día se calculan con `max` sobre
# este ranking, así que agregar un estado nuevo en el medio cambia el
# resultado de las dos cosas.
COMPONENT_STATUS_RANK = {
    "operational": 0,
    "maintenance": 1,
    "degraded": 2,
    "partial_outage": 3,
    "major_outage": 4,
}

# `unknown` queda FUERA del ranking a propósito: no es "peor que operativo"
# ni "mejor que degradado", es ausencia de dato. Se propaga como gris y
# nunca dispara un banner. Un outage inventado por una sonda que no corrió
# cuesta más credibilidad que un hueco admitido.
STATUS_UNKNOWN = "unknown"

INCIDENT_STATUSES = ("investigating", "identified", "monitoring", "resolved")
INCIDENT_IMPACTS = ("none", "minor", "major", "critical")

# Impacto declarado a mano → estado de componente equivalente, para poder
# comparar el relato humano con la sonda en la misma escala.
IMPACT_TO_COMPONENT_STATUS = {
    "none": "maintenance",
    "minor": "degraded",
    "major": "partial_outage",
    "critical": "major_outage",
}


# ---------------------------------------------------------------------------
# Componentes
# ---------------------------------------------------------------------------
#
# Están escritos desde lo que el CLIENTE puede notar, no desde el diagrama
# de servicios. "ShortWorker sin consumidor en la cola transcription" no le
# dice nada a nadie en UMG; "Transcripción y sincronía: caído" sí. El `id`
# es estable y viaja en la API (los incidentes lo referencian); el label
# traducible vive en el frontend bajo `status.component.<id>`.
COMPONENTS = (
    {
        "id": "api",
        "label": "Portal y API",
        "description": "Login, dashboard, editor de letras y descargas.",
    },
    {
        "id": "transcription",
        "label": "Transcripción y sincronía",
        "description": "Transcribir el audio y sincronizar la letra palabra por palabra.",
    },
    {
        "id": "render",
        "label": "Generación de videos",
        "description": "Render del lyric video, del Short y de la miniatura.",
    },
    {
        "id": "backgrounds",
        "label": "Fondos con IA",
        "description": "Generación de fondos y previews con Veo / Imagen.",
    },
    {
        "id": "storage",
        "label": "Entregables y descargas",
        "description": "Almacenamiento de los archivos finales y links de descarga.",
    },
)

COMPONENT_IDS = tuple(c["id"] for c in COMPONENTS)


def _worse(a: str, b: str) -> str:
    """El peor de dos estados. `unknown` pierde contra cualquier dato real."""
    if a == STATUS_UNKNOWN:
        return b
    if b == STATUS_UNKNOWN:
        return a
    return a if COMPONENT_STATUS_RANK.get(a, 0) >= COMPONENT_STATUS_RANK.get(b, 0) else b


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


# Backlog a partir del cual la cola se reporta degradada. NO es un outage:
# la cola profunda significa "vas a esperar más", no "no funciona". Los
# valores por defecto salen del régimen normal medido (colas casi siempre
# en 0-5; un lote de UMG mete 30-60 de golpe y eso NO es un incidente),
# así que el umbral está arriba del lote típico a propósito.
def _transcription_backlog_threshold() -> int:
    return _int_env("STATUS_TRANSCRIPTION_BACKLOG_DEGRADED", 80)


def _render_backlog_threshold() -> int:
    return _int_env("STATUS_RENDER_BACKLOG_DEGRADED", 80)


# ---------------------------------------------------------------------------
# Derivación: snapshot de /health → estado por componente
# ---------------------------------------------------------------------------

def derive_components(snap: Optional[dict]) -> list[dict]:
    """Traduce un snapshot de `observability.health_snapshot()` a componentes.

    Función PURA — no toca DB ni red. Es el único lugar donde se decide qué
    ve el cliente, así que los tests la ejercitan con snapshots armados a
    mano en vez de levantar la app.

    Devuelve `[{id, status, reason}]` en el orden de COMPONENTS.
    """
    if not isinstance(snap, dict) or not snap:
        return [
            {"id": cid, "status": STATUS_UNKNOWN, "reason": "no_snapshot"}
            for cid in COMPONENT_IDS
        ]

    db_state = snap.get("db")
    redis_state = snap.get("redis")
    r2_state = snap.get("r2")
    depth = snap.get("queue_depth") if isinstance(snap.get("queue_depth"), dict) else None
    workers = snap.get("workers_alive")
    missing_queues = set(snap.get("fleet_missing_queues") or [])
    breaker = snap.get("veo_breaker") if isinstance(snap.get("veo_breaker"), dict) else {}
    pool = snap.get("db_pool") if isinstance(snap.get("db_pool"), dict) else {}

    # ¿Tenemos señal de cola? Cuando `rq` no es importable en este proceso,
    # health_snapshot omite `queue_depth` y `workers_alive` por completo. Sin
    # esa distinción, "no pudimos preguntar" se leería igual que "no hay
    # workers" y publicaríamos un outage total inventado.
    have_queue_signal = depth is not None and isinstance(workers, int) and workers >= 0
    # `redis: not_configured` fuera de prod es intencional (fallback por
    # threads) y la app anda: no es un outage para el usuario.
    redis_broken = redis_state in ("down", "error")

    out: list[dict] = []

    def add(cid: str, status: str, reason: Optional[str] = None) -> None:
        out.append({"id": cid, "status": status, "reason": reason})

    # --- api ---------------------------------------------------------------
    # Si hay snapshot, el proceso contestó. Lo que puede tirar abajo el
    # portal desde acá es Postgres.
    if db_state == "down":
        add("api", "major_outage", "db_down")
    elif db_state != "up":
        add("api", STATUS_UNKNOWN, "db_unknown")
    else:
        util = pool.get("utilization")
        if isinstance(util, (int, float)) and util > 0.9:
            # Pool saturado = 503 con Retry-After para el usuario. Es lento
            # y con errores intermitentes, no caído.
            add("api", "degraded", "db_pool_saturated")
        else:
            add("api", "operational")

    # --- transcription -----------------------------------------------------
    # Las dos colas del camino de transcripción: `transcription` (single) y
    # `transcription_batch` (lotes de campaña).
    if redis_broken:
        add("transcription", "major_outage", "queue_unavailable")
    elif not have_queue_signal:
        add("transcription", STATUS_UNKNOWN, "no_queue_signal")
    elif workers == 0:
        add("transcription", "major_outage", "no_workers")
    elif missing_queues & {"transcription", "transcription_batch"}:
        add("transcription", "major_outage", "no_consumer")
    else:
        backlog = _q(depth, "transcription") + _q(depth, "transcription_batch")
        if backlog > _transcription_backlog_threshold():
            add("transcription", "degraded", f"backlog_{backlog}")
        else:
            add("transcription", "operational")

    # --- render ------------------------------------------------------------
    if redis_broken:
        add("render", "major_outage", "queue_unavailable")
    elif not have_queue_signal:
        add("render", STATUS_UNKNOWN, "no_queue_signal")
    elif workers == 0:
        add("render", "major_outage", "no_workers")
    elif missing_queues & {"enterprise", "default", "batch_render"}:
        add("render", "major_outage", "no_consumer")
    else:
        backlog = (_q(depth, "enterprise") + _q(depth, "default")
                   + _q(depth, "batch_render"))
        if backlog > _render_backlog_threshold():
            add("render", "degraded", f"backlog_{backlog}")
        else:
            add("render", "operational")

    # --- backgrounds -------------------------------------------------------
    # El breaker abierto es un evento de cuota de Veo: los renders siguen
    # saliendo pero con degradé en vez de fondo IA. Es una degradación
    # PARCIAL y visible en el entregable, así que se publica.
    if breaker.get("open"):
        add("backgrounds", "partial_outage", "veo_quota_breaker_open")
    elif redis_broken:
        add("backgrounds", "major_outage", "queue_unavailable")
    elif not have_queue_signal:
        add("backgrounds", STATUS_UNKNOWN, "no_queue_signal")
    elif "bg_preview" in missing_queues:
        # Los previews de fondo se cortan; el render final va por otra cola.
        add("backgrounds", "degraded", "preview_no_consumer")
    else:
        add("backgrounds", "operational")

    # --- storage -----------------------------------------------------------
    if r2_state in ("not_configured", "error"):
        add("storage", "major_outage", "storage_not_configured")
    elif snap.get("r2_probe_error"):
        add("storage", "major_outage", "storage_unreachable")
    elif r2_state not in ("ready", "configured", "up"):
        add("storage", STATUS_UNKNOWN, "no_storage_signal")
    else:
        cb = snap.get("r2_circuit_breaker")
        probe_ms = snap.get("r2_probe_ms")
        if isinstance(cb, dict) and cb.get("open"):
            add("storage", "partial_outage", "storage_circuit_open")
        elif isinstance(probe_ms, (int, float)) and probe_ms > 1500:
            add("storage", "degraded", f"storage_slow_{int(probe_ms)}ms")
        else:
            add("storage", "operational")

    order = {cid: i for i, cid in enumerate(COMPONENT_IDS)}
    out.sort(key=lambda c: order.get(c["id"], 99))
    return out


def _q(depth: dict, name: str) -> int:
    """Profundidad de una cola. -1 (rq no pudo contarla) cuenta como 0.

    Un -1 sumado crudo restaría del backlog y podría tapar una cola
    realmente profunda con otra que no se pudo medir.
    """
    v = depth.get(name)
    return v if isinstance(v, int) and v > 0 else 0


# ---------------------------------------------------------------------------
# Snapshot cacheado + registro de tramos observados
# ---------------------------------------------------------------------------

# La página es pública y durante un outage la abre TODO el mundo a la vez.
# Sin cache, cada refresh dispara SELECT 1 + PING + HEAD a R2 justo cuando
# la infra está peor. 20 s es más rápido que el poll del banner (60 s) así
# que nadie ve datos viejos de más de un ciclo.
_SNAPSHOT_TTL_S = 20.0
_OBSERVE_MIN_INTERVAL_S = 20.0

# Un tramo se extiende hasta la observación siguiente sólo si el hueco es
# corto. Más allá de esto el tiempo queda SIN OBSERVAR y el día se dibuja
# gris: la sonda externa corre cada 5 min (uptime.yml), así que un hueco de
# más de 15 min significa que ni la sonda ni un solo visitante pasaron —
# no que todo estuvo bien.
MAX_OBSERVATION_GAP_S = 900

_cache_lock = threading.Lock()
_cached: dict = {"at": 0.0, "components": None, "snapshot": None}
_last_observed_at = 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres devuelve tz-aware; SQLite (tests) devuelve naive. Comparar
    los dos tira TypeError, y esa excepción caería justo en el camino de la
    página pública."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_components(*, force: bool = False) -> tuple[list[dict], Optional[dict]]:
    """Estado por componente, cacheado. Devuelve `(components, raw_snapshot)`."""
    now = time.monotonic()
    with _cache_lock:
        if (not force and _cached["components"] is not None
                and now - _cached["at"] < _SNAPSHOT_TTL_S):
            return _cached["components"], _cached["snapshot"]
    snap = None
    try:
        from observability import health_snapshot
        snap = health_snapshot(enforce_fleet_readiness=False)
    except Exception as exc:  # pragma: no cover - defensivo
        logger.warning("status: health_snapshot falló: %s", exc)
    components = derive_components(snap)
    with _cache_lock:
        _cached["at"] = time.monotonic()
        _cached["components"] = components
        _cached["snapshot"] = snap
    return components, snap


def observe_components(db: Session, components: list[dict]) -> None:
    """Registra/extiende el tramo abierto de cada componente. Best-effort.

    Se llama desde los GET públicos. NUNCA puede hacer fallar la respuesta:
    la página tiene que seguir en pie exactamente cuando la DB está mal.
    """
    global _last_observed_at
    now_m = time.monotonic()
    with _cache_lock:
        if now_m - _last_observed_at < _OBSERVE_MIN_INTERVAL_S:
            return
        _last_observed_at = now_m
    now = utcnow()
    try:
        for comp in components:
            if comp["status"] == STATUS_UNKNOWN:
                # No se registra un tramo de "no sé": dejar el hueco es la
                # única forma de que el día salga gris en vez de inventado.
                continue
            last = (
                db.query(StatusComponentEvent)
                .filter(StatusComponentEvent.component == comp["id"])
                .order_by(StatusComponentEvent.last_seen_at.desc())
                .first()
            )
            gap = (
                (now - _aware(last.last_seen_at)).total_seconds()
                if last is not None else None
            )
            if (last is not None and last.status == comp["status"]
                    and gap is not None and 0 <= gap <= MAX_OBSERVATION_GAP_S):
                last.last_seen_at = now
                if comp.get("reason"):
                    last.reason = comp["reason"][:120]
            else:
                db.add(StatusComponentEvent(
                    component=comp["id"],
                    status=comp["status"],
                    reason=(comp.get("reason") or "")[:120] or None,
                    started_at=now,
                    last_seen_at=now,
                ))
        db.commit()
    except Exception as exc:
        logger.warning("status: no se pudo registrar la observación: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Historial de 90 días
# ---------------------------------------------------------------------------

# Cobertura mínima para pintar un día. Con la sonda externa cada 5 min un
# día sano tiene ~86.400 s observados; una hora es suficiente para no
# pintar de verde un día del que casi no se sabe nada, y suficientemente
# bajo para que el día en curso ya tenga color a la hora de arrancar.
MIN_DAY_COVERAGE_S = 3600


def uptime_history(db: Session, days: int = 90) -> dict:
    """Barras por día y % de disponibilidad por componente.

    Cómo se calcula, porque acá es donde una página de status miente:

    * La sonda aporta segundos OBSERVADOS con su estado. Lo no observado no
      se rellena: si nadie miró, el día sale `no_data` (gris), nunca verde.
    * Un incidente declarado a mano aporta su ventana completa
      (`started_at` → `resolved_at` o ahora) como no-operativa. Un humano
      que declara un incidente es mejor evidencia que una sonda que no lo
      vio, así que su ventana cuenta aunque la sonda estuviera verde.
    * Las dos fuentes se combinan por día con `max`, NUNCA sumando. Sumarlas
      dobla el denominador cuando las dos cubren el mismo rato: un día con
      86.400 s de sonda verde y 86.400 s de incidente declarado daría
      172.800 s "observados" y 50% de uptime en un día que estuvo entero
      caído.
    * `uptime_pct` = 1 − malos/observados, y viene siempre acompañado de
      `coverage_pct`. Un 100% sobre 2% de cobertura no es un 100%, y el
      frontend tiene que poder decirlo.
    """
    days = max(1, min(int(days or 90), 90))
    now = utcnow()
    today = now.date()
    start_day = today - timedelta(days=days - 1)
    window_start = _day_start(start_day)

    day_keys = [start_day + timedelta(days=i) for i in range(days)]

    def _empty_slot() -> dict:
        return {
            # Sonda
            "probe_observed": 0.0,
            "probe_bad": 0.0,
            "probe_worst": STATUS_UNKNOWN,
            # Incidente declarado a mano
            "incident_seconds": 0.0,
            "incident_worst": STATUS_UNKNOWN,
        }

    buckets: dict[str, dict] = {
        cid: {d: _empty_slot() for d in day_keys} for cid in COMPONENT_IDS
    }

    def _accumulate(cid: str, lo: Optional[datetime], hi: Optional[datetime],
                    status: str, *, source: str) -> None:
        """Reparte [lo, hi) en los días del bucket de `cid`."""
        if cid not in buckets or lo is None or hi is None:
            return
        lo = max(lo, window_start)
        hi = min(hi, now)
        if hi <= lo:
            # Observación PUNTUAL: un tramo recién abierto tiene
            # started_at == last_seen_at hasta el latido siguiente, y un
            # incidente puede declararse y resolverse en el mismo minuto.
            # Aporta 0 segundos de cobertura pero SÍ registra su estado: sin
            # esto, un outage visto una sola vez desaparece del gráfico —
            # justo el dato que el visitante vino a buscar.
            slot = buckets[cid].get(lo.date())
            if slot is not None:
                field = "probe_worst" if source == "probe" else "incident_worst"
                slot[field] = _worse(slot[field], status)
            return
        cursor = lo
        while cursor < hi:
            day = cursor.date()
            chunk_end = min(hi, _day_start(day) + timedelta(days=1))
            seconds = (chunk_end - cursor).total_seconds()
            slot = buckets[cid].get(day)
            if slot is not None and seconds > 0:
                if source == "probe":
                    slot["probe_observed"] += seconds
                    if status != "operational":
                        slot["probe_bad"] += seconds
                    slot["probe_worst"] = _worse(slot["probe_worst"], status)
                else:
                    slot["incident_seconds"] += seconds
                    slot["incident_worst"] = _worse(slot["incident_worst"], status)
            cursor = chunk_end

    try:
        events = (
            db.query(StatusComponentEvent)
            .filter(StatusComponentEvent.last_seen_at >= window_start)
            .all()
        )
    except Exception as exc:
        logger.warning("status: no se pudo leer el historial de sondas: %s", exc)
        events = []
    for ev in events:
        _accumulate(ev.component, _aware(ev.started_at), _aware(ev.last_seen_at),
                    ev.status, source="probe")

    try:
        incidents = (
            db.query(StatusIncident)
            .filter(StatusIncident.public.is_(True))
            .filter(
                (StatusIncident.resolved_at.is_(None))
                | (StatusIncident.resolved_at >= window_start)
            )
            .all()
        )
    except Exception as exc:
        logger.warning("status: no se pudieron leer los incidentes: %s", exc)
        incidents = []
    for inc in incidents:
        if inc.impact == "none":
            # Mantenimiento programado no baja el uptime: estaba anunciado.
            continue
        status = IMPACT_TO_COMPONENT_STATUS.get(inc.impact, "degraded")
        lo = _aware(inc.started_at) or window_start
        hi = _aware(inc.resolved_at) or now
        # Sin componentes declarados el incidente se lee como "toda la
        # plataforma": es lo que ve el cliente cuando el banner no aclara.
        for cid in (inc.components or COMPONENT_IDS):
            _accumulate(cid, lo, hi, status, source="incident")

    out = []
    for comp in COMPONENTS:
        cid = comp["id"]
        bars = []
        total_observed = 0.0
        total_bad = 0.0
        for d in day_keys:
            slot = buckets[cid][d]
            # Duración real del día: el de hoy va sólo hasta ahora, así que
            # `coverage_pct` no arranca en 4% cada mañana.
            day_len = max(
                0.0,
                (min(now, _day_start(d) + timedelta(days=1))
                 - _day_start(d)).total_seconds(),
            )
            probe_bad = min(slot["probe_bad"], slot["probe_observed"])
            observed = min(
                max(slot["probe_observed"], slot["incident_seconds"]),
                day_len,
            )
            bad = min(max(probe_bad, slot["incident_seconds"]), observed)
            worst = _worse(slot["probe_worst"], slot["incident_worst"])
            total_observed += observed
            total_bad += bad
            coverage_floor = min(MIN_DAY_COVERAGE_S, day_len)
            if worst == STATUS_UNKNOWN:
                bars.append({"day": d.isoformat(), "status": "no_data"})
            elif observed < coverage_floor:
                # Poca cobertura pero SÍ hay señal. Si la señal es mala se
                # publica igual —omitir el único dato malo del día por
                # falta de cobertura es esconder justo lo que importa—; si
                # es buena se marca `low_coverage` y el frontend la dibuja
                # tenue en vez de afirmar un día verde entero.
                bars.append({"day": d.isoformat(), "status": worst,
                             "low_coverage": True})
            else:
                bars.append({"day": d.isoformat(), "status": worst})
        out.append({
            "id": cid,
            "days": bars,
            "uptime_pct": (
                round(100.0 * (1.0 - (total_bad / total_observed)), 2)
                if total_observed > 0 else None
            ),
            "coverage_pct": round(
                100.0 * min(total_observed / max(_window_seconds(day_keys, now), 1.0), 1.0),
                1,
            ),
        })
    return {"days": days, "components": out}


def _day_start(day) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _window_seconds(day_keys: list, now: datetime) -> float:
    """Segundos reales de la ventana: los días completos + lo transcurrido hoy.

    Usar `days * 86400` haría que la cobertura de hoy nunca pueda llegar a
    100% y que un despliegue nuevo muestre 1% de cobertura para siempre.
    """
    if not day_keys:
        return 0.0
    return max(0.0, (now - _day_start(day_keys[0])).total_seconds())


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _incident_dict(inc: StatusIncident, updates: Optional[list] = None,
                   *, include_private: bool = False) -> dict:
    data = {
        "id": inc.id,
        "title": inc.title,
        "status": inc.status,
        "impact": inc.impact,
        "components": list(inc.components or []),
        "started_at": _iso(inc.started_at),
        "resolved_at": _iso(inc.resolved_at),
        "updated_at": _iso(inc.updated_at),
        "resolved": inc.resolved_at is not None,
    }
    if include_private:
        data["banner"] = bool(inc.banner)
        data["public"] = bool(inc.public)
        data["created_by"] = inc.created_by
        data["created_at"] = _iso(inc.created_at)
    if updates is not None:
        data["updates"] = [
            {
                "id": u.id,
                "status": u.status,
                "body": u.body,
                "created_at": _iso(u.created_at),
            }
            for u in updates
        ]
    return data


def _load_updates(db: Session, incident_ids: list[int]) -> dict[int, list]:
    """Timeline de varios incidentes, del más nuevo al más viejo.

    Una sola query para todos y no una por incidente: la página pública
    lista los abiertos más hasta 50 resueltos, y un N+1 ahí son 51 queries
    por visita en el momento de mayor tráfico de la página.
    """
    if not incident_ids:
        return {}
    try:
        rows = (
            db.query(StatusIncidentUpdate)
            .filter(StatusIncidentUpdate.incident_id.in_(incident_ids))
            .order_by(StatusIncidentUpdate.created_at.desc(),
                      StatusIncidentUpdate.id.desc())
            .all()
        )
    except Exception as exc:
        logger.warning("status: no se pudo leer el timeline: %s", exc)
        return {}
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.incident_id, []).append(row)
    return grouped


def _active_incidents(db: Session) -> list[StatusIncident]:
    """Incidentes públicos abiertos. Devuelve [] si la DB no contesta.

    NO propaga la excepción a propósito. Un 500 en esta página durante una
    caída de Postgres es el peor resultado posible: el visitante viene
    justo por eso y se encuentra con que ni la página de estado funciona.
    Sin la DB perdemos el relato humano, pero la sonda —que no la
    necesita para Redis, R2 ni las colas— sigue reportando, y `api` sale
    en `major_outage` por el propio `db: down`. Es decir: la respuesta
    sigue siendo correcta y más informativa que un error.
    """
    try:
        return (
            db.query(StatusIncident)
            .filter(StatusIncident.public.is_(True))
            .filter(StatusIncident.resolved_at.is_(None))
            .order_by(StatusIncident.started_at.desc())
            .all()
        )
    except Exception as exc:
        logger.warning("status: no se pudieron leer los incidentes abiertos: %s", exc)
        return []


def overall_indicator(components: list[dict],
                      incidents: list[StatusIncident]) -> str:
    """El peor entre lo que ve la sonda y lo que declaró un humano."""
    worst = "operational"
    for comp in components:
        worst = _worse(worst, comp["status"])
    for inc in incidents:
        worst = _worse(worst, IMPACT_TO_COMPONENT_STATUS.get(inc.impact, "degraded"))
    return worst


# Estado mínimo de la sonda para que el banner aparezca SIN que nadie haya
# redactado un incidente. Por defecto `partial_outage`: una cola con
# backlog (degraded) hace esperar, no rompe, y ponerle una barra roja a
# todos los usuarios por eso quema la señal para cuando importa. Se puede
# bajar a "degraded" con la env var si algún día se quiere más ruido.
def _auto_banner_min() -> str:
    raw = str(os.environ.get("STATUS_AUTO_BANNER_MIN", "partial_outage")).strip()
    return raw if raw in COMPONENT_STATUS_RANK else "partial_outage"


def _severity(indicator: str) -> str:
    """Estado → severidad visual del banner."""
    if indicator in ("major_outage", "partial_outage"):
        return "critical"
    if indicator == "degraded":
        return "warning"
    return "info"


def build_summary(db: Session, components: list[dict]) -> dict:
    """Payload chico para la barra de la home. Un poll cada 60 s por pestaña."""
    incidents = _active_incidents(db)
    indicator = overall_indicator(components, incidents)

    banner_incidents = [inc for inc in incidents if inc.banner]
    auto_min = COMPONENT_STATUS_RANK[_auto_banner_min()]
    auto_affected = [
        c["id"] for c in components
        if c["status"] != STATUS_UNKNOWN
        and COMPONENT_STATUS_RANK.get(c["status"], 0) >= auto_min
    ]

    incident_payload = None
    if banner_incidents:
        # El de mayor impacto manda el banner; el resto se ven en /status.
        top = max(
            banner_incidents,
            key=lambda i: (INCIDENT_IMPACTS.index(i.impact)
                           if i.impact in INCIDENT_IMPACTS else 0,
                           _aware(i.started_at) or utcnow()),
        )
        incident_payload = {
            "id": top.id,
            "title": top.title,
            "status": top.status,
            "impact": top.impact,
            "components": list(top.components or []),
            "started_at": _iso(top.started_at),
            "updated_at": _iso(top.updated_at),
        }

    show_banner = bool(incident_payload) or bool(auto_affected)
    banner_severity = "info"
    if incident_payload:
        banner_severity = _severity(
            IMPACT_TO_COMPONENT_STATUS.get(incident_payload["impact"], "degraded")
        )
    elif auto_affected:
        # Del estado real de los componentes afectados y no fijo en
        # "critical": si algún día se baja STATUS_AUTO_BANNER_MIN a
        # "degraded", un backlog de cola tiene que salir amarillo.
        worst_auto = "operational"
        for c in components:
            if c["id"] in auto_affected:
                worst_auto = _worse(worst_auto, c["status"])
        banner_severity = _severity(worst_auto)

    return {
        "indicator": indicator,
        "banner": show_banner,
        "severity": banner_severity,
        "incident": incident_payload,
        # `auto` es el caso sin relato humano: la sonda vio algo y no hay
        # nadie despierto. El frontend usa copy genérico y lista los
        # componentes.
        "auto_affected": auto_affected if not incident_payload else [],
        "open_incidents": len(incidents),
        "updated_at": _iso(utcnow()),
    }


# ---------------------------------------------------------------------------
# Endpoints públicos (sin auth)
# ---------------------------------------------------------------------------

@router.get("/summary")
def status_summary(db: Session = Depends(get_db)):
    """Payload mínimo para la barra horizontal de la home.

    Público a propósito: la landing sin login también la muestra, y quien
    tiene el token expirado por el mismo outage tiene que poder verla.
    """
    components, _ = current_components()
    observe_components(db, components)
    return build_summary(db, components)


# Cache del payload completo de la página. El resto del módulo ya cachea el
# snapshot de /health, pero el historial de 90 días es una query aparte y
# esta es LA página que recibe tráfico justo cuando la DB está sufriendo:
# un outage manda a todos los clientes acá a la vez y a refrescar. 15 s es
# más que suficiente (el banner poll​ea cada 60) y convierte una tormenta de
# queries en una cada 15 s por proceso.
_PAGE_TTL_S = 15.0
_page_cache: dict = {}


def _invalidate_page_cache() -> None:
    """Tira el cache de la página. Lo llama TODA mutación de admin.

    Sin esto, publicar un incidente y no verlo en /status durante 15 s
    convierte al operador en su propio bug report ("¿lo publiqué o no?") y
    lo empuja a publicar de nuevo. La invalidación explícita deja el cache
    haciendo sólo lo que tiene que hacer —absorber tráfico de lectura
    durante un outage— sin agregar latencia a la publicación.

    Es por proceso: con varios workers de uvicorn, el resto se pone al día
    en <=15 s. Alcanza — la alternativa (invalidar por Redis) agregaría una
    dependencia justo en el camino que tiene que sobrevivir sin ella.
    """
    with _cache_lock:
        _page_cache.clear()


@router.get("")
def status_page(days: int = Query(90, ge=1, le=90), db: Session = Depends(get_db)):
    """Payload completo de la página pública `/status`."""
    now_m = time.monotonic()
    with _cache_lock:
        hit = _page_cache.get(days)
        if hit and now_m - hit[0] < _PAGE_TTL_S:
            return hit[1]

    components, _ = current_components()
    observe_components(db, components)

    active = _active_incidents(db)
    since = utcnow() - timedelta(days=days)
    try:
        recent = (
            db.query(StatusIncident)
            .filter(StatusIncident.public.is_(True))
            .filter(StatusIncident.resolved_at.isnot(None))
            .filter(StatusIncident.resolved_at >= since)
            .order_by(StatusIncident.resolved_at.desc())
            .limit(50)
            .all()
        )
    except Exception as exc:
        # Igual que en `_active_incidents`: sin DB se pierde el historial,
        # no la página.
        logger.warning("status: no se pudo leer el historial de incidentes: %s", exc)
        recent = []
    updates = _load_updates(db, [i.id for i in active] + [i.id for i in recent])

    history = uptime_history(db, days=days)
    history_by_id = {h["id"]: h for h in history["components"]}
    derived_by_id = {c["id"]: c for c in components}

    payload = {
        "indicator": overall_indicator(components, active),
        "updated_at": _iso(utcnow()),
        "history_days": history["days"],
        "components": [
            {
                "id": comp["id"],
                "label": comp["label"],
                "description": comp["description"],
                "status": derived_by_id.get(comp["id"], {}).get(
                    "status", STATUS_UNKNOWN
                ),
                # `reason` es jerga interna (`backlog_120`, `no_consumer`):
                # no se publica. El cliente necesita el color y el nombre
                # del servicio, no el nombre de la cola.
                "uptime_pct": history_by_id.get(comp["id"], {}).get("uptime_pct"),
                "coverage_pct": history_by_id.get(comp["id"], {}).get("coverage_pct"),
                "days": history_by_id.get(comp["id"], {}).get("days", []),
            }
            for comp in COMPONENTS
        ],
        "active_incidents": [
            _incident_dict(inc, updates.get(inc.id, [])) for inc in active
        ],
        "past_incidents": [
            _incident_dict(inc, updates.get(inc.id, [])) for inc in recent
        ],
    }
    with _cache_lock:
        _page_cache[days] = (time.monotonic(), payload)
    return payload


@router.get("/incidents/{incident_id}")
def status_incident(incident_id: int, db: Session = Depends(get_db)):
    inc = (
        db.query(StatusIncident)
        .filter(StatusIncident.id == incident_id)
        .filter(StatusIncident.public.is_(True))
        .first()
    )
    if inc is None:
        raise HTTPException(status_code=404, detail="Incidente no encontrado")
    return _incident_dict(inc, _load_updates(db, [inc.id]).get(inc.id, []))


# ---------------------------------------------------------------------------
# Endpoints de admin
# ---------------------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    body: str = Field(..., min_length=1, max_length=5000)
    status: str = Field(default="investigating")
    impact: str = Field(default="minor")
    components: list[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    banner: bool = True
    public: bool = True


class IncidentPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=3, max_length=200)
    impact: Optional[str] = None
    components: Optional[list[str]] = None
    started_at: Optional[datetime] = None
    banner: Optional[bool] = None
    public: Optional[bool] = None


class IncidentUpdateCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)
    status: Optional[str] = None


def _validate_vocab(status: Optional[str], impact: Optional[str],
                    components: Optional[list[str]]) -> None:
    if status is not None and status not in INCIDENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status inválido; usá uno de {list(INCIDENT_STATUSES)}",
        )
    if impact is not None and impact not in INCIDENT_IMPACTS:
        raise HTTPException(
            status_code=422,
            detail=f"impact inválido; usá uno de {list(INCIDENT_IMPACTS)}",
        )
    if components is not None:
        unknown = [c for c in components if c not in COMPONENT_IDS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(f"componentes desconocidos: {unknown}; "
                        f"válidos: {list(COMPONENT_IDS)}"),
            )


@admin_router.get("/components")
def admin_components(admin: dict = Depends(require_admin)):
    """Estado derivado + la razón cruda de la sonda (jerga interna incluida).

    `force=True` no: el admin también consume el cache de 20 s. La tira de
    salud de /admin ya muestra /health crudo cada 15 s para el detalle.
    """
    components, snap = current_components()
    return {
        "components": [
            {**c, "label": next(x["label"] for x in COMPONENTS if x["id"] == c["id"])}
            for c in components
        ],
        "health_status": (snap or {}).get("status"),
        "auto_banner_min": _auto_banner_min(),
    }


@admin_router.get("/incidents")
def admin_list_incidents(
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(StatusIncident)
        .order_by(StatusIncident.started_at.desc())
        .limit(limit)
        .all()
    )
    updates = _load_updates(db, [r.id for r in rows])
    return {
        "incidents": [
            _incident_dict(r, updates.get(r.id, []), include_private=True)
            for r in rows
        ],
        "components": [{"id": c["id"], "label": c["label"]} for c in COMPONENTS],
    }


@admin_router.post("/incidents")
def admin_create_incident(
    body: IncidentCreate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Crea el incidente Y su primera entrada de timeline, en una transacción.

    Las dos cosas juntas y no en dos llamadas porque un incidente sin
    ninguna entrada es un título rojo sin explicación en la página pública
    — el peor estado posible: alarma al cliente y no le dice nada.
    """
    _validate_vocab(body.status, body.impact, body.components)
    now = utcnow()
    started = _aware(body.started_at) or now
    if started > now + timedelta(minutes=5):
        raise HTTPException(
            status_code=422,
            detail="started_at no puede estar en el futuro",
        )
    resolved_at = now if body.status == "resolved" else None
    inc = StatusIncident(
        title=body.title.strip(),
        status=body.status,
        impact=body.impact,
        components=list(dict.fromkeys(body.components)),
        started_at=started,
        resolved_at=resolved_at,
        # Un incidente creado ya resuelto (postmortem cargado después) nunca
        # muestra banner: avisar de algo que ya terminó sólo asusta.
        banner=bool(body.banner) and resolved_at is None,
        public=bool(body.public),
        created_by=str(admin.get("username") or admin.get("id") or "")[:100],
        created_at=now,
        updated_at=now,
    )
    db.add(inc)
    db.flush()
    db.add(StatusIncidentUpdate(
        incident_id=inc.id,
        status=body.status,
        body=body.body.strip(),
        created_by=inc.created_by,
        created_at=now,
    ))
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="status.incident.created",
        detail={
            "incident_id": inc.id,
            "title": inc.title,
            "impact": inc.impact,
            "components": inc.components,
            "banner": inc.banner,
            "public": inc.public,
        },
    ))
    db.commit()
    _invalidate_page_cache()
    db.refresh(inc)
    return _incident_dict(inc, _load_updates(db, [inc.id]).get(inc.id, []),
                          include_private=True)


@admin_router.post("/incidents/{incident_id}/updates")
def admin_add_update(
    incident_id: int,
    body: IncidentUpdateCreate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Agrega una entrada al timeline. Es la ÚNICA forma de cambiar el status.

    Cambiar el status sin publicar texto dejaría la página diciendo
    "Monitoring" sin contar qué se arregló, que es como no haber avisado.
    """
    _validate_vocab(body.status, None, None)
    inc = db.query(StatusIncident).filter(StatusIncident.id == incident_id).first()
    if inc is None:
        raise HTTPException(status_code=404, detail="Incidente no encontrado")
    now = utcnow()
    new_status = body.status or inc.status
    if new_status == "resolved":
        # Idempotente: re-resolver no mueve la marca original, que es la
        # que usa el historial de uptime.
        if inc.resolved_at is None:
            inc.resolved_at = now
        inc.banner = False
    else:
        inc.resolved_at = None
    inc.status = new_status
    inc.updated_at = now
    db.add(StatusIncidentUpdate(
        incident_id=inc.id,
        status=new_status,
        body=body.body.strip(),
        created_by=str(admin.get("username") or admin.get("id") or "")[:100],
        created_at=now,
    ))
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="status.incident.updated",
        detail={"incident_id": inc.id, "status": new_status},
    ))
    db.commit()
    _invalidate_page_cache()
    db.refresh(inc)
    return _incident_dict(inc, _load_updates(db, [inc.id]).get(inc.id, []),
                          include_private=True)


@admin_router.patch("/incidents/{incident_id}")
def admin_patch_incident(
    incident_id: int,
    body: IncidentPatch,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Edita metadata: título, impacto, componentes, ventana, banner on/off.

    NO toca el timeline: las entradas publicadas son append-only (ver el
    docstring de StatusIncidentUpdate).
    """
    _validate_vocab(None, body.impact, body.components)
    inc = db.query(StatusIncident).filter(StatusIncident.id == incident_id).first()
    if inc is None:
        raise HTTPException(status_code=404, detail="Incidente no encontrado")
    changed = {}
    if body.title is not None:
        inc.title = body.title.strip()
        changed["title"] = inc.title
    if body.impact is not None:
        inc.impact = body.impact
        changed["impact"] = inc.impact
    if body.components is not None:
        inc.components = list(dict.fromkeys(body.components))
        changed["components"] = inc.components
    if body.started_at is not None:
        started = _aware(body.started_at)
        if started > utcnow() + timedelta(minutes=5):
            raise HTTPException(status_code=422,
                                detail="started_at no puede estar en el futuro")
        inc.started_at = started
        changed["started_at"] = _iso(started)
    if body.banner is not None:
        inc.banner = bool(body.banner)
        changed["banner"] = inc.banner
    if body.public is not None:
        inc.public = bool(body.public)
        changed["public"] = inc.public
    inc.updated_at = utcnow()
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="status.incident.patched",
        detail={"incident_id": inc.id, "changed": changed},
    ))
    db.commit()
    _invalidate_page_cache()
    db.refresh(inc)
    return _incident_dict(inc, _load_updates(db, [inc.id]).get(inc.id, []),
                          include_private=True)


@admin_router.delete("/incidents/{incident_id}")
def admin_delete_incident(
    incident_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Borra el incidente y su timeline. Para el falso positivo publicado por
    error; para un incidente real usá `public=false`, que lo saca de la
    página pero conserva el registro."""
    inc = db.query(StatusIncident).filter(StatusIncident.id == incident_id).first()
    if inc is None:
        raise HTTPException(status_code=404, detail="Incidente no encontrado")
    # DELETE explícito de los hijos: el ON DELETE CASCADE de la FK no lo
    # aplica SQLite sin `PRAGMA foreign_keys=ON` (los tests corren ahí), y
    # dejar updates huérfanos rompería `_load_updates`.
    db.query(StatusIncidentUpdate).filter(
        StatusIncidentUpdate.incident_id == inc.id
    ).delete(synchronize_session=False)
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="status.incident.deleted",
        detail={"incident_id": inc.id, "title": inc.title},
    ))
    db.delete(inc)
    db.commit()
    _invalidate_page_cache()
    return {"ok": True, "deleted": incident_id}
