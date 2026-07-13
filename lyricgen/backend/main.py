"""FastAPI application for GenLy AI — Production SaaS."""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()

# --- Environment (production | staging | development) ---
# Single source of truth for "where am I running" — used by Sentry, the
# /health endpoint, and email gating so staging never sends real-looking
# mail to a real customer's inbox.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production").lower().strip() or "production"

# --- Sentry ---
# 2026-06-01 UMG-launch hardening: the inline sentry_sdk.init() that used
# to live here was being silently OVERRIDDEN by the second, lighter init
# inside observability.init_sentry() (called below) — the SDK keeps only
# the last init, so prod ran without release tag or SQLAlchemy tracing.
# All Sentry config now lives in observability.init_sentry() (single
# source of truth, shared with worker.py).

from fastapi import FastAPI, File, Form, Header, Query, UploadFile, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from auth import (
    authenticate_user,
    create_token,
    start_login_session,
    create_user,
    create_password_reset_token,
    create_email_verification_token,
    verify_password_reset_token,
    verify_email_token,
    get_current_user,
    get_current_user_from_token_param,
    get_user_by_username,
    get_user_by_email,
    get_user_by_id,
    get_plan_usage,
    ensure_default_admin,
    pwd_context,
    PLANS,
    create_media_token,
    verify_media_token,
    validate_password_strength,
    has_prores_access,
    has_drive_access,
    has_scenes_access,
    scenes_credit_cost,
    telemetry_enabled,
    generate_api_key,
    is_super_admin,
)
import storage
from datetime import datetime, timedelta, timezone

from database import (
    Job, User, UserSettings, AuditLog, APIKey, get_db, init_db,
    BackgroundAsset, AssetUsage, Delivery, DeliveryChangeRequest,
    SalesLead, UserSession, LoginSession, UiEvent, CreditGrant,
    scoped_db, pool_stats,
)
from jobs import bulk_delete_jobs, create_job, delete_job, get_job, get_all_jobs, update_job
from observability import init_sentry, init_logging, health_snapshot
from pipeline import run_pipeline, transcribe
from queue_jobs import enqueue_pipeline, enqueue_edit, queue_depth, enqueue_prores_prewarm, enqueue_drive_delivery
from render_spec import umg_catalog, validate_umg_config
from billing import router as billing_router
from admin import router as admin_router
import emails

# ---------------------------------------------------------------------------
# Logging + Sentry (structured JSON via observability; Sentry is gated on DSN)
# ---------------------------------------------------------------------------

init_logging()
init_sentry()
logger = logging.getLogger("genly")

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

app = FastAPI(
    title="GenLy AI API",
    version="2.0.0",
    docs_url="/docs" if os.environ.get("SHOW_DOCS", "true").lower() == "true" else None,
    redoc_url=None,
)

# --- Rate limiting (120 req/min default per IP via SlowAPIMiddleware) ---
from slowapi.middleware import SlowAPIMiddleware

_rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() != "false"


def _rate_limit_key(request: Request) -> str:
    """Key the upload rate limit by user_id when authenticated, falling back
    to IP for unauthenticated requests. This prevents one user's burst from
    starving another user behind the same NAT (e.g. an office), and makes
    the limit fair when UMG runs many label-team users from one location.

    The user_id is parsed from the JWT (best-effort; on parse failure we use
    IP). We don't want to do a DB hit here — slowapi calls this on every
    request, including ones that 429.
    """
    try:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(None, 1)[1]
            from auth import JWT_SECRET, JWT_ALGORITHM
            from jose import jwt as _jwt
            payload = _jwt.decode(
                token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False},
            )
            uid = payload.get("sub") or payload.get("user_id")
            if uid:
                # Distinguish user-keyed limits from IP-keyed ones so they
                # don't share a bucket when a request occasionally
                # authenticates from a previously-anonymous IP.
                return f"user:{uid}"
    except Exception:
        pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_rate_limit_key,
    enabled=_rate_limit_enabled,
    default_limits=["120/minute"],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Response compression. FastAPI doesn't enable this by default — a known
# papercut. Without it, large JSON responses (e.g. /jobs with segments,
# /admin/jobs) ship uncompressed on every request, which is wire-size
# brutal over mobile / patchy connections (the UMG operator complained
# about the dashboard being "lento" on a 4G hotspot).
#
# minimum_size=500: skip compressing tiny responses (auth tokens, 404s,
# health pings) where the gzip header overhead is bigger than the
# saving. compresslevel=5: balance between ratio (~60-70% reduction on
# JSON) and CPU. Levels >6 give diminishing returns for ~2x CPU.
#
# Browsers send `Accept-Encoding: gzip` automatically since the late
# 90s, so this is invisible to clients. Vercel already does the same
# (Brotli) for the frontend; this brings api.genly.pro to parity.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)

# --- CORS: comma-separated list, e.g. "https://app.example.com,https://admin.example.com"
# In production we refuse to start with no allowed origins — wildcard +
# credentials is what Starlette's CORSMiddleware actually emits as
# "reflect-the-Origin", which lets any site make credentialed requests.
# Local dev is permitted to fall back to wildcard *without* credentials.
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
# Vercel git-branch deploys mint a new origin per branch (e.g.
# https://genly-git-staging-tometh22s-projects.vercel.app, and the
# same shape for every fix/foo branch). Maintaining an exact-match
# allowlist for those is a moving target — every new branch breaks
# staging until someone remembers to update CORS_ORIGINS. The regex
# below is read from CORS_ORIGIN_REGEX and forwarded to Starlette's
# CORSMiddleware as `allow_origin_regex`, so any origin that matches
# the pattern is admitted in addition to the exact allowlist.
# Empty (or unset) keeps the legacy exact-only behaviour.
_cors_regex = os.environ.get("CORS_ORIGIN_REGEX", "").strip() or None

if not _ALLOWED_ORIGINS and ENVIRONMENT == "production":
    # Railway-only safety net: if CORS_ORIGINS was not copied to the service,
    # derive a strict single-origin allowlist from known deploy URLs so the
    # app can boot and answer healthchecks (instead of crashlooping).
    fallback_candidates = [
        os.environ.get("FRONTEND_URL", "").strip(),
        os.environ.get("APP_URL", "").strip(),
        os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip(),
    ]
    for candidate in fallback_candidates:
        if not candidate:
            continue
        if candidate.startswith("http://") or candidate.startswith("https://"):
            _ALLOWED_ORIGINS = [candidate.rstrip("/")]
        else:
            _ALLOWED_ORIGINS = [f"https://{candidate.rstrip('/')}" ]
        logger.warning(
            "CORS_ORIGINS missing in production; falling back to derived origin %s.",
            _ALLOWED_ORIGINS[0],
        )
        break

if not _ALLOWED_ORIGINS and not _cors_regex:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "CORS_ORIGINS or CORS_ORIGIN_REGEX must be set explicitly in "
            "production. Set CORS_ORIGINS (exact allowlist) or "
            "CORS_ORIGIN_REGEX (pattern), or FRONTEND_URL/APP_URL/"
            "RAILWAY_PUBLIC_DOMAIN for safe fallback."
        )
    # Dev: wildcard origins, but DROP credentials so we don't accidentally
    # ship the same combo to production via env-var typo.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    cors_kwargs = {
        "allow_origins": _ALLOWED_ORIGINS,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if _cors_regex:
        cors_kwargs["allow_origin_regex"] = _cors_regex
        logger.info("CORS regex enabled: %s", _cors_regex)
    app.add_middleware(CORSMiddleware, **cors_kwargs)

# --- Transient DB error retry middleware ---
# Postgres on Railway occasionally drops idle pool connections in ways
# that pool_pre_ping + TCP keepalives don't fully prevent (drops happen
# mid-query, after the pre-ping). Symptom is `psycopg2.OperationalError:
# SSL connection has been closed unexpectedly`, surfacing as a 500 to
# the client on the very first request after an idle period.
#
# SQLAlchemy auto-invalidates the dead connection on error, so the next
# checkout gets a fresh one. We just need to retry once.
#
# Implemented as raw ASGI middleware (not BaseHTTPMiddleware) because we
# need to buffer the request body before the inner app consumes it and
# then synthesize a fresh `receive` callable on retry. BaseHTTPMiddleware
# does not let you re-call the inner app with a replayed body.
_TRANSIENT_DB_MARKERS = (
    "SSL connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
)

# Hard cap on request bodies eligible for replay-on-retry. Above this we
# let the request fail naturally — buffering 50+ MB MP3 uploads into
# memory just to recover from a transient DB blip costs more than the bug.
_RETRY_BODY_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


class DbTransientRetryMiddleware:
    """Retry once if a Postgres connection drops mid-request.

    Small POST/PUT/PATCH JSON bodies are buffered up front and the inner
    app is invoked with a replay-able `receive`. On a matching
    OperationalError, we retry by invoking the inner app again with a
    fresh `receive` over the same buffered bytes.

    File uploads (multipart) and large bodies (> 1 MiB) are passed
    through verbatim, no retry — the client is expected to handle those.
    Requests that have already started streaming a response cannot be
    retried (we'd corrupt the wire), so we only retry when nothing has
    been sent to the client yet.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        body_buffered = False
        body_bytes = b""

        if method in ("POST", "PUT", "PATCH"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1", "replace")
                       for k, v in scope.get("headers", [])}
            content_type = headers.get("content-type", "")
            try:
                content_length = int(headers.get("content-length", "") or 0)
            except ValueError:
                content_length = 0
            if (not content_type.startswith("multipart/")
                    and 0 < content_length <= _RETRY_BODY_MAX_BYTES):
                # Buffer body now so we can replay on retry. Drain until
                # more_body == False (or client disconnects).
                chunks = []
                while True:
                    msg = await receive()
                    mtype = msg.get("type")
                    if mtype == "http.disconnect":
                        # Client gave up — propagate as normal disconnect.
                        await self.app(scope, _disconnect_receive, send)
                        return
                    if mtype == "http.request":
                        chunks.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            break
                body_bytes = b"".join(chunks)
                body_buffered = True

        # First attempt. Capture send so we can tell if the response
        # already started (in which case retrying is unsafe).
        response_started = False
        captured_exc = None

        async def wrapped_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        first_receive = _make_replay_receive(body_bytes) if body_buffered else receive
        try:
            await self.app(scope, first_receive, wrapped_send)
            return
        except OperationalError as e:
            captured_exc = e
            transient = any(m in str(e) for m in _TRANSIENT_DB_MARKERS)
            if not transient:
                raise
            if response_started:
                logger.warning(
                    "Transient DB error on %s %s after response started — can't retry",
                    method, scope.get("path", ""),
                )
                raise
            if method in ("POST", "PUT", "PATCH") and not body_buffered:
                logger.warning(
                    "Transient DB error on %s %s but body not buffered — not retrying",
                    method, scope.get("path", ""),
                )
                raise

        # Retry path. Fresh receive over the same body. Real send.
        logger.warning(
            "Transient DB error on %s %s — retrying once",
            method, scope.get("path", ""),
        )
        await asyncio.sleep(0.15)
        second_receive = _make_replay_receive(body_bytes) if body_buffered else receive
        try:
            await self.app(scope, second_receive, send)
        except OperationalError:
            # Second attempt also failed — surface the ORIGINAL error so
            # logs/Sentry show "this is the SSL drop case, not a fresh bug".
            assert captured_exc is not None
            raise captured_exc


def _make_replay_receive(body: bytes):
    """Return an ASGI `receive` callable that yields `body` once and
    then keeps returning http.disconnect (mirrors a closed stream)."""
    delivered = False

    async def _replay_receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return _replay_receive


async def _disconnect_receive():
    return {"type": "http.disconnect"}


app.add_middleware(DbTransientRetryMiddleware)


# --- Server-Timing header middleware ---
# 2026-05-27 Wave-0 observability audit: every response now carries
# `X-Response-Time: <ms>` so frontend RUM / DevTools Network panel can
# attribute round-trip latency between "server thought" vs "network in
# transit" vs "browser parse". Adding this is cheap (sub-millisecond
# per request) and unblocks debugging slow endpoints WITHOUT needing
# Sentry tracing access for every operator.
#
# The standard `Server-Timing` header would also work (and shows up in
# Chrome DevTools natively under Network → Timing → "Server"), but
# X-Response-Time is what existing dashboards (Datadog, Grafana) often
# parse out of the box. We emit BOTH so either consumer wins.
@app.middleware("http")
async def add_response_time_header(request: Request, call_next):
    import time as _t
    _start = _t.perf_counter()
    response = await call_next(request)
    elapsed_ms = (_t.perf_counter() - _start) * 1000.0
    # Round to 1 decimal so the header doesn't churn on every request
    # (helps caches that key on response headers — irrelevant here but
    # cheap to keep stable).
    response.headers["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    return response


# --- Include routers ---
app.include_router(billing_router)
app.include_router(admin_router)


# --- Startup ---
@app.on_event("startup")
def on_startup():
    """Initialize DB and create default admin. Also kick off the
    reaper thread so zombie jobs (worker died mid-render) get
    auto-flipped to error every 5 min — no manual cleanup, owner gets
    a digest email + Sentry alert per pass."""
    init_db()
    db = next(get_db())
    try:
        ensure_default_admin(db)
    finally:
        db.close()
    logger.info("GenLy AI started — database initialized")

    # Background reaper. Daemon → dies with the container. Single
    # instance is enough; if the API ever scales horizontally, the
    # reap_all_stuck call is idempotent (filters by status="processing"
    # so duplicate runs are no-ops on already-reaped rows).
    import time as _time
    from reaper import reap_all_stuck as _reap

    # CV3 (audit 2026-05-25) — Multi-replica coordination helper.
    # Wraps a callable in a Postgres advisory lock. Cuando hay 2+ replicas
    # API, ambas ejecutan los daemon threads (bg_cache_cleanup, outputs_
    # cleanup) — sin coordinación corren N veces por ciclo, generando
    # ruido Sentry, posibles double-deletes y emails duplicados. El
    # reaper YA tiene su propio lock interno; este helper extiende el
    # mismo patrón a los otros loops.
    _BG_PREVIEW_CLEANUP_LOCK_KEY = 9118364455199102
    _OUTPUTS_CLEANUP_LOCK_KEY = 9118364455199103

    def _run_with_advisory_lock(lock_key: int, work_fn, *, name: str = "") -> bool:
        """Execute work_fn solo si conseguimos el advisory lock.

        Returns True si lo ejecutó, False si otra replica ya tenía el lock
        (skip silencioso, normal en multi-replica). En SQLite (dev local)
        skip-loops el lock — siempre ejecuta.
        """
        from database import SessionLocal
        from sqlalchemy import text
        _db = SessionLocal()
        try:
            if _db.bind.dialect.name != "postgresql":
                work_fn()
                return True
            got = _db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": lock_key},
            ).scalar()
            if not got:
                logger.debug(
                    "[%s] another replica holds the advisory lock; skipping",
                    name or f"lock:{lock_key}",
                )
                return False
            try:
                work_fn()
                return True
            finally:
                try:
                    _db.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": lock_key},
                    )
                    _db.commit()
                except Exception:  # pragma: no cover
                    pass
        finally:
            _db.close()

    def _reaper_loop():
        # Brief delay so the very first request doesn't compete with
        # a cold-start reaper holding a DB connection.
        _time.sleep(60)
        # Heartbeat: cada sweep exitoso bumpea un timestamp que
        # /health lee. Sin esto una muerte silenciosa del thread no
        # se detecta hasta que un operador nota jobs trabados horas
        # después. Ver observability.py:mark_reaper_ok.
        from observability import mark_reaper_ok as _heartbeat
        while True:
            try:
                n = _reap()
                if n > 0:
                    logger.warning(f"reaper killed {n} stuck job(s)")
                _heartbeat()
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    # Tag so all reaper crashes group under one Sentry
                    # issue instead of fragmenting by exception type —
                    # a dying reaper means stuck jobs accumulate
                    # silently, which is an incident regardless of the
                    # specific exception that killed the sweep.
                    with sentry_sdk.push_scope() as _scope:
                        _scope.set_tag("event", "reaper.loop_crash")
                        sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)  # back off on error
            _time.sleep(300)  # 5 min between successful passes

    threading.Thread(target=_reaper_loop, daemon=True, name="reaper").start()
    logger.info("reaper thread started (threshold=100min, every 5min)")

    # bg_preview cache cleanup — Capa C 2026-05-24. Borra previews bajo
    # `bg_cache/` con más de 24h de TTL. El cache existe para que el
    # operator que pre-genera el fondo durante edit lo reuse al apretar
    # "Crear video"; si no vuelve en 24h, asumimos abandono y liberamos
    # R2 storage. Sin esto, cada operador deja N previews descartados
    # (cambió params) acumulándose forever.
    def _bg_preview_cleanup_loop():
        _time.sleep(180)  # 3min después del boot
        while True:
            try:
                def _do_cleanup():
                    from bg_preview import cleanup_old_cache
                    report = cleanup_old_cache(retention_hours=24, apply=True)
                    if report.get("deleted", 0) > 0:
                        logger.info(
                            "[BG_PREVIEW_CLEANUP] deleted %d objects (%.1f MB freed)",
                            report["deleted"], report.get("bytes_freed", 0) / 1024 / 1024,
                        )
                # CV3: gated por advisory lock — solo 1 replica corre por ciclo.
                _run_with_advisory_lock(
                    _BG_PREVIEW_CLEANUP_LOCK_KEY, _do_cleanup,
                    name="bg_preview_cleanup",
                )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(6 * 3600)   # 6h entre sweeps

    threading.Thread(target=_bg_preview_cleanup_loop, daemon=True, name="bg_preview_cleanup").start()
    logger.info("bg_preview cleanup thread started (TTL=24h, every 6h)")

    # Outputs cleanup loop. Sweeps OUTPUTS_DIR every hour to keep
    # local disk bounded — deletes jobs whose deliverables are on R2
    # and retries the upload for jobs whose R2 push failed earlier.
    # Without this, a transient R2 outage leaves multi-GB ProRes
    # masters on disk forever and Railway disk fills over weeks.
    def _outputs_cleanup_loop():
        _time.sleep(120)  # let the API come up first
        while True:
            try:
                def _do_outputs_cleanup():
                    from scripts.cleanup_old_outputs import cleanup as _cleanup_outputs
                    _cleanup_outputs()
                # CV3 (audit 2026-05-25): gated por advisory lock para que
                # con 2+ replicas API NO se borre el mismo job 2 veces.
                _run_with_advisory_lock(
                    _OUTPUTS_CLEANUP_LOCK_KEY, _do_outputs_cleanup,
                    name="outputs_cleanup",
                )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(3600)  # 1 h between passes

    threading.Thread(
        target=_outputs_cleanup_loop, daemon=True, name="outputs-cleanup",
    ).start()
    logger.info("outputs-cleanup thread started (every 1 h)")

    # Retención de telemetría de sesiones. Las rows de user_sessions crecen
    # ~1 por usuario por sesión (no por heartbeat), así que el volumen es
    # mínimo — el sweep diario igual las mantiene acotadas a 90 días, que
    # es más que suficiente ventana para el dashboard de actividad.
    _USER_SESSIONS_CLEANUP_LOCK_KEY = 9118364455199104
    _USER_SESSIONS_RETENTION_DAYS = 90

    def _user_sessions_cleanup_loop():
        _time.sleep(240)  # let the API come up first
        while True:
            try:
                def _do_sessions_cleanup():
                    from database import SessionLocal, UserSession as _US
                    _db = SessionLocal()
                    try:
                        cutoff = datetime.now(timezone.utc) - timedelta(
                            days=_USER_SESSIONS_RETENTION_DAYS)
                        deleted = (
                            _db.query(_US)
                            .filter(_US.last_seen_at < cutoff)
                            .delete(synchronize_session=False)
                        )
                        _db.commit()
                        if deleted:
                            logger.info(
                                "[SESSIONS_CLEANUP] deleted %d sessions older than %dd",
                                deleted, _USER_SESSIONS_RETENTION_DAYS,
                            )
                    finally:
                        _db.close()
                _run_with_advisory_lock(
                    _USER_SESSIONS_CLEANUP_LOCK_KEY, _do_sessions_cleanup,
                    name="user_sessions_cleanup",
                )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(24 * 3600)  # diario

    threading.Thread(
        target=_user_sessions_cleanup_loop, daemon=True, name="user-sessions-cleanup",
    ).start()
    logger.info("user-sessions-cleanup thread started (TTL=90d, daily)")

    # Guardia de masters ProRes stale (post-incidente 2026-06-10): un .mov
    # en R2 más viejo que su lyric_video.mp4 = la descarga sirve un cut
    # viejo. El fix #622 evita que se CREEN casos nuevos; este scan diario
    # detecta cualquier reaparición (o caso legacy que se nos escapó) y
    # alerta por Sentry ANTES de que lo descubra un cliente descargando.
    _STALE_PRORES_SCAN_LOCK_KEY = 9118364455199105

    def _stale_prores_scan_loop():
        _time.sleep(300)  # let the API come up first
        while True:
            try:
                def _do_stale_scan():
                    from prores import scan_stale_prores
                    scan_stale_prores(limit=300)
                _run_with_advisory_lock(
                    _STALE_PRORES_SCAN_LOCK_KEY, _do_stale_scan,
                    name="stale_prores_scan",
                )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(24 * 3600)  # diario

    threading.Thread(
        target=_stale_prores_scan_loop, daemon=True, name="stale-prores-scan",
    ).start()
    logger.info("stale-prores-scan thread started (daily)")

    # Alertas de negocio por tenant (Fase 2 panel world-class, 2026-06-11):
    # health score diario por cuenta — caída de uso WoW, spike de
    # retrabajos, tasa de error. La señal de churn que Sentry técnico no
    # da: "UMG bajó 40% su uso esta semana" tiene que sonar ANTES de que
    # el cliente lo mencione en una llamada.
    _BUSINESS_ALERTS_LOCK_KEY = 9118364455199106

    def _business_alerts_loop():
        _time.sleep(360)  # let the API come up first
        while True:
            try:
                def _do_business_alerts():
                    from admin_metrics import run_business_alerts
                    from database import SessionLocal as _SL
                    _db = _SL()
                    try:
                        run_business_alerts(_db)
                    finally:
                        _db.close()
                _run_with_advisory_lock(
                    _BUSINESS_ALERTS_LOCK_KEY, _do_business_alerts,
                    name="business_alerts",
                )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(24 * 3600)  # diario

    threading.Thread(
        target=_business_alerts_loop, daemon=True, name="business-alerts",
    ).start()
    logger.info("business-alerts thread started (daily)")

    # Tripwire de saturación del pool de Postgres — indicador LÍDER: avisa por
    # Sentry (→ Sentinel → Telegram) ANTES de que el pool se agote y tire el
    # QueuePool timeout a un cliente. El pool está topeado por el
    # max_connections del plan (ver database.py); esto es la señal para actuar
    # (PgBouncer/plan) a tiempo, no el fix del techo.
    try:
        import db_pool_watchdog
        db_pool_watchdog.start()
    except Exception as _exc:  # nunca romper el arranque por el watchdog
        logger.warning("db-pool-watchdog no arrancó: %s", _exc)


# --- Background library (public, authenticated) ---
_BACKGROUNDS_LIB = os.path.join(os.path.dirname(__file__), "..", "assets", "backgrounds", "library")


def _user_can_use_asset(asset: "BackgroundAsset", current_user: dict) -> bool:
    """Tenant gate for library assets. Admins see everything; everyone else
    can only see assets that are global (owner_tenant_id IS NULL) or owned
    by their own tenant. Backs the UMG exclusivity contract."""
    if current_user.get("role") == "admin":
        return True
    if asset.owner_tenant_id is None:
        return True
    return asset.owner_tenant_id == current_user.get("tenant_id")


def _apply_asset_tenant_filter(query, current_user: dict):
    """Add a tenant scope to a BackgroundAsset query. Admins get the
    unfiltered query back; everyone else gets `owner IS NULL OR owner = mine`.
    """
    if current_user.get("role") == "admin":
        return query
    from sqlalchemy import or_
    return query.filter(
        or_(
            BackgroundAsset.owner_tenant_id.is_(None),
            BackgroundAsset.owner_tenant_id == current_user.get("tenant_id"),
        )
    )


@app.get("/backgrounds")
def list_backgrounds(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List active pre-approved background assets visible to the caller.

    Tenant scope: a non-admin user only sees assets either marked global
    (owner_tenant_id IS NULL) or owned by their own tenant_id. Admins see
    everything for moderation/audit.
    """
    q = db.query(BackgroundAsset).filter(BackgroundAsset.is_active == True)
    q = _apply_asset_tenant_filter(q, current_user)
    assets = q.order_by(BackgroundAsset.created_at.desc()).all()
    return [a.to_dict() for a in assets]


@app.get("/backgrounds/{asset_id}/usage")
def background_usage(
    asset_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per-tenant usage summary for a library asset.

    Powers the "you already used this on [date]" warning in the picker.
    Returns whether the caller's tenant has used this asset before, the
    last-used timestamp, total use count, and per-mode breakdown so the
    UI can distinguish "used as-is" from "used as variation source".
    """
    asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
    if not asset or not _user_can_use_asset(asset, current_user):
        raise HTTPException(status_code=404, detail="Asset not found")

    tenant_id = current_user["tenant_id"]
    rows = (
        db.query(AssetUsage)
        .filter(AssetUsage.asset_id == asset_id, AssetUsage.tenant_id == tenant_id)
        .order_by(AssetUsage.used_at.desc())
        .all()
    )
    use_count = len(rows)
    last_used_at = rows[0].used_at.isoformat() if rows and rows[0].used_at else None
    as_is_count = sum(1 for r in rows if r.mode == "as_is")
    variation_count = sum(1 for r in rows if r.mode == "variation")
    return {
        "asset_id": asset_id,
        "tenant_id": tenant_id,
        "used": use_count > 0,
        "use_count": use_count,
        "as_is_count": as_is_count,
        "variation_count": variation_count,
        "last_used_at": last_used_at,
    }


@app.get("/backgrounds/usage")
def backgrounds_usage_batch(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Resumen de uso de TODOS los assets visibles, en una sola query.

    Incidente 2026-06-11: la grilla de biblioteca hacía un GET
    /backgrounds/{id}/usage POR ASSET (con 80 assets = 80 requests
    simultáneos, cada uno con su auth + queries) y el pool de Postgres
    se quedaba sin conexiones (OperationalError "SSL connection has been
    closed" en Sentry, thumbnails sin cargar). Mismo shape por asset que
    el endpoint individual, keyed por asset_id; el viejo queda por compat.

    NOTA ruta: definido ANTES en orden de matching que
    /backgrounds/{asset_id}/usage no hace falta — "usage" no parsea como
    int, así que FastAPI no lo captura como asset_id (path param int).
    """
    tenant_id = current_user["tenant_id"]
    q = db.query(BackgroundAsset.id).filter(BackgroundAsset.is_active == True)  # noqa: E712
    q = _apply_asset_tenant_filter(q, current_user)
    visible_ids = {row[0] for row in q.all()}
    if not visible_ids:
        return {"tenant_id": tenant_id, "usage": {}}
    rows = (
        db.query(AssetUsage)
        .filter(AssetUsage.asset_id.in_(visible_ids), AssetUsage.tenant_id == tenant_id)
        .order_by(AssetUsage.used_at.desc())
        .all()
    )
    usage = {}
    for r in rows:
        agg = usage.setdefault(r.asset_id, {
            "asset_id": r.asset_id,
            "tenant_id": tenant_id,
            "used": True,
            "use_count": 0,
            "as_is_count": 0,
            "variation_count": 0,
            "last_used_at": r.used_at.isoformat() if r.used_at else None,
        })
        agg["use_count"] += 1
        if r.mode == "variation":
            agg["variation_count"] += 1
        else:
            agg["as_is_count"] += 1
    return {"tenant_id": tenant_id, "usage": usage}


def _resolve_library_background(
    background_id: int,
    background_mode: str,
    current_user: dict,
    db: Session,
    job_dir: str,
    job_id: str,
):
    """Common library-asset resolver shared by /upload and /generate.

    Enforces tenant access, registers an AssetUsage row for the warning &
    audit, and returns the tuple consumed by enqueue_pipeline:
        (bg_path, bg_r2_key, variation_source_path, variation_source_r2_key)

    For mode="as_is": bg_path/bg_r2_key point at the library file directly
    so the worker uses it unchanged.
    For mode="variation": variation_source_* point at the library file and
    bg_path is None — the pipeline will extract a frame and run Veo
    image-to-video to derive a brand-new clip from it.
    """
    asset = (
        db.query(BackgroundAsset)
        .filter(BackgroundAsset.id == background_id, BackgroundAsset.is_active == True)
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Background not found.")
    if not _user_can_use_asset(asset, current_user):
        # Don't reveal whether the asset exists — same response as not found.
        raise HTTPException(status_code=404, detail="Background not found.")

    # Variation requires a video source — _extract_frame_from_video calls
    # ffprobe and explodes on stills. The UI hides the toggle for images,
    # but a direct API caller could still send the combo; fall back to
    # as_is silently rather than failing the job.
    if background_mode == "variation" and asset.file_type != "mp4":
        logger.warning(
            "asset %s is %s — falling back to as_is (variation requires video)",
            asset.id,
            asset.file_type,
        )
        background_mode = "as_is"

    bg_path = None
    bg_r2_key = None
    var_path = None
    var_r2_key = None
    bg_ext = os.path.splitext(asset.filename)[1].lower() or f".{asset.file_type}"

    if asset.filename.startswith("library/"):
        local_path = os.path.join(job_dir, f"bg_library{bg_ext}")
        if background_mode == "variation":
            var_path = local_path
            var_r2_key = asset.filename
        else:
            bg_path = local_path
            bg_r2_key = asset.filename
    else:
        local_path = os.path.join(_BACKGROUNDS_LIB, asset.filename)
        if background_mode == "variation":
            var_path = local_path
        else:
            bg_path = local_path

    # Audit + per-tenant usage warning. We log on enqueue rather than
    # waiting for render completion so the warning fires the next time
    # UMG opens the picker, even if the job ends up failing — they still
    # "used" it (the contract is about exclusive availability, not a
    # successful render).
    try:
        db.add(
            AssetUsage(
                asset_id=asset.id,
                user_id=current_user["id"],
                tenant_id=current_user["tenant_id"],
                job_id=job_id,
                mode=background_mode,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to record asset usage for asset=%s job=%s", asset.id, job_id)

    return bg_path, bg_r2_key, var_path, var_r2_key, asset.id


@app.get("/fonts")
def list_fonts(current_user: dict = Depends(get_current_user)):
    """Return the catalogue of selectable typography for the lyric video.

    The frontend renders previews directly via the Google Fonts CDN — every
    entry's google_family + google_weight matches the local TTF used by
    the worker, so the picker preview matches the rendered output.
    """
    from pipeline import _FONT_CATALOGUE
    # Strip the filename — that's a backend-only concern.
    return [
        {k: v for k, v in entry.items() if k != "filename"}
        for entry in _FONT_CATALOGUE
    ]


@app.get("/backgrounds/{asset_id}/preview")
async def preview_background(
    asset_id: int,
    token: str = Query(...),
):
    """Serve a background asset file for preview.

    When the asset lives in R2 (filename starts with `library/`), redirect
    to a short-lived signed URL so the browser fetches directly from
    Cloudflare — no streaming through uvicorn for what may be a 5 MB clip.
    Falls back to FileResponse from disk for legacy / local-only assets.

    No Depends(get_db) — scoped_db() releases the pool slot before
    the FileResponse hand-off so concurrent background grid renders
    don't queue against the pool."""
    import storage
    with scoped_db() as db:
        user = get_current_user_from_token_param(token, db)
        asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
        if not asset or not _user_can_use_asset(asset, user):
            raise HTTPException(status_code=404, detail="Asset not found")
        # Snapshot the fields we need before closing the session.
        asset_filename = asset.filename
        asset_file_type = asset.file_type

    if asset_filename.startswith("library/") and storage.is_enabled():
        url = storage.generate_signed_url(asset_filename, expiry_seconds=900)
        if url:
            return RedirectResponse(url, status_code=302)
        # If signing failed for any reason, fall through to local fallback.

    file_path = os.path.join(_BACKGROUNDS_LIB, asset_filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "video/mp4" if asset_file_type == "mp4" else f"image/{asset_file_type}"
    return FileResponse(file_path, media_type=media_type)


@app.get("/health")
async def health():
    """Full health snapshot — DB + Redis + R2 + queue + workers.

    Kept as the legacy endpoint for backward compat (dashboards,
    scripts, the existing daily-smoke + uptime workflows). NEW external
    monitors should prefer the split endpoints:
      - GET /health/live  → liveness only (cheap, no deps)
      - GET /health/ready → full readiness (same shape as /health)

    The split follows the Kubernetes liveness/readiness convention:
    liveness answers "is the process alive?" (kill+restart if not);
    readiness answers "should traffic be routed here?" (drain if not).
    External monitors hitting /health every 30s with the legacy probe
    causes 2880+ DB SELECT 1 + Redis PING + R2 HEAD calls per day per
    monitor — usually wasted load, since liveness is what they actually
    need. /health/live is the right target for those.

    Status → HTTP mapping:
      - ok, degraded, starting → 200 (LB keeps the instance in rotation)
      - down                   → 503 (LB pulls the instance out)

    "starting" is reported by health_snapshot() during the first
    STARTUP_GRACE_S seconds (default 20) when a required dependency
    (Postgres SELECT 1, Redis ping) is briefly unreachable. Without
    that grace window Railway's first healthcheck probe on a fresh
    container can fire before the SQLAlchemy pool seats its first
    socket, returning 503 and aborting the deploy 5/5 replicas. See
    observability.py:_within_startup_grace.
    """
    snap = health_snapshot()
    if snap.get("status") == "down":
        return JSONResponse(snap, status_code=503)
    return snap


@app.get("/health/live")
async def health_live():
    """Liveness probe — returns 200 if the Python process is responsive.

    Intentionally does NOT touch DB, Redis, R2, or any external system.
    Use this for high-frequency external monitors (BetterStack at 30s,
    UptimeRobot at 60s) so we don't generate ~2880 DB SELECT 1 +
    Redis PING + R2 HEAD calls per day per monitor — that's nearly all
    wasted load, since liveness is what those monitors actually need.

    A 200 here means: the FastAPI event loop is alive and serving
    requests. It does NOT mean the app is fully functional — for that,
    use /health/ready.

    No 503 path: if the process is dying (signal received), FastAPI
    drains in-flight requests during the lifespan shutdown handler and
    new requests are refused at the socket level, which already gives
    the orchestrator the "down" signal. Adding an explicit
    shutting-down branch here would require state we don't track and
    isn't needed for any current monitor.
    """
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe — returns 200 if all dependencies are reachable.

    Use this when you need to know whether the instance can actually
    serve user traffic right now: load balancer routing decisions,
    deploy gate ("is the new version ready to take traffic?"),
    operational dashboards.

    Same payload + semantics as /health (kept as alias for back-compat).
    503 when a required dependency (DB, Redis in prod) is unreachable.
    200 with status=degraded when a non-critical issue exists (low disk,
    no live workers, etc.) but service is still usable.
    """
    snap = health_snapshot()
    if snap.get("status") == "down":
        return JSONResponse(snap, status_code=503)
    return snap


# ---------------------------------------------------------------------------
# Auth endpoints (public)
# ---------------------------------------------------------------------------

# Pydantic max_length DEBE ser <= al VARCHAR(N) de la columna en
# database.py — sino el INSERT rompe con 500 antes de llegar al límite
# de Pydantic. Ver database.py:User para los Column(String(N)) reales.
#
# Hoy: username/tenant_id = 100, email = 255, password (bcrypt 72 byte
# max) = 72. password en el BaseModel admite hasta el límite bcrypt,
# y el handler valida con bcrypt al hashear (más informativo que 422).
#
# Defensa contra DoS por payload size + alineación con DB schema.
class LoginRequest(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=200)  # validado por bcrypt en handler


class RegisterRequest(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=200)
    email: str = Field(default="", max_length=255)  # DB column VARCHAR(255)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., max_length=255)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., max_length=255)  # DB column VARCHAR(255)
    password: str = Field(..., max_length=200)


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., max_length=255)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)


class DeleteAccountRequest(BaseModel):
    password: str = Field(..., max_length=200)


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(..., max_length=100)  # DB column VARCHAR(100)


class CreateLeadRequest(BaseModel):
    # Public landing form. max_length aligned with sales_leads columns.
    name: str = Field(..., max_length=255)
    email: str = Field(..., max_length=255)
    company: str = Field(default="", max_length=255)
    volume: str = Field(default="", max_length=100)
    message: str = Field(default="", max_length=5000)


@app.post("/auth/login")
@limiter.limit("10/minute")
async def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = start_login_session(db, user, request)

    # Audit
    db.add(AuditLog(
        user_id=user.id, action="auth.login",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            # Visibilidad inmediata de Insights post-login, sin esperar el
            # freshen de /auth/me. Solo UI; el gate real es el backend.
            "is_super_admin": is_super_admin(user.username, user.email, user.role),
            "tenant_id": user.tenant_id,
            "plan": user.plan_id,
            "allow_overage": getattr(user, "allow_overage", False) or False,
            "features": {
                "prores_export": has_prores_access(user),
                "scenes": has_scenes_access(user),
                "scenes_credit_cost": scenes_credit_cost(),
                "telemetry": telemetry_enabled(),
            },
        },
    }


@app.post("/api/leads")
@limiter.limit("5/minute")
async def create_lead(body: CreateLeadRequest, request: Request, db: Session = Depends(get_db)):
    """Public sales/contact lead capture from the landing form (no auth).

    Persists the lead and emails the sales inbox asynchronously. Returns
    200 even if the email fails — the DB row is the source of truth.
    """
    name = body.name.strip()
    email = body.email.strip()
    if not name or "@" not in email:
        raise HTTPException(status_code=400, detail="Name and a valid email are required")

    lead = SalesLead(
        name=name,
        email=email,
        company=(body.company or "").strip() or None,
        volume=(body.volume or "").strip() or None,
        message=(body.message or "").strip() or None,
        ip_address=request.client.host if request.client else None,
    )
    db.add(lead)
    db.commit()

    threading.Thread(
        target=emails.send_lead_notification,
        args=(name, lead.company or "", email, lead.volume or "", lead.message or ""),
        daemon=True,
    ).start()

    return {"ok": True}


@app.post("/auth/register")
@limiter.limit("5/minute")
async def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Public self-registration."""
    if len(body.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    try:
        validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        user = create_user(
            db,
            username=body.username,
            password=body.password,
            email=body.email or None,
            plan="free",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = start_login_session(db, user, request)

    # Send welcome email
    if user.email:
        threading.Thread(
            target=emails.send_welcome,
            args=(user.email, user.username),
            daemon=True,
        ).start()

        # Send verification email
        verify_token = create_email_verification_token(db, user)
        threading.Thread(
            target=emails.send_email_verification,
            args=(user.email, user.username, verify_token),
            daemon=True,
        ).start()

    # Audit
    db.add(AuditLog(
        user_id=user.id, action="auth.register",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "tenant_id": user.tenant_id,
            "plan": user.plan_id,
            "allow_overage": getattr(user, "allow_overage", False) or False,
            "features": {
                "prores_export": has_prores_access(user),
                "scenes": has_scenes_access(user),
                "scenes_credit_cost": scenes_credit_cost(),
                "telemetry": telemetry_enabled(),
            },
        },
    }


@app.post("/auth/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(body: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """Send password reset email."""
    user = get_user_by_email(db, body.email)
    # Always return OK to not leak email existence
    if user and user.email:
        token = create_password_reset_token(db, user)
        threading.Thread(
            target=emails.send_password_reset,
            args=(user.email, user.username, token),
            daemon=True,
        ).start()
    return {"ok": True, "message": "If an account exists with that email, a reset link has been sent."}


@app.post("/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(body: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """Reset password using token."""
    try:
        validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    user = verify_password_reset_token(db, body.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.hashed_password = pwd_context.hash(body.password)
    db.commit()

    return {"ok": True, "message": "Password reset successfully"}


@app.post("/auth/verify-email")
@limiter.limit("10/minute")
async def verify_email_endpoint(body: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)):
    """Verify email address."""
    user = verify_email_token(db, body.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    return {"ok": True, "message": "Email verified successfully"}


@app.get("/auth/me")
def me(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current user info, incluyendo los feature flags.

    Audit A6: antes /auth/me devolvía sólo los claims del token (sin `features`),
    así que el refresh del frontend NUNCA repoblaba `features.scenes` — un
    cliente recién habilitado quedaba bloqueado hasta re-login. Ahora calculamos
    `features` del modelo de DB (autoritativo, incl. acceso por billing_group),
    igual que /auth/login, para que un reload capte el cambio de entitlement."""
    user = get_user_by_id(db, current_user["id"])
    _u = user if user else current_user
    return {
        **current_user,
        "features": {
            "prores_export": has_prores_access(_u),
            "scenes": has_scenes_access(_u),
            "scenes_credit_cost": scenes_credit_cost(),
            "telemetry": telemetry_enabled(),
        },
    }


@app.post("/auth/refresh")
@limiter.limit("60/minute")
def refresh_token(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Issue a fresh JWT for the authenticated user without requiring re-login.

    The frontend calls this proactively when the stored token is close to
    expiry so sessions extend seamlessly without the user noticing.
    """
    user = get_user_by_id(db, current_user["id"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    # Reusar el jti del token actual → mismo dispositivo/sesión, sólo se
    # extiende el exp (no crea una sesión nueva en cada refresh).
    return {"token": create_token(user, jti=current_user.get("jti"))}


# ---------------------------------------------------------------------------
# Configuración → Perfil (nombre + avatar)
# ---------------------------------------------------------------------------

class UpdateProfileRequest(BaseModel):
    full_name: str | None = None


@app.patch("/auth/profile")
@limiter.limit("20/minute")
async def update_profile(
    body: UpdateProfileRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Actualizar el perfil propio (por ahora: nombre para mostrar)."""
    user = get_user_by_id(db, current_user["id"])
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if body.full_name is not None:
        user.full_name = body.full_name.strip()[:200] or None
    db.commit()
    db.refresh(user)
    return user.to_dict()


# Avatar: máx 5 MB, jpg/png/webp, se redimensiona a 256px y se sube a R2
# bajo avatars/. Se sirve vía GET /auth/avatar/{user_id} con signed URL.
_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


@app.post("/auth/avatar")
@limiter.limit("10/minute")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Subir/reemplazar el avatar propio."""
    if file.content_type not in _AVATAR_MIME:
        raise HTTPException(status_code=400, detail="Formato no soportado. Usá JPG, PNG o WebP.")
    raw = await file.read()
    if len(raw) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="La imagen supera los 5 MB.")
    # Redimensionar a 256px (cuadrado, recorte centrado) y normalizar a PNG.
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        side = min(img.size)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize((256, 256))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No pude procesar la imagen: {e}")

    if not storage.is_enabled():
        raise HTTPException(status_code=503, detail="Almacenamiento no disponible.")
    key = f"avatars/{current_user['id']}.png"
    if not storage.put_object_bytes(key, data, content_type="image/png"):
        raise HTTPException(status_code=502, detail="No pude guardar el avatar.")

    user = get_user_by_id(db, current_user["id"])
    user.avatar_url = key
    db.commit()
    db.refresh(user)
    return {"ok": True, "avatar_url": key}


@app.get("/auth/avatar/{user_id}")
async def get_avatar(user_id: int):
    """Redirige a una signed URL del avatar (mismo patrón que el preview
    de fondos). Público por user_id — un avatar no es información sensible
    y simplifica el render en el sidebar/admin sin pasar token en la URL."""
    with scoped_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        key = user.avatar_url if user else None
    if not key:
        raise HTTPException(status_code=404, detail="Sin avatar")
    if storage.is_enabled():
        url = storage.generate_signed_url(key, expiry_seconds=3600)
        if url:
            return RedirectResponse(url, status_code=302)
    raise HTTPException(status_code=404, detail="Avatar no disponible")


# ---------------------------------------------------------------------------
# Configuración → Dispositivos (sesiones activas + cierre remoto)
# ---------------------------------------------------------------------------

@app.get("/auth/sessions")
@limiter.limit("30/minute")
async def list_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sesiones de login del usuario (dispositivos). Marca la actual."""
    rows = (
        db.query(LoginSession)
        .filter(LoginSession.user_id == current_user["id"], LoginSession.revoked_at.is_(None))
        .order_by(LoginSession.last_seen_at.desc())
        .limit(50)
        .all()
    )
    return {"sessions": [s.to_dict(current_jti=current_user.get("jti")) for s in rows]}


@app.post("/auth/sessions/{session_id}/revoke")
@limiter.limit("30/minute")
async def revoke_session(
    session_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cerrar una sesión (propia). El token de ese dispositivo queda 401
    en su próximo request."""
    sess = (
        db.query(LoginSession)
        .filter(LoginSession.id == session_id, LoginSession.user_id == current_user["id"])
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if sess.revoked_at is None:
        sess.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True, "revoked": session_id, "was_current": sess.jti == current_user.get("jti")}


@app.post("/auth/sessions/revoke-others")
@limiter.limit("10/minute")
async def revoke_other_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cerrar todas las sesiones menos la actual."""
    now = datetime.now(timezone.utc)
    q = db.query(LoginSession).filter(
        LoginSession.user_id == current_user["id"],
        LoginSession.revoked_at.is_(None),
    )
    if current_user.get("jti"):
        q = q.filter(LoginSession.jti != current_user["jti"])
    n = q.update({LoginSession.revoked_at: now}, synchronize_session=False)
    db.commit()
    return {"ok": True, "revoked_count": n}


# ---------------------------------------------------------------------------
# Configuración → Mi equipo (solo lectura)
# ---------------------------------------------------------------------------

@app.get("/team/members")
@limiter.limit("30/minute")
async def team_members(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Compañeros del mismo workspace (tenant). Solo lectura — sin
    invitaciones. Backs Configuración → Mi equipo."""
    members = (
        db.query(User)
        .filter(User.tenant_id == current_user["tenant_id"], User.is_active == True)  # noqa: E712
        .order_by(User.username)
        .all()
    )
    return {
        "tenant_id": current_user["tenant_id"],
        "members": [
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "avatar_url": u.avatar_url,
                "role": u.role,
                "is_self": u.id == current_user["id"],
            }
            for u in members
        ],
    }


# ---------------------------------------------------------------------------
# Telemetría de sesiones (tiempo en la app)
# ---------------------------------------------------------------------------

# Gap máximo entre heartbeats para considerar que la sesión sigue viva.
# Más que esto (laptop cerrada, pestaña dormida) = sesión nueva, así el
# "tiempo en app" no acumula horas fantasma.
_SESSION_GAP = timedelta(minutes=30)


@app.post("/telemetry/heartbeat")
@limiter.limit("30/minute")
def telemetry_heartbeat(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Heartbeat de presencia del frontend (1/min mientras la pestaña está visible).

    Alimenta user_sessions, que backs el "tiempo en la app" y el "en línea
    ahora" del tab Actividad del AdminPanel.

    Contrato de resiliencia: este endpoint NUNCA devuelve 5xx — la
    telemetría es best-effort y un hiccup acá no puede generar ruido en el
    cliente ni en Sentry. Con TELEMETRY_ENABLED apagada responde 200 sin
    escribir (inerte).
    """
    if not telemetry_enabled():
        return {"ok": True, "recorded": False}

    try:
        now = datetime.now(timezone.utc)
        session = (
            db.query(UserSession)
            .filter(UserSession.user_id == current_user["id"])
            .order_by(UserSession.last_seen_at.desc())
            .first()
        )
        last_seen = session.last_seen_at if session is not None else None
        # SQLite (tests) devuelve datetimes naive aunque la columna sea
        # timezone=True; Postgres devuelve aware. Mismo guard que reaper.py.
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen is not None and (now - last_seen) <= _SESSION_GAP:
            session.last_seen_at = now
            session.heartbeats = (session.heartbeats or 1) + 1
        else:
            db.add(UserSession(
                user_id=current_user["id"],
                tenant_id=current_user.get("tenant_id") or "default",
                started_at=now,
                last_seen_at=now,
                heartbeats=1,
            ))
        db.commit()
        return {"ok": True, "recorded": True}
    except Exception as e:
        logger.warning("[TELEMETRY] heartbeat write failed for user %s: %s",
                       current_user.get("id"), e)
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": True, "recorded": False}


# Whitelist de eventos de UI aceptados por /telemetry/events. Un tipo fuera
# de la lista se descarta en silencio (no es un error del cliente: versiones
# viejas/nuevas del frontend pueden diferir durante un deploy). Mantener en
# sync con frontend/src/lib/telemetryTrack.js y con admin_insights.insights_wizard.
_UI_EVENT_TYPES = frozenset({
    "wizard.step",           # {step_from, step_to, trigger}
    "wizard.scene_mode",     # {mode}
    "wizard.style",          # {style}
    "wizard.library_filter", # {filter}
    "wizard.library_select", # {asset_id, file_type, had_used_badge}
    "wizard.library_mode",   # {asset_id, mode: as_is|variation}
    "wizard.start_review",   # {}
    "wizard.generate",       # {batch_size, mode: direct|reviewed}
    "wizard.approve_lyrics", # {segments_edited}
    "edit.entered",          # {job_id}
    "edit.submitted",        # {job_id, fields}
})
_UI_EVENTS_MAX_BATCH = 25
_UI_EVENT_DATA_MAX_BYTES = 2048


class UiEventIn(BaseModel):
    type: str
    data: dict | None = None


class UiEventsBatch(BaseModel):
    events: list[UiEventIn]


@app.post("/telemetry/events")
@limiter.limit("60/minute")
def telemetry_events(
    body: UiEventsBatch,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Batch de eventos de comportamiento del wizard (best-effort).

    Alimenta ui_events, que backs el funnel del wizard del panel Insights
    (super-admin). Mismo contrato de resiliencia que /telemetry/heartbeat:
    NUNCA 5xx, con TELEMETRY_ENABLED apagada responde 200 sin escribir.

    Defensas: batch capado a _UI_EVENTS_MAX_BATCH, event_type whitelisted,
    event_data descartado si excede _UI_EVENT_DATA_MAX_BYTES serializado —
    un cliente roto no puede inflar la tabla.
    """
    if not telemetry_enabled():
        return {"ok": True, "recorded": 0}

    try:
        now = datetime.now(timezone.utc)
        recorded = 0
        for ev in body.events[:_UI_EVENTS_MAX_BATCH]:
            if ev.type not in _UI_EVENT_TYPES:
                continue
            data = ev.data
            if data is not None:
                try:
                    if len(json.dumps(data)) > _UI_EVENT_DATA_MAX_BYTES:
                        data = None
                except (TypeError, ValueError):
                    data = None
            db.add(UiEvent(
                user_id=current_user["id"],
                tenant_id=current_user.get("tenant_id") or "default",
                event_type=ev.type,
                event_data=data,
                created_at=now,
            ))
            recorded += 1
        if recorded:
            db.commit()
        return {"ok": True, "recorded": recorded}
    except Exception as e:
        logger.warning("[TELEMETRY] events write failed for user %s: %s",
                       current_user.get("id"), e)
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": True, "recorded": 0}


@app.post("/auth/change-password")
@limiter.limit("5/minute")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the authenticated user's password."""
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not pwd_context.verify(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    try:
        validate_password_strength(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user.hashed_password = pwd_context.hash(body.new_password)
    db.add(AuditLog(
        user_id=user.id, action="auth.change_password",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    return {"ok": True}


@app.get("/auth/data-export")
async def data_export(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """GDPR data export — returns all user data as a downloadable JSON file."""
    user = db.query(User).filter(User.id == current_user["id"]).first()
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    jobs = db.query(Job).filter(Job.user_id == user.id).order_by(Job.created_at.desc()).all()
    data = {
        "account": {
            "username": user.username,
            "email": user.email,
            "plan": user.plan_id,
            "role": user.role,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "settings": settings.settings_json if settings else {},
        "jobs": [
            {
                "job_id": j.job_id,
                "artist": j.artist,
                "song_title": j.song_title,
                "status": j.status,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ],
    }
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=genly-data-export.json"},
    )


@app.delete("/auth/account")
@limiter.limit("2/minute")
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete the authenticated user's account (anonymise, deactivate)."""
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not pwd_context.verify(body.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect password")
    user.is_active = False
    user.email = None
    user.username = f"deleted_{user.id}"
    db.add(AuditLog(
        user_id=user.id, action="auth.delete_account",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    return {"ok": True}


@app.get("/auth/api-keys")
async def list_api_keys(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's active API keys (secrets never returned)."""
    keys = db.query(APIKey).filter(
        APIKey.user_id == current_user["id"],
        APIKey.is_active.is_(True),
    ).order_by(APIKey.created_at.desc()).all()
    return [
        {
            "id": k.id,
            "name": k.name,
            "prefix": k.key_prefix,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        }
        for k in keys
    ]


@app.post("/auth/api-keys")
@limiter.limit("10/minute")
async def create_api_key(
    body: CreateAPIKeyRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new API key. The full secret is returned exactly once."""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Key name is required")
    active_count = db.query(APIKey).filter(
        APIKey.user_id == current_user["id"],
        APIKey.is_active.is_(True),
    ).count()
    if active_count >= 10:
        raise HTTPException(status_code=400, detail="Maximum 10 API keys per account")
    full_key, prefix, key_hash = generate_api_key()
    key = APIKey(
        user_id=current_user["id"],
        name=body.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
    )
    db.add(key)
    db.add(AuditLog(
        user_id=current_user["id"], action="auth.api_key.create",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    db.refresh(key)
    return {
        "id": key.id,
        "name": key.name,
        "prefix": prefix,
        "key": full_key,
        "created_at": key.created_at.isoformat() if key.created_at else None,
    }


@app.delete("/auth/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke an API key by ID."""
    key = db.query(APIKey).filter(
        APIKey.id == key_id,
        APIKey.user_id == current_user["id"],
    ).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    db.add(AuditLog(
        user_id=current_user["id"], action="auth.api_key.revoke",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Google Drive integration — OAuth endpoints
# ---------------------------------------------------------------------------
# Permite al operador conectar su cuenta de Google Drive a la app para
# que el botón "Guardar en Drive" (PR-D2/D3) pueda subir ProRes
# directamente desde R2 a Drive (server-to-server, ~30x más rápido
# que el flow descargar-luego-subir desde casa).
#
# Scope: drive.file (limitado a archivos que la app crea). No requiere
# Google app verification. Ver lyricgen/backend/drive_oauth.py.


@app.get("/drive/auth-url")
async def drive_auth_url(
    current_user: dict = Depends(get_current_user),
):
    """Devuelve la URL de OAuth a la que el frontend redirige al user.
    El state token está HMAC-signed y bindea la sesión OAuth a este
    user — sin esto un atacante podría forzar callbacks a otra cuenta."""
    if not has_drive_access(current_user):
        raise HTTPException(status_code=403, detail="Drive integration not enabled for your account.")
    from drive_oauth import build_authorization_url, DriveOAuthError
    try:
        url = build_authorization_url(current_user["id"])
    except DriveOAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"auth_url": url}


@app.get("/drive/callback")
async def drive_callback(
    code: str = Query("", max_length=2048),
    state: str = Query("", max_length=2048),
    error: str = Query("", max_length=200),
    db: Session = Depends(get_db),
):
    """Callback de Google después del consent screen. Verifica el state
    (HMAC), intercambia el code por tokens, encripta y guarda el
    refresh_token en user_drive_tokens. Después redirige al frontend
    con un fragmento que el cliente parsea para mostrar 'conectado ✓'.

    Nota: este endpoint NO usa get_current_user porque Google no manda
    el JWT del user — la identidad viene del state token que firmamos
    al construir la auth URL.
    """
    from drive_oauth import (
        DriveOAuthError, exchange_code_for_tokens, encrypt_token,
        fetch_userinfo, verify_state_token,
    )
    from database import UserDriveTokens

    # Frontend public URL para redirigir tras éxito / error. Lo
    # parametrizamos via env var FRONTEND_URL si está, sino derivamos
    # del GOOGLE_OAUTH_REDIRECT_URI (mismo host base).
    frontend_url = os.environ.get(
        "FRONTEND_URL",
        "https://www.genly.pro",
    )
    success_redirect = f"{frontend_url}/settings?drive=connected"
    error_redirect = f"{frontend_url}/settings?drive=error"

    if error:
        # User cerró el consent screen o lo rechazó.
        logger.info("[drive_oauth] callback error=%s", error)
        return RedirectResponse(f"{error_redirect}&reason={error}", status_code=302)

    try:
        user_id = verify_state_token(state)
    except DriveOAuthError as e:
        logger.warning("[drive_oauth] invalid state: %s", e)
        return RedirectResponse(f"{error_redirect}&reason=invalid_state", status_code=302)

    # Canary gate: el state token vino firmado por nosotros, pero igual
    # re-chequeamos has_drive_access del user_id contenido — si su
    # acceso fue revocado entre auth-url y callback, no guardamos tokens.
    callback_user = db.query(User).filter(User.id == user_id).first()
    if not has_drive_access(callback_user):
        logger.warning("[drive_oauth] callback for user %s without drive access", user_id)
        return RedirectResponse(f"{error_redirect}&reason=not_enabled", status_code=302)

    try:
        tokens = exchange_code_for_tokens(code)
    except DriveOAuthError as e:
        logger.warning("[drive_oauth] code exchange failed: %s", e)
        return RedirectResponse(f"{error_redirect}&reason=exchange_failed", status_code=302)

    refresh_token = tokens["refresh_token"]
    scope = tokens.get("scope", "")
    access_token = tokens.get("access_token", "")

    # Userinfo es best-effort — si falla, igual guardamos los tokens.
    info = fetch_userinfo(access_token) if access_token else {}
    google_email = info.get("email")

    # Upsert: si el user ya tenía Drive conectado, sobreescribimos con
    # los tokens nuevos (caso típico: revocó en Google y reconecta).
    existing = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == user_id).first()
    encrypted = encrypt_token(refresh_token)
    if existing is None:
        existing = UserDriveTokens(
            user_id=user_id,
            encrypted_refresh_token=encrypted,
            scope=scope,
            google_email=google_email,
        )
        db.add(existing)
    else:
        existing.encrypted_refresh_token = encrypted
        existing.scope = scope
        existing.google_email = google_email
        existing.connected_at = datetime.now(timezone.utc)
    db.commit()

    return RedirectResponse(success_redirect, status_code=302)


@app.get("/drive/status")
def drive_status(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Devuelve si este user tiene Drive conectado y, si sí, qué cuenta.
    El frontend lo usa para decidir si mostrar 'Conectar' o 'Conectado
    como X — Desconectar' en Settings."""
    if not has_drive_access(current_user):
        raise HTTPException(status_code=403, detail="Drive integration not enabled for your account.")
    from database import UserDriveTokens
    row = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == current_user["id"]).first()
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "email": row.google_email,
        "connected_at": row.connected_at.isoformat() if row.connected_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@app.delete("/drive/disconnect")
async def drive_disconnect(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoca el refresh_token en Google (best-effort) y borra la row
    local. Si Google falla, igual borramos la row — el user ya no
    quiere conexión y los tokens viejos quedarán huérfanos del lado
    de Google, sin afectarnos."""
    if not has_drive_access(current_user):
        raise HTTPException(status_code=403, detail="Drive integration not enabled for your account.")
    from drive_oauth import decrypt_token, revoke_refresh_token, DriveTokenDecryptError
    from database import UserDriveTokens

    row = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == current_user["id"]).first()
    if row is None:
        return {"ok": True, "was_connected": False}

    # Best-effort revoke en Google. Si la encryption key rotó, no
    # podemos decrypt el token — igual borramos la row local.
    try:
        refresh = decrypt_token(row.encrypted_refresh_token)
        revoke_refresh_token(refresh)
    except DriveTokenDecryptError:
        logger.warning(
            "[drive_oauth] decrypt failed for user %s on disconnect — borrando row igual",
            current_user["id"],
        )

    db.delete(row)
    db.commit()
    return {"ok": True, "was_connected": True}


# ---------------------------------------------------------------------------


@app.get("/usage")
def usage(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current plan usage with overage info.

    2026-05-30 perf: cached in Redis with a 30 s TTL keyed by
    tenant_id:user_id. The counter only changes when the operator
    approves or rejects a job; both of those endpoints below call
    `cache.invalidate(cache.usage_key(...))` so the next /usage hit
    returns the live value without waiting for TTL expiry. Without
    the cache this endpoint paid a fresh DB SELECT + 150 ms LATAM↔
    Railway round-trip on EVERY mount of the sidebar usage badge.
    """
    from cache import get_or_set_json, usage_key
    key = usage_key(current_user["tenant_id"], current_user["id"])
    return get_or_set_json(
        key,
        ttl_s=30,
        compute=lambda: get_plan_usage(
            db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"),
            billing_group=current_user.get("billing_group"),
        ),
    )


@app.get("/plans")
async def list_plans():
    """Return available plans (public)."""
    return {
        k: {
            "limit": v["limit"],
            "price_per_video": v["price_per_video"],
            "monthly_price": v["monthly_price"],
            "overage_rate": v["overage_rate"],
        }
        for k, v in PLANS.items()
        if k != "unlimited"
    }


# ---------------------------------------------------------------------------
# Protected endpoints
# ---------------------------------------------------------------------------

# Default bumped 100 → 500 MB (2026-05-24): los músicos suben WAV master
# (50-150 MB típico, hasta 300 MB para temas largos en stereo 24-bit).
# El upload va directo browser → R2 vía presigned URL (no toca la API),
# así que este límite es solo guardrail server-side, no afecta memoria
# del worker FastAPI. Override vía env MAX_UPLOAD_MB.
#
# CUIDADO: el endpoint legacy /upload (multipart-form, deprecated) usa
# el mismo límite. Si reactivás /upload con MAX_UPLOAD_MB=500 vas a
# OOMear el worker — el body completo entra a memoria. Por eso el
# /upload tiene un _drain_to_spooled que va a disk arriba de 1MB, pero
# igual NO usar /upload para WAV grande; el flujo es presigned R2.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
_MP3_MAGIC_BYTES = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
_AUDIO_EXTENSIONS = (".mp3", ".wav")

_TITLE_NOISE_SUFFIXES = (
    "(Official Video)", "(Official Audio)", "(Lyric Video)",
    "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
    "(Live)", "(Lyrics)",
)


# U11 (audit Sprint 2+ 2026-05-25, re-applied 2026-05-25 post-c6efbf4 revert):
# known-artists cache para resolver ambigüedad del " - " split. La convención
# "Artist - Title" del código original asumía un orden, pero ~50% de los
# downloads reales son "Title - Artist" (export de YouTube/Spotify). Sin
# contexto DB, el parser elegía mal → metadata invertida a lrclib + Genius
# → lookups fallaban → fallback a Whisper-only sin synced lyrics.
import time as _u11_time
_KNOWN_ARTISTS_CACHE: dict[str, tuple[float, set[str]]] = {}
_KNOWN_ARTISTS_TTL_S = 300  # 5 min
_KNOWN_ARTISTS_LIMIT = 1000


def _known_artists_for_tenant(db, tenant_id: str) -> set[str]:
    """Cached lookup of distinct artist strings the tenant ya subió.
    Used by _parse_filename_artist_title to disambiguate 'X - Y' splits.
    Returns lowercase set para comparación case-insensitive."""
    if not tenant_id or db is None:
        return set()
    now = _u11_time.time()
    cached = _KNOWN_ARTISTS_CACHE.get(tenant_id)
    if cached and (now - cached[0]) < _KNOWN_ARTISTS_TTL_S:
        return cached[1]
    try:
        from database import Job
        rows = (
            db.query(Job.artist)
            .filter(Job.tenant_id == tenant_id)
            .filter(Job.artist.isnot(None))
            .filter(Job.artist != "")
            .distinct()
            .limit(_KNOWN_ARTISTS_LIMIT)
            .all()
        )
        artists = {r[0].strip().lower() for r in rows if r[0] and r[0].strip()}
    except Exception:
        artists = set()
    _KNOWN_ARTISTS_CACHE[tenant_id] = (now, artists)
    return artists


# Track-number prefix patterns: "01 ", "01-", "01.", "1. ", "01-Title" etc.
# NO incluye underscore en el class (choca con convención Title_Artist).
import re as _u11_re
_TRACK_NUMBER_PREFIX = _u11_re.compile(r"^\s*\d{1,3}[\s\-.]+")


def _parse_filename_artist_title(
    filename: str,
    *,
    db=None,
    tenant_id: str = "",
) -> tuple[str, str]:
    """Best-effort artist/title extraction from a bare filename.

    U11 fix (audit 2026-05-25): el código histórico asumía que " - " split
    significaba "Artist - Title" pero ~50% de los downloads del operador
    son "Title - Artist" (YouTube / Spotify export convention). Cuando se
    pasa `db` + `tenant_id`, consultamos los artists conocidos del tenant
    para elegir el orden correcto. Sin DB, fallback a la heurística histórica.

    Convenciones soportadas:
      "Artist - Title.ext"  → ("Artist", "Title")   ← convención A (default)
      "Title - Artist.ext"  → ("Artist", "Title")   ← convención B (con DB lookup)
      "Title_Artist.ext"    → ("Artist", "Title")   ← YouTube/Suno export
      "05 Title.ext"        → ("", "Title")         ← track-number prefix stripped
      "05 - Artist - Title" → handled del mismo modo

    Falls back to ("", basename) cuando no hay separator. El operator UI
    siempre permite corregir antes de generar.
    """
    if not filename:
        return "", ""
    base = filename
    for ext in (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    # U11: strip leading track number (e.g. "05 Intuicion M" → "Intuicion M").
    base = _TRACK_NUMBER_PREFIX.sub("", base).strip()

    artist, title = "", base.strip()
    if " - " in base:
        head, _, tail = base.partition(" - ")
        head, tail = head.strip(), tail.strip()
        # U11: si tenemos contexto DB, decidir qué lado es el artist
        # consultando known-artists del tenant. Si UNO matchea, ese es
        # el artist. Si AMBOS o NINGUNO matchean → fallback al default
        # histórico (head=artist).
        known = _known_artists_for_tenant(db, tenant_id) if tenant_id else set()
        head_known = head.lower() in known
        tail_known = tail.lower() in known
        if tail_known and not head_known:
            # "Title - Artist" pattern (operator already used this artist).
            artist, title = tail, head
        else:
            # Default: "Artist - Title" (head=artist) OR ambiguous.
            artist, title = head, tail
    elif "_" in base:
        head, _, tail = base.partition("_")
        title, artist = head.strip(), tail.strip()
    for sfx in _TITLE_NOISE_SUFFIXES:
        title = title.replace(sfx, "").strip()
    # U11: strip "(1)", "(2)" copy suffixes from title (e.g. "Ser Anti (1)" → "Ser Anti").
    title = _u11_re.sub(r"\s*\(\d+\)\s*$", "", title).strip()
    return artist, title


def _validate_audio_upload(file, data: bytes) -> None:
    """Validate a freshly-read audio payload (MP3 or WAV). Raises 400 on
    any problem. Magic-bytes check supplements the extension check so a
    renamed file gets caught.

    UMG uploads lossless WAV; everyone else uploads MP3. Both are valid
    inputs to the rest of the pipeline (Whisper, moviepy, ffmpeg all
    handle either format). Whisper-API has a hard 25 MB limit which is
    handled separately at transcribe time — see _transcribe_via_openai_api.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    name_lower = file.filename.lower()
    if not name_lower.endswith(_AUDIO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="Only MP3 and WAV files are accepted.",
        )
    size_mb = len(data) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max allowed: {MAX_UPLOAD_MB} MB.",
        )
    if name_lower.endswith(".mp3"):
        if not data.startswith(_MP3_MAGIC_BYTES):
            raise HTTPException(
                status_code=400,
                detail="File does not look like a valid MP3 (magic bytes check failed).",
            )
    elif name_lower.endswith(".wav"):
        # WAV files start with "RIFF" + 4 bytes size + "WAVE".
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise HTTPException(
                status_code=400,
                detail="File does not look like a valid WAV (RIFF/WAVE header check failed).",
            )


# Back-compat alias — older call sites still reference the MP3 name.
_validate_mp3_upload = _validate_audio_upload


# Streaming-upload chunk size. 1 MiB strikes a balance between syscall
# overhead and memory footprint — small enough that 50 concurrent
# uploads still fit in 256 MiB of buffers, large enough that the read /
# write loop isn't dominated by Python overhead.
_UPLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB


async def _stream_upload_to_disk(file, dest_path: str, *, max_mb: int = None) -> int:
    """Stream `file` (Starlette UploadFile) to `dest_path` in 1 MiB chunks
    and return the number of bytes written.

    Replaces the previous `data = await file.read(); open(...).write(data)`
    pattern, which buffered the entire body in RAM. On lossless WAV
    uploads (~30-50 MB) and concurrent batches (3 users × 5 tracks ≈ 750
    MB of buffers), the old pattern OOMed the API container; Railway
    returned 502 with no CORS headers and the operator saw only a
    generic error.

    Acquires a shared upload slot via Redis so simultaneous uploads
    across replicas can't burst past `MAX_CONCURRENT_UPLOADS`. Raises
    503 + Retry-After on concurrency cap, 413 if the body exceeds
    `max_mb` (defaults to `MAX_UPLOAD_MB`). The partial file is unlinked
    before raising so a refused upload doesn't leave half-written bytes.
    """
    if max_mb is None:
        max_mb = MAX_UPLOAD_MB
    limit = max_mb * 1024 * 1024
    size = 0
    lease = _try_acquire_upload_slot()
    f = open(dest_path, "wb")
    try:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                f.close()
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (>{max_mb} MB).",
                )
            f.write(chunk)
    finally:
        if not f.closed:
            f.close()
        _release_upload_slot(lease)
    return size


def _validate_audio_file_on_disk(filename: str, path: str) -> None:
    """Header-only audio validation that reads the first 16 bytes off
    disk instead of the full body. Mirrors `_validate_audio_upload` but
    without the in-memory size check — `_stream_upload_to_disk` handles
    that on the way in."""
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    name_lower = filename.lower()
    if not name_lower.endswith(_AUDIO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="Only MP3 and WAV files are accepted.",
        )
    try:
        with open(path, "rb") as fh:
            header = fh.read(16)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read uploaded file for validation: {e}",
        )
    if name_lower.endswith(".mp3"):
        if not header.startswith(_MP3_MAGIC_BYTES):
            try:
                os.unlink(path)
            except OSError:
                pass
            raise HTTPException(
                status_code=400,
                detail="File does not look like a valid MP3 (magic bytes check failed).",
            )
    elif name_lower.endswith(".wav"):
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            try:
                os.unlink(path)
            except OSError:
                pass
            raise HTTPException(
                status_code=400,
                detail="File does not look like a valid WAV (RIFF/WAVE header check failed).",
            )


# --- Fondo personalizado (audit 2026-06-11: lifecycle de imagen subida) ----
#
# Antes de este fix el fondo custom solo validaba la EXTENSIÓN (un HEIC
# renombrado .jpg entraba y reventaba a mitad de pipeline) y no tenía cap
# de tamaño (asimetría con el audio, que valida magic bytes y tamaño desde
# siempre). Además el archivo subido quedaba huérfano del ciclo de vida:
# bg_r2_key_cached nunca se seteaba para fondos humanos → los edits
# rápidos (typography/lyrics/metadata) devolvían 400 y /retry regeneraba
# con Veo pisando la imagen del usuario.
_BG_EXTENSIONS = (".mp4", ".mov", ".jpg", ".jpeg", ".png")
MAX_BG_IMAGE_MB = int(os.environ.get("MAX_BG_IMAGE_MB", "25"))
# Los videos de fondo comparten el techo global de upload (MAX_UPLOAD_MB).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _validate_background_file_on_disk(filename: str, path: str) -> None:
    """Magic-bytes + size validation para el fondo custom, espejo de
    `_validate_audio_file_on_disk`. Lanza 400 con mensaje claro y borra
    el archivo inválido — nunca dejarlo seguir al pipeline, donde el
    error aparecería recién a mitad de render como "El render falló"."""
    name_lower = (filename or "").lower()
    ext = os.path.splitext(name_lower)[1]

    def _reject(detail: str):
        try:
            os.unlink(path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=detail)

    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        with open(path, "rb") as fh:
            header = fh.read(16)
    except OSError as e:
        _reject(f"Could not read uploaded background for validation: {e}")

    if ext in (".jpg", ".jpeg", ".png"):
        if size_mb > MAX_BG_IMAGE_MB:
            _reject(
                f"Background image too large ({size_mb:.1f} MB). "
                f"Max allowed: {MAX_BG_IMAGE_MB} MB."
            )
        if ext == ".png":
            if not header.startswith(_PNG_MAGIC):
                _reject("File does not look like a valid PNG (magic bytes check failed).")
        elif not header.startswith(_JPEG_MAGIC):
            # Cubre el caso real: HEIC/WebP renombrado a .jpg.
            _reject("File does not look like a valid JPEG (magic bytes check failed).")
    else:  # .mp4 / .mov
        if size_mb > MAX_UPLOAD_MB:
            _reject(
                f"Background video too large ({size_mb:.1f} MB). "
                f"Max allowed: {MAX_UPLOAD_MB} MB."
            )
        # ISO-BMFF: el box `ftyp` vive en los bytes 4..8 en cualquier
        # MP4/MOV bien formado de uploads reales.
        if header[4:8] != b"ftyp":
            _reject("File does not look like a valid MP4/MOV (magic bytes check failed).")


def _save_custom_background(background_file, job_dir: str, job_id: str, tenant_id: str):
    """Materializa el fondo subido por el usuario: valida (extensión +
    magic bytes + tamaño), escribe a disco, sube a R2 y — clave del fix
    2026-06-11 — persiste la key en `bg_r2_key_cached` para que los edits
    rápidos y /retry preserven el archivo del usuario en vez de
    regenerar con Veo. Devuelve (bg_path, bg_r2_key) o (None, None) si
    no vino archivo."""
    if not (background_file and background_file.filename):
        return None, None
    bg_ext = os.path.splitext(background_file.filename)[1].lower()
    if bg_ext not in _BG_EXTENSIONS:
        # Antes se ignoraba en silencio y el job salía con fondo IA — el
        # usuario creía que su archivo se usó. Rechazo explícito.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported background format '{bg_ext or '?'}'. "
                "Use MP4, MOV, JPG or PNG."
            ),
        )
    bg_filename = f"bg_custom{bg_ext}"
    bg_path = os.path.join(job_dir, bg_filename)
    with open(bg_path, "wb") as f:
        shutil.copyfileobj(background_file.file, f)
    _validate_background_file_on_disk(background_file.filename, bg_path)
    bg_r2_key = None
    if storage.is_enabled():
        bg_r2_key = storage.upload_input(bg_path, tenant_id, job_id, bg_filename)
        if bg_r2_key:
            # El archivo humano YA vive en R2: esa misma key habilita el
            # fast-path de edits (gate en request_edit) y la preservación
            # en /retry (preserved_bg_r2_key) desde el minuto cero. Si el
            # fondo termina animado con Veo, el pipeline re-cachea el clip
            # resultante encima (mejor aún: el edit reusa la animación).
            try:
                update_job(job_id, bg_r2_key_cached=bg_r2_key)
            except Exception as e:
                logger.warning("[BG] could not persist bg_r2_key_cached for %s: %s", job_id, e)
    return bg_path, bg_r2_key


def _clamp_title_size(raw) -> float:
    """Parse + clamp the title-card size multiplier (Full Rotor v1) to
    0.5–2.0. Tolerates junk/empty input → 1.0 (no scaling)."""
    try:
        return max(0.5, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return 1.0


def _job_scope(current_user: dict) -> dict:
    """Return kwargs for jobs.get_job / jobs.get_all_jobs scoping reads
    to the caller's tenant.

    The product model treats `tenant_id` as a team workspace: every user
    explicitly placed into a tenant (via `create_user(..., tenant_id=...)`
    or via admin assignment) is meant to see every other team member's
    jobs in that workspace. Self-registered users get a tenant derived
    from their username (see auth.create_user) so they don't share with
    strangers by accident. We therefore scope by tenant_id only — see
    tests/test_tenant_isolation.py::test_two_users_same_tenant_share_jobs
    for the contract this enforces.
    """
    # Cross-tenant para admins (pedido CEO 2026-06-11): el rol admin es de
    # PLATAFORMA, no de tenant — necesita abrir el video de cualquier
    # cliente para verificar incidentes con sus propios ojos (caso UMG
    # Chile: el link /videos/{id} de otro tenant daba 404 incluso para el
    # dueño de la empresa). El acceso a media queda auditado en
    # /media-token y /download vía _audit_cross_tenant_access.
    if current_user.get("role") == "admin":
        # Explícito, no kwargs vacíos: get_all_jobs tiene default
        # tenant_id="default" y con {} el admin veía SOLO ese tenant
        # (historial frizado en jun-02). None = cross-tenant real, mismo
        # contrato que get_job.
        return {"tenant_id": None}
    return {"tenant_id": current_user["tenant_id"]}


def _audit_cross_tenant_access(db: Session, current_user: dict, job: dict, kind: str) -> None:
    """Deja rastro cuando un admin accede a media de OTRO tenant.

    Parte del contrato de la apertura cross-tenant: la visibilidad de
    plataforma para admins viene con trail de auditoría (compliance UMG).
    Best-effort: un fallo acá no bloquea el acceso."""
    try:
        if current_user.get("role") != "admin":
            return
        job_tenant = job.get("tenant_id") if isinstance(job, dict) else getattr(job, "tenant_id", None)
        if not job_tenant or job_tenant == current_user.get("tenant_id"):
            return
        db.add(AuditLog(
            user_id=current_user["id"],
            action="admin.cross_tenant_access",
            detail={
                "job_id": job.get("job_id") if isinstance(job, dict) else job.job_id,
                "job_tenant": job_tenant,
                "kind": kind,
            },
        ))
        db.commit()
    except Exception as e:
        logger.warning("[AUDIT] cross-tenant access log failed: %s", e)


def _lock_user_for_quota(db: Session, user_id: int) -> None:
    """Take a row-level lock on the user so the count → insert sequence
    in /upload becomes atomic.

    Without this, two concurrent uploads at limit-1 both pass the count
    check before either inserts the new Job row, and the tenant exceeds
    its quota by N. Postgres SELECT ... FOR UPDATE serializes the reads
    on the user row; the lock is released when the request's transaction
    commits or rolls back. SQLite (used by tests) ignores FOR UPDATE.
    """
    if "sqlite" in str(db.bind.url):
        return
    db.execute(
        User.__table__.select().where(User.id == user_id).with_for_update()
    ).first()


def _try_send_usage_alert(db: Session, current_user: dict, usage: dict) -> None:
    """Fire a usage-alert email at the 80% and 100% thresholds — once per
    threshold per calendar month per user.  Uses AuditLog for deduplication so
    concurrent requests at the same quota level don't fan-out duplicate mail.
    Best-effort: any exception is swallowed so it never blocks a job submit.
    """
    try:
        percent = usage["percent"]
        if percent < 80:
            return
        action = "usage_alert_100" if percent >= 100 else "usage_alert_80"

        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        already_sent = db.query(AuditLog).filter(
            AuditLog.user_id == current_user["id"],
            AuditLog.action == action,
            AuditLog.created_at >= month_start,
        ).first()
        if already_sent:
            return

        user_obj = db.query(User).filter(User.id == current_user["id"]).first()
        if not user_obj or not user_obj.email:
            return

        notif_key = "notif_quota_100" if percent >= 100 else "notif_quota_80"
        user_settings = db.query(UserSettings).filter(
            UserSettings.user_id == user_obj.id
        ).first()
        prefs = (user_settings.settings_json or {}) if user_settings else {}
        if not prefs.get(notif_key, True):
            return

        db.add(AuditLog(user_id=user_obj.id, action=action, detail={"percent": percent}))
        db.commit()

        threading.Thread(
            target=emails.send_usage_alert,
            kwargs={
                "email": user_obj.email,
                "username": user_obj.username,
                "percent": percent,
                "used": usage["used"],
                "limit": usage["limit"],
                "plan": usage["plan"],
            },
            daemon=True,
        ).start()
    except Exception as _e:
        logger.warning("usage alert skipped: %s", _e)


def _enforce_plan_quota(db: Session, current_user: dict,
                        credits_needed: int = 1) -> None:
    """Raise 402 if the account can't cover the video about to be generated.

    The message is operator-facing (UMG, label teams). It avoids
    backend-y phrasing ("plan", "overage") and points at a human
    contact path so the operator knows what to do — keeping it
    blocking but not a dead-end.

    `credits_needed` es el peso del video que se está por generar: 1 normal,
    scenes_credit_cost() cuando viene con Escenas. El gate compara contra
    `total_available` (cupo del plan + regalo vigente), el mismo número que
    muestra el medidor — así un video de Escenas no arranca con menos
    créditos que su costo (antes bastaba "queda al menos 1" y la cuenta
    terminaba en overage sin aviso), y un regalo emitido a mitad de mes
    desbloquea a una cuenta que ya había agotado el plan.
    """
    plan = current_user.get("plan", "100")
    tenant_id = current_user["tenant_id"]
    _lock_user_for_quota(db, current_user["id"])
    usage = get_plan_usage(db, current_user["id"], tenant_id, plan,
                           billing_group=current_user.get("billing_group"))
    if plan != "unlimited" and usage["percent"] >= 80:
        _try_send_usage_alert(db, current_user, usage)
    available = usage["total_available"]
    if available < credits_needed and plan != "unlimited":
        if not current_user.get("allow_overage", False):
            support_email = os.environ.get("SUPPORT_EMAIL", "soporte@genly.pro")
            if credits_needed > 1 and available > 0:
                raise HTTPException(
                    status_code=402,
                    detail=(
                        f"Un video con Escenas consume {credits_needed} créditos "
                        f"y a tu cuenta le queda{'n' if available != 1 else ''} "
                        f"{available}. Generalo sin Escenas, o contactá a "
                        f"{support_email} para extender el cupo."
                    ),
                )
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Llegaste al límite mensual de {usage['limit']} videos "
                    f"({usage['used']} usados este mes). "
                    f"Para extender el cupo, contactá a {support_email}."
                ),
            )


# System default for per-tenant daily cap when User.max_videos_per_day is None.
# Catches accidental burst usage (a UMG user looping a script, accidental retry
# storm, etc.) before it racks up Veo bills. UMG's verbal commitment is
# 200/month ≈ 7/day; el default histórico de 50 daba 7× headroom pero se
# saturaba durante smoke tests (2026-05-24: tomas hit 50/50 probando el
# wizard refactor). Bumpeado a 500 con env override y bypass explícito para
# plan="unlimited" (los unlimited NO deberían tener cap diario).
DEFAULT_DAILY_CAP = int(os.environ.get("DAILY_VOLUME_CAP", "500"))


DEFAULT_MAX_CONCURRENT_JOBS = 5

# System-wide ceiling. Sum of `processing` jobs across ALL tenants cannot
# exceed this, even if each individual tenant is below their own cap.
# Sized at ~2× the worker replica count (3) — enough burst headroom that
# workers never sit idle, but small enough that a multi-tenant flood
# cannot saturate the worker pool and starve the premium customer.
# Override via env GLOBAL_MAX_PROCESSING for capacity tuning during scale-up.
GLOBAL_MAX_PROCESSING = int(os.environ.get("GLOBAL_MAX_PROCESSING", "8"))


def _enforce_concurrent_jobs_cap(*_, **__) -> None:
    """Deprecated. Concurrency is now bounded naturally by the RQ worker
    pool — every submission is accepted with status="queued" and the
    worker flips it to "processing" the moment it picks the job off the
    queue. Kept as a no-op so any forgotten callsite is harmless."""
    return None


# Soft caps on jobs that need attention (queued + processing +
# pending_review). Two layers:
#   * USER_BACKLOG_LIMIT:   one user (operator) can have N jobs in-flight.
#     Matches the 5-batch ceiling Tomi committed to UMG per operator.
#   * TENANT_BACKLOG_LIMIT: the whole tenant (e.g. Universal with 3
#     operators) can have M jobs in-flight. Default = 5x USER limit so
#     up to 5 operators can be at full throughput without colliding.
# Admins bypass both for test seeding. Both limits are env-tunable so
# enterprise tenants can be raised without a redeploy.
USER_BACKLOG_LIMIT = int(os.environ.get("USER_BACKLOG_LIMIT", "5"))
TENANT_BACKLOG_LIMIT = int(os.environ.get("TENANT_BACKLOG_LIMIT", str(USER_BACKLOG_LIMIT * 5)))

_BACKLOG_STATUSES = [
    "awaiting_upload", "queued", "processing", "pending_review",
]


def _enforce_tenant_backlog(db: Session, current_user: dict) -> None:
    """Two-layer backlog gate. Per-user fires first so a single operator
    can't monopolise their tenant's tenant-wide quota; per-tenant catches
    the case where multiple operators collectively saturate.
    """
    # Admins are exempt — they may legitimately seed many test jobs.
    if current_user.get("role") == "admin":
        return
    tenant_id = current_user["tenant_id"]
    user_id = current_user["id"]

    # Per-user check first (faster to fail and more relevant feedback).
    user_in_flight = (
        db.query(Job)
        .filter(Job.user_id == user_id)
        .filter(Job.status.in_(_BACKLOG_STATUSES))
        .count()
    )
    if user_in_flight >= USER_BACKLOG_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenés {user_in_flight} videos en proceso o pendientes de "
                f"revisión (límite: {USER_BACKLOG_LIMIT} por usuario). "
                f"Aprobá o rechazá algunos antes de subir más."
            ),
        )

    # Per-tenant check second.
    tenant_in_flight = (
        db.query(Job)
        .filter(Job.tenant_id == tenant_id)
        .filter(Job.status.in_(_BACKLOG_STATUSES))
        .count()
    )
    if tenant_in_flight >= TENANT_BACKLOG_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tu equipo tiene {tenant_in_flight} videos en proceso o "
                f"pendientes de revisión (límite: {TENANT_BACKLOG_LIMIT} por "
                f"equipo). Esperá a que se completen algunos antes de subir más."
            ),
        )


def _enforce_daily_volume_cap(db: Session, current_user: dict) -> None:
    """Raise 429 if the tenant has hit its per-day video cap. UMG-readiness:
    prevents a runaway from creating $200 of Veo in an hour.

    Bypass: plan="unlimited" no tiene cap diario (por definición). El control
    de costo en unlimited vive en el budget anual / billing aparte.
    """
    plan = (current_user.get("plan") or "").strip().lower()
    if plan == "unlimited":
        return

    tenant_id = current_user["tenant_id"]
    user_model = db.query(User).filter(User.id == current_user["id"]).first()

    cap = (user_model.max_videos_per_day if user_model
           and user_model.max_videos_per_day is not None
           else DEFAULT_DAILY_CAP)

    # Count jobs created in the last 24 hours, regardless of status (queueing
    # 100 broken jobs in an hour still wastes resources and signals abuse).
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    used_today = (
        db.query(Job)
        .filter(Job.tenant_id == tenant_id)
        .filter(Job.created_at >= since)
        .count()
    )

    if used_today >= cap:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily volume cap reached ({used_today}/{cap} in the last 24h). "
                "Try again later, or contact support to increase your cap."
            ),
        )


# Minimum free disk to accept a new upload. A single 4K@60 UMG render
# needs ~3-5 GB of working space (source MP4 + ProRes master + short).
# Refuse new work below this threshold so ffmpeg never trips ENOSPC
# mid-render — that's the worst failure mode (corrupt output, dangling
# .tmp files, undeletable locks). The outputs-cleanup loop should free
# space within minutes; client retries succeed once it does.
_MIN_FREE_DISK_GB_FOR_UPLOAD = float(
    os.environ.get("MIN_FREE_DISK_GB_FOR_UPLOAD", "5")
)


def _enforce_disk_capacity() -> None:
    """503 when local disk is too low to safely take another job.

    The outputs-cleanup loop running in main reclaims space from
    completed jobs / failed R2 uploads. If it can't keep up (hardware
    full, R2 down for hours), we'd rather refuse new uploads than
    half-render a UMG master and corrupt the deliverable.
    """
    try:
        du = shutil.disk_usage(OUTPUTS_DIR)
    except OSError:
        return  # disk usage unavailable → don't block uploads on it
    free_gb = du.free / 1024 / 1024 / 1024
    if free_gb < _MIN_FREE_DISK_GB_FOR_UPLOAD:
        logger.error(
            "/upload refused: only %.1f GB free, minimum %.1f. Cleanup loop "
            "should reclaim space soon; retry in a few minutes.",
            free_gb, _MIN_FREE_DISK_GB_FOR_UPLOAD,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Servidor sin espacio en disco temporalmente. La limpieza "
                "automática se ejecuta cada hora; reintentá en unos minutos."
            ),
            headers={"Retry-After": "300"},
        )


# Memory pressure gate. We refuse new uploads when the API container is
# already close to its memory cap so a 30-50 MB WAV being streamed in
# doesn't push uvicorn into an OOM kill (Railway then returns 502 with
# no CORS headers and the operator only sees a generic error). Set
# above the streaming overhead headroom (~5%) so we leave room for the
# upload itself.
_MAX_MEMORY_PERCENT = float(os.environ.get("MAX_MEMORY_PERCENT", "85"))


def _enforce_memory_pressure() -> None:
    """503 + Retry-After when API container memory is above the cap.
    Best-effort: psutil missing or read failure → don't block uploads."""
    try:
        import psutil
    except ImportError:
        return
    try:
        pct = psutil.virtual_memory().percent
    except Exception:
        return
    if pct >= _MAX_MEMORY_PERCENT:
        logger.warning(
            "/upload refused: memory at %.1f%% (cap %.1f%%). "
            "The frontend's 503 retry path will pick this up.",
            pct, _MAX_MEMORY_PERCENT,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Servidor saturado momentáneamente. Reintentamos solos en "
                "unos minutos."
            ),
            headers={"Retry-After": "60"},
        )


# Concurrent-upload counter. With the streaming refactor each upload
# costs ~1 MiB of RAM regardless of file size, AND uploads >50MB go
# direct browser->R2 (zero API container bandwidth/memory). Capping the
# count gives a hard ceiling across replicas (Redis-shared) so a burst
# from a single tenant can't melt the API even if memory_percent hasn't
# crossed the threshold yet. Default raised from the original 8 to 32
# so a multi-tenant burst (e.g. 6 paying clients × 5 simultaneous
# uploads each) doesn't block at the slot counter. Tune via env var
# without redeploying. Disabled when Redis is missing (dev / tests) —
# the memory gate above still applies in that case.
_MAX_CONCURRENT_UPLOADS = int(os.environ.get("MAX_CONCURRENT_UPLOADS", "32"))
_UPLOAD_LEASE_TTL_S = int(os.environ.get("UPLOAD_LEASE_TTL_S", "600"))
_UPLOAD_COUNTER_KEY = "uploads:in_flight"

# Global cap on simultaneous inline Whisper runs. Whisper loads a model
# into memory (~500 MB for base/small) and keeps it for the duration of
# the request. Without a global ceiling, N users transcribing at the same
# time spike memory together, each passing the per-request 85% gate in a
# race, and then collectively push the container into OOM. Two concurrent
# transcriptions is the safe ceiling for a 1-2 GB API container.
_MAX_CONCURRENT_TRANSCRIPTIONS = int(os.environ.get("MAX_CONCURRENT_TRANSCRIPTIONS", "2"))
_TRANSCRIPTION_LEASE_TTL_S = int(os.environ.get("TRANSCRIPTION_LEASE_TTL_S", "300"))
_TRANSCRIPTION_COUNTER_KEY = "transcriptions:in_flight"


def _try_acquire_transcription_slot() -> str | None:
    """Reserve a Whisper slot in Redis. Same pattern as _try_acquire_upload_slot.

    Returns a lease id on success, None when Redis is unavailable (dev/test)
    or when OpenAI's Whisper API is configured (no local memory used, no need
    to gate concurrency). Raises 503 only on the local-Whisper code path.
    """
    # OpenAI's Whisper API handles concurrency for us — each transcription
    # is a remote HTTP call, not a local model load. No reason to cap.
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return None
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        import uuid as _uuid
        from redis import Redis
        client = Redis.from_url(redis_url, socket_timeout=2)
        lease = _uuid.uuid4().hex[:12]
        pipe = client.pipeline()
        pipe.sadd(_TRANSCRIPTION_COUNTER_KEY, lease)
        pipe.scard(_TRANSCRIPTION_COUNTER_KEY)
        pipe.expire(_TRANSCRIPTION_COUNTER_KEY, _TRANSCRIPTION_LEASE_TTL_S)
        _, count, _ = pipe.execute()
    except Exception as e:  # pragma: no cover
        logger.debug("transcription concurrency: Redis unavailable (%s)", e)
        return None
    if count > _MAX_CONCURRENT_TRANSCRIPTIONS:
        try:
            client.srem(_TRANSCRIPTION_COUNTER_KEY, lease)
        except Exception:
            pass
        logger.warning(
            "/transcribe-uploaded refused: %d concurrent transcriptions in flight (cap %d)",
            count, _MAX_CONCURRENT_TRANSCRIPTIONS,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Transcripción temporalmente saturada. Reintentá en unos segundos."
            ),
            headers={"Retry-After": "30"},
        )
    return lease


def _release_transcription_slot(lease_id: str | None) -> None:
    """Release a previously-acquired transcription slot. Best-effort."""
    if not lease_id:
        return
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return
    try:
        from redis import Redis
        client = Redis.from_url(redis_url, socket_timeout=2)
        client.srem(_TRANSCRIPTION_COUNTER_KEY, lease_id)
    except Exception:  # pragma: no cover
        pass


def _try_acquire_upload_slot() -> str | None:
    """Reserve an upload slot in Redis. Returns a lease id (string) on
    success, None when Redis isn't reachable (no enforcement), and
    raises 503 when the cap is reached.

    Slot release happens via `_release_upload_slot(lease_id)` after the
    request finishes. The lease auto-expires via TTL so a crashed
    request doesn't leak slots forever.
    """
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        import uuid as _uuid
        from redis import Redis
        client = Redis.from_url(redis_url, socket_timeout=2)
        # SADD + SCARD is atomic enough — within a Redis instance
        # commands are serialized. The lease set holds active lease
        # ids; we expire the SET so a wedged client can't hold slots
        # forever (orphans are reaped on the next pass).
        lease = _uuid.uuid4().hex[:12]
        pipe = client.pipeline()
        pipe.sadd(_UPLOAD_COUNTER_KEY, lease)
        pipe.scard(_UPLOAD_COUNTER_KEY)
        pipe.expire(_UPLOAD_COUNTER_KEY, _UPLOAD_LEASE_TTL_S)
        _, count, _ = pipe.execute()
    except Exception as e:  # pragma: no cover
        logger.debug("upload concurrency: Redis unavailable (%s)", e)
        return None
    if count > _MAX_CONCURRENT_UPLOADS:
        try:
            client.srem(_UPLOAD_COUNTER_KEY, lease)
        except Exception:
            pass
        logger.warning(
            "/upload refused: %d concurrent uploads in flight (cap %d)",
            count, _MAX_CONCURRENT_UPLOADS,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Estamos saturados con otros uploads. Reintentamos en unos "
                "segundos."
            ),
            headers={"Retry-After": "30"},
        )
    return lease


def _release_upload_slot(lease_id: str | None) -> None:
    """Release a previously-acquired upload slot. Best-effort."""
    if not lease_id:
        return
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return
    try:
        from redis import Redis
        client = Redis.from_url(redis_url, socket_timeout=2)
        client.srem(_UPLOAD_COUNTER_KEY, lease_id)
    except Exception:  # pragma: no cover
        pass


def _parse_umg_params(
    delivery_profile: str,
    umg_frame_size: str,
    umg_fps: str,
    umg_prores_profile: str,
    current_user: dict | None = None,
) -> dict | None:
    """Parse and validate UMG delivery params. Returns umg_spec dict or None.

    `current_user` is checked against `has_prores_access` for any non-
    YouTube profile — broadcast deliverables are gated to allow-listed
    tenants (PRORES_TENANTS env) plus admins. We refuse with 403 here
    rather than letting the request go through and silently rendering a
    YouTube MP4, because the operator's intent ("UMG master") and what
    we'd produce would diverge — a confusing failure mode.
    """
    if delivery_profile not in ("youtube", "umg", "both"):
        raise HTTPException(
            status_code=400,
            detail="delivery_profile must be one of: youtube, umg, both",
        )
    if delivery_profile == "youtube":
        return None
    if current_user is not None and not has_prores_access(current_user):
        raise HTTPException(
            status_code=403,
            detail="Broadcast (ProRes) delivery is not enabled for your account. "
                   "Contact support if you need this feature.",
        )
    if not (umg_frame_size and umg_fps and umg_prores_profile):
        raise HTTPException(
            status_code=400,
            detail="umg_frame_size, umg_fps and umg_prores_profile are required "
                   "when delivery_profile is umg or both",
        )
    try:
        fps_val = float(umg_fps)
        profile_val = int(umg_prores_profile)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="umg_fps must be a number and umg_prores_profile an integer",
        )
    errors = validate_umg_config(umg_frame_size, fps_val, profile_val)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "frame_size": umg_frame_size,
        "fps": fps_val,
        "prores_profile": profile_val,
    }


# ---------------------------------------------------------------------------
# Direct-to-R2 upload (presigned PUT) — primary flow as of PR #24.
#
# The browser PUTs the audio body straight to R2 using a presigned URL the
# API generates here. The API container never sees the bytes, which:
#
#   1. Decouples the API memory footprint from upload size — the lossless
#      WAV OOM path that motivated PR #23 disappears entirely.
#   2. Frees uvicorn workers from holding the connection open for the
#      slow upload (a 50 MB WAV at 1 MB/s used to tie up a worker for 50
#      seconds; now /upload-url returns in ~10 ms).
#   3. Lets us add R2 multipart uploads for resumability / parallelism
#      without further backend churn.
#
# Multipart kicks in for files above _MULTIPART_THRESHOLD_BYTES (16 MB by
# default) — under that, single-PUT is simpler and fast enough.
#
# The legacy multipart-form endpoints (/upload, /transcribe with file body)
# stay around as deprecated fallbacks for direct API callers; the frontend
# uses the presigned flow exclusively.
# ---------------------------------------------------------------------------

# Threshold above which the frontend should switch to multipart upload.
# Single-PUT is simpler but a connection drop wastes the entire transfer;
# multipart lets us retry just the failed part.
_MULTIPART_THRESHOLD_BYTES = int(
    os.environ.get("MULTIPART_THRESHOLD_BYTES", str(16 * 1024 * 1024))
)
# Max size of a single multipart part. S3/R2 require parts >= 5 MiB
# (except the last). 4 MiB violated that and made every multipart-
# complete fail with EntityTooSmall — confirmed in prod 2026-05-14 16:30
# right after the part-size-down change went live. Back to 8 MiB, the
# pre-incident default, which gives headroom over the 5 MiB floor and
# keeps part counts low for browser parallelism.
#
# Cloudflare proxy timeout (originally why we tried 4 MiB) is being
# addressed separately by restoring direct browser → R2 PUT (no proxy
# in the data path); see PR for r2_cors + frontend rollback.
_MULTIPART_PART_SIZE_BYTES = int(
    os.environ.get("MULTIPART_PART_SIZE_BYTES", str(8 * 1024 * 1024))
)
_PRESIGN_PUT_TTL_S = int(os.environ.get("PRESIGN_PUT_TTL_S", "900"))
# TTL de las URLs de PARTES multipart. Más largo que el single-PUT a
# propósito: el init batch-presigna las ~19 partes de un archivo de
# 150 MB de una sola vez, y en un uplink lento la subida completa puede
# tardar 30-50 min — con 900 s las últimas partes expiraban a mitad de
# camino (403) justo dentro de la ventana en que el reaper todavía no
# liberaba el job. 1 h cubre el peor caso realista; el riesgo extra es
# mínimo (la URL firma un solo part_number de un upload_id específico).
_PRESIGN_PART_TTL_S = int(os.environ.get("PRESIGN_PART_TTL_S", "3600"))


class _UploadUrlReq(BaseModel):
    filename: str = Field(..., max_length=500)       # DB Job.filename = VARCHAR(500)
    content_type: str = Field(default="", max_length=200)
    size_bytes: int = Field(default=0, ge=0)
    artist: str = Field(default="", max_length=255)  # DB Job.artist = VARCHAR(255)
    title: str = Field(default="", max_length=500)   # DB Job.song_title = VARCHAR(500)


def _validate_audio_filename_only(filename: str) -> None:
    """Cheap pre-flight check: just the extension. The full magic-bytes
    check happens after the bytes land on R2 / disk via the existing
    `_validate_audio_file_on_disk`."""
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    if not filename.lower().endswith(_AUDIO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="Only MP3 and WAV files are accepted.",
        )


def _safe_basename(filename: str) -> str:
    """Strip any directory components from a user-supplied filename and
    reject obvious traversal attempts. Returns the bare filename safe
    for `os.path.join(job_dir, ...)`.

    SECURITY (incident class): the original code did
    `os.path.join(job_dir, file.filename)` directly on multipart upload
    filenames. A request with `filename="../poc.mp3"` would write
    outside the job dir (and could overwrite a sibling job's
    `lyric_video.mp4` if the attacker guessed the job_id). This helper
    fixes the four call sites in one place.

    Defense in depth: also rejects null bytes and control chars (which
    some shells / databases handle inconsistently), and caps length at
    255 chars (POSIX NAME_MAX).
    """
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    # Strip directory parts (handles both / and \, plus repeated separators).
    base = os.path.basename(filename.replace("\\", "/"))
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    # Reject null bytes + control chars — they survive os.path.basename
    # but cause downstream surprises (NUL truncates strings in C libs).
    if "\x00" in base or any(ord(c) < 32 for c in base):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if len(base) > 255:
        raise HTTPException(status_code=400, detail="Filename too long.")
    return base


@app.post("/upload-url")
@limiter.limit("120/minute")
async def upload_url(
    request: Request,
    body: _UploadUrlReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a presigned PUT URL for a single-shot direct-to-R2 upload.

    Returns:
        {
          "job_id": "...",
          "upload_url": "https://...",
          "key": "inputs/<tenant>/<job>/<filename>",
          "expires_in": 900,
          "use_multipart": false,
          "part_size": 8388608,    # only meaningful when use_multipart=true
        }

    When `size_bytes` indicates a body above _MULTIPART_THRESHOLD_BYTES,
    use_multipart is True and `upload_url` is null — the browser must
    fall through to /upload-multipart-init for the per-part presigning
    machinery.
    """
    _validate_audio_filename_only(body.filename)
    # SECURITY: sanitize the user-supplied filename BEFORE it propagates
    # to `Job.filename` (then to `OUTPUTS_DIR/<job_id>/<filename>` on
    # /transcribe-uploaded). A traversal like `../poc.mp3` would
    # otherwise escape the job dir at write time.
    body.filename = _safe_basename(body.filename)
    if body.size_bytes and body.size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        # Log the rejection with the real size — a client-side cap bug
        # (silent 107 MB WAV reject, 2026-07-02) took hours to diagnose
        # because nothing anywhere recorded the attempted size.
        logger.warning(
            "[UPLOAD] 413 reject: %s (%.1f MB > %d MB) tenant=%s user=%s",
            body.filename, body.size_bytes / 1048576, MAX_UPLOAD_MB,
            current_user["tenant_id"], current_user["id"],
        )
        raise HTTPException(
            status_code=413,
            detail=f"File too large (>{MAX_UPLOAD_MB} MB).",
        )

    # Disk gate is informational here: the bytes never touch local disk
    # during upload. We still enforce it so a downstream /transcribe-
    # uploaded run won't ENOSPC on Whisper temp files. Memory pressure
    # gate is intentionally NOT applied — a presigned URL costs ~0 bytes
    # of API memory.
    _enforce_plan_quota(db, current_user)
    _enforce_daily_volume_cap(db, current_user)
    _enforce_tenant_backlog(db, current_user)
    _enforce_disk_capacity()

    if not storage.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Direct-to-R2 uploads require object storage. Configure R2_* env vars.",
        )

    artist_form = (body.artist or "").strip()
    title_form = (body.title or "").strip()
    parsed_artist, parsed_title = _parse_filename_artist_title(
        body.filename, db=db, tenant_id=current_user.get("tenant_id", "")
    )
    job_artist = artist_form or parsed_artist or "Unknown"
    job_song_title = title_form or parsed_title

    job_id = create_job(
        db,
        artist=job_artist,
        style="oscuro",                # set for real on /generate
        filename=body.filename,
        user_id=current_user["id"],
        tenant_id=current_user["tenant_id"],
        delivery_profile="youtube",    # set for real on /generate
        initial_status="awaiting_upload",
        song_title=job_song_title,
    )

    # 2026-05-28 dedup gap (audit on Don Electrón_Intoxicados duplicate):
    # PR #388 closed the /generate direct-create path but the modern flow
    # is /upload-url → R2 → /transcribe-uploaded, and /upload-url had no
    # supersede. A double-fire (browser retry, dropzone double-event,
    # operator re-drop without UI feedback) lands two awaiting_upload
    # rows; each follows its own pipeline and the operator ends up with
    # two transcribed_pending duplicates in admin. Mirror the dedup
    # pattern from /generate (main.py ~5688): filename is already
    # `_safe_basename`-canonicalized at line 2387.
    try:
        from jobs import supersede_sibling_drafts
        supersede_sibling_drafts(
            db, keep_job_id=job_id, user_id=current_user["id"],
            tenant_id=current_user["tenant_id"], filename=body.filename,
        )
    except Exception as e:
        logger.warning("[DEDUP] supersede sibling drafts failed: %s", e)

    use_multipart = (
        body.size_bytes > 0 and body.size_bytes >= _MULTIPART_THRESHOLD_BYTES
    )
    logger.info(
        "[UPLOAD] ticket minted: job=%s file=%s size=%.1f MB multipart=%s tenant=%s",
        job_id, body.filename, (body.size_bytes or 0) / 1048576,
        use_multipart, current_user["tenant_id"],
    )
    response = {
        "job_id": job_id,
        "key": _input_object_key_for_job(
            current_user["tenant_id"], job_id, body.filename,
        ),
        "expires_in": _PRESIGN_PUT_TTL_S,
        "use_multipart": use_multipart,
        "part_size": _MULTIPART_PART_SIZE_BYTES,
        "upload_url": None,
    }
    if not use_multipart:
        signed = storage.presign_put_url(
            current_user["tenant_id"], job_id, body.filename,
            content_type=body.content_type or None,
            expiry_seconds=_PRESIGN_PUT_TTL_S,
        )
        if not signed:
            raise HTTPException(
                status_code=503,
                detail="Could not sign upload URL.",
            )
        response["upload_url"] = signed["url"]
        response["key"] = signed["key"]
        # Persist the key now so /transcribe-uploaded can find it without
        # re-deriving (which would reject if the filename gets sanitized
        # differently between calls).
        from jobs import get_job_model
        job_row = get_job_model(db, job_id)
        if job_row:
            job_row.input_r2_key = signed["key"]
            db.commit()
    return response


def _input_object_key_for_job(tenant_id: str, job_id: str, filename: str) -> str:
    """Public-facing wrapper around storage._input_object_key — the
    underscore prefix on the storage helper signals intent (private),
    but the API surface needs the same key."""
    return storage._input_object_key(tenant_id, job_id, filename)


class _MultipartInitReq(BaseModel):
    job_id: str = Field(..., max_length=12)            # DB Job.job_id = VARCHAR(12)
    filename: str = Field(..., max_length=500)
    content_type: str = Field(default="", max_length=200)
    # 2026-05-25 velocity sprint: si el cliente conoce upfront cuántos
    # chunks va a subir (ceil(file_size / part_size)), lo manda acá y
    # el server presigna TODOS los chunks en una sola respuesta. Ahorra
    # 1 round-trip por chunk (~80-120 ms RTT × N chunks). Backwards
    # compat: default 0 = comportamiento previo (cliente sigue llamando
    # /upload-multipart-part-url por chunk).
    expected_parts: int = Field(default=0, ge=0, le=10_000)


@app.post("/upload-multipart-init")
@limiter.limit("60/minute")
async def upload_multipart_init(
    request: Request,
    body: _MultipartInitReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Begin a multipart upload for a job that was created via /upload-url
    with use_multipart=true. Returns the upload_id and key the browser
    needs to start signing parts.

    Si el body trae `expected_parts > 0`, el response incluye también
    `presigned_parts: [{part_number, url}]` con todos los chunks
    pre-firmados — el cliente puede saltarse las llamadas individuales
    a /upload-multipart-part-url. Backwards-compatible: sin
    expected_parts el response es idéntico al previo.
    """
    _validate_audio_filename_only(body.filename)
    from jobs import get_job_model
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row.status != "awaiting_upload":
        raise HTTPException(
            status_code=409,
            detail=f"Job is in state {job_row.status!r}, not awaiting_upload.",
        )
    if job_row.multipart_upload_id:
        # Idempotent: return the existing upload_id so a flaky frontend
        # retry doesn't create two parallel multipart uploads (which would
        # leave one orphaned in R2 storage).
        resp = {
            "upload_id": job_row.multipart_upload_id,
            "key": job_row.input_r2_key,
            "part_size": _MULTIPART_PART_SIZE_BYTES,
            "presign_ttl_s": _PRESIGN_PART_TTL_S,
        }
        if body.expected_parts > 0:
            resp["presigned_parts"] = _batch_presign_parts(
                job_row.input_r2_key,
                job_row.multipart_upload_id,
                body.expected_parts,
            )
        return resp
    if not storage.is_enabled():
        raise HTTPException(status_code=503, detail="Object storage not configured.")
    init = storage.multipart_init(
        current_user["tenant_id"], body.job_id, body.filename,
        content_type=body.content_type or None,
    )
    if not init:
        # Most common cause: R2 credentials missing/wrong, or R2 bucket
        # config (CORS, ACL) rejecting create_multipart_upload. The
        # full traceback is in the API container logs (see storage.py).
        raise HTTPException(
            status_code=503,
            detail=(
                "No pudimos iniciar la subida del archivo grande. "
                "Revisá la conexión y reintentá; si persiste, contactá soporte."
            ),
        )
    job_row.input_r2_key = init["key"]
    job_row.multipart_upload_id = init["upload_id"]
    db.commit()
    resp = {
        "upload_id": init["upload_id"],
        "key": init["key"],
        "part_size": _MULTIPART_PART_SIZE_BYTES,
        "presign_ttl_s": _PRESIGN_PART_TTL_S,
    }
    if body.expected_parts > 0:
        resp["presigned_parts"] = _batch_presign_parts(
            init["key"], init["upload_id"], body.expected_parts,
        )
    return resp


def _batch_presign_parts(key: str, upload_id: str, expected_parts: int) -> list:
    """Presign N parts in a single batch. Each entry is
    `{"part_number": int, "url": str}`. Skipped on per-part failure
    (returns the entries that succeeded). The frontend falls back to
    /upload-multipart-part-url for any missing entries.

    Errores individuales no se logean LOUD para no spammear — el path
    de fallback per-part les sirve al cliente.
    """
    out = []
    for part_number in range(1, expected_parts + 1):
        try:
            url = storage.multipart_presign_part(
                key, upload_id, part_number, expiry_seconds=_PRESIGN_PART_TTL_S,
            )
            if url:
                out.append({"part_number": part_number, "url": url})
        except Exception:
            pass
    return out


class _MultipartPartReq(BaseModel):
    job_id: str = Field(..., max_length=12)            # DB Job.job_id = VARCHAR(12)
    part_number: int


@app.post("/upload-multipart-part-url")
@limiter.limit("600/minute")
async def upload_multipart_part_url(
    request: Request,
    body: _MultipartPartReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sign one part of a multipart upload. The frontend calls this
    once per part; PUTs the bytes against the returned URL; reads the
    `ETag` response header; submits {part_number, etag} back via
    /upload-multipart-complete."""
    if body.part_number < 1 or body.part_number > 10_000:
        raise HTTPException(status_code=400, detail="part_number out of range")
    from jobs import get_job_model
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row.status != "awaiting_upload" or not job_row.multipart_upload_id:
        raise HTTPException(
            status_code=409,
            detail="Job is not in an active multipart upload.",
        )
    url = storage.multipart_presign_part(
        job_row.input_r2_key, job_row.multipart_upload_id,
        body.part_number, expiry_seconds=_PRESIGN_PART_TTL_S,
    )
    if not url:
        raise HTTPException(status_code=503, detail="Could not sign part URL.")
    return {"url": url, "expires_in": _PRESIGN_PART_TTL_S}


async def _drain_to_spooled(request: Request, max_bytes: int):
    """Stream the request body into a SpooledTemporaryFile.

    Async-generator driven (`async for chunk in request.stream()`) so
    the worker yields to the event loop between chunks. Crucial for
    concurrent uploads from slow upstreams: previously `await
    request.body()` blocked the worker for the full upstream duration
    (~80 s per 4 MB part on a residential connection). With 5 audios
    × 4 parts in flight, all 20 workers were pinned waiting for bytes,
    starving every other endpoint and tanking effective throughput.

    Memory profile: SpooledTemporaryFile keeps the body in RAM up to
    `max_size` (8 MiB) and spills to disk only above that. For our
    4 MB part size the body stays in RAM — same footprint as before,
    but worker time is freed.

    Returns a rewound file object ready for boto3 to read. Raises
    HTTPException(413) if `max_bytes` is exceeded mid-stream.
    """
    from tempfile import SpooledTemporaryFile
    f = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            f.close()
            raise HTTPException(status_code=413, detail="Body exceeds size limit.")
        f.write(chunk)
    if total == 0:
        f.close()
        raise HTTPException(status_code=400, detail="Empty body.")
    f.seek(0)
    return f


@app.post("/upload-part-proxy")
@limiter.limit("600/minute")
async def upload_part_proxy(
    request: Request,
    job_id: str,
    part_number: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Proxy a multipart chunk to R2 server-side. The browser POSTs raw
    bytes here (same-origin, no CORS preflight) instead of PUTting directly
    to r2.cloudflarestorage.com which would require R2 bucket CORS config.

    Body is read via async streaming into a spooled temp file (see
    _drain_to_spooled). boto3 then runs in a thread executor so the
    event loop stays free for other concurrent requests."""
    if part_number < 1 or part_number > 10_000:
        raise HTTPException(status_code=400, detail="part_number out of range")
    # Resilient lookup: the global DbTransientRetryMiddleware can't
    # replay this request (body > 1 MiB), so we handle SSL drops inline
    # before reading the body. See jobs.get_job_model_resilient for the
    # production incident this addresses.
    from jobs import get_job_model_resilient
    job_row = get_job_model_resilient(db, job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row.status != "awaiting_upload" or not job_row.multipart_upload_id:
        raise HTTPException(
            status_code=409, detail="Job is not in an active multipart upload."
        )
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > _MULTIPART_PART_SIZE_BYTES + 1024:
        raise HTTPException(status_code=413, detail="Chunk exceeds part size limit.")

    key = job_row.input_r2_key
    upload_id = job_row.multipart_upload_id
    body = await _drain_to_spooled(request, _MULTIPART_PART_SIZE_BYTES + 1024)
    try:
        loop = asyncio.get_event_loop()
        etag = await loop.run_in_executor(
            None, storage.upload_part, key, upload_id, part_number, body,
        )
    finally:
        body.close()
    if etag is None:
        raise HTTPException(status_code=502, detail="R2 part upload failed.")
    return {"etag": etag}


@app.post("/upload-file-proxy")
@limiter.limit("120/minute")
async def upload_file_proxy(
    request: Request,
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Proxy a single-PUT file to R2 server-side. The browser POSTs raw
    bytes here (same-origin, no CORS preflight) instead of PUTting directly
    to r2.cloudflarestorage.com which would require R2 bucket CORS config.
    Mirrors /upload-part-proxy for the non-multipart (<16 MB) path."""
    # Resilient lookup — see /upload-part-proxy for why the global
    # middleware can't help (body too large to buffer for replay).
    from jobs import get_job_model_resilient
    job_row = get_job_model_resilient(db, job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row.status != "awaiting_upload":
        raise HTTPException(
            status_code=409, detail="Job is not awaiting upload."
        )
    if not job_row.input_r2_key:
        raise HTTPException(
            status_code=409, detail="Job has no R2 key allocated."
        )
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size.")

    content_type = request.headers.get("content-type") or "application/octet-stream"
    key = job_row.input_r2_key
    body = await _drain_to_spooled(request, MAX_UPLOAD_MB * 1024 * 1024)
    try:
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None, storage.put_object_bytes, key, body, content_type,
        )
    finally:
        body.close()
    if not ok:
        raise HTTPException(status_code=502, detail="R2 upload failed.")
    return {"job_id": job_id, "key": key}


class _MultipartCompleteReq(BaseModel):
    job_id: str = Field(..., max_length=12)            # DB Job.job_id = VARCHAR(12)
    parts: list = Field(..., max_length=10000)  # R2 max 10k parts per upload


@app.post("/upload-multipart-complete")
@limiter.limit("60/minute")
async def upload_multipart_complete(
    request: Request,
    body: _MultipartCompleteReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Finalize a multipart upload. Once R2 stitches the parts, the job
    stays in awaiting_upload until /transcribe-uploaded promotes it."""
    from jobs import get_job_model
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row.status != "awaiting_upload" or not job_row.multipart_upload_id:
        raise HTTPException(
            status_code=409,
            detail="Job is not in an active multipart upload.",
        )
    parts_payload = []
    for p in body.parts:
        try:
            part_no = int(p.get("part_number"))
            etag = str(p.get("etag") or "").strip().strip('"')
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid part format.")
        if not etag:
            raise HTTPException(status_code=400, detail="Part etag missing.")
        parts_payload.append({"PartNumber": part_no, "ETag": f'"{etag}"'})
    try:
        completed_key = storage.multipart_complete(
            job_row.input_r2_key, job_row.multipart_upload_id, parts_payload,
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"R2 multipart_complete failed: {e}",
        )
    if not completed_key:
        # storage.multipart_complete swallowed a boto error and returned
        # None. Two very different situations hide behind that None and
        # they must NOT be handled the same way — a HEAD on the target
        # key is the ground truth that tells them apart:
        #
        #   1. The object IS there. CompleteMultipartUpload is
        #      destructive: the first call stitches the object and
        #      consumes the upload_id, so a second, concurrent complete
        #      (double-submit, or a client retry whose first response was
        #      lost) gets NoSuchUpload even though the upload durably
        #      succeeded. Answer 200 — the bytes are safe. (Prod
        #      2026-07-06: NoSuchUpload on a UMG .wav whose 6 parts had
        #      all landed.)
        #
        #   2. No object landed. The upload_id is gone for good (R2-side
        #      abort / stale-multipart sweep) — NoSuchUpload here is
        #      PERMANENT, retrying /upload-multipart-complete can never
        #      succeed. Drop the dead upload_id so the row doesn't wedge
        #      in awaiting_upload and tell the client to re-upload.
        recovered_size = storage.head_object_size(job_row.input_r2_key)
        if recovered_size is None:
            logger.warning(
                "[UPLOAD] multipart_complete failed and no object present "
                "— dead upload_id, asking client to re-upload: job=%s key=%s",
                body.job_id, job_row.input_r2_key,
            )
            job_row.multipart_upload_id = None
            db.commit()
            raise HTTPException(
                status_code=409,
                detail="La subida expiró en R2. Volvé a subir el archivo.",
            )
        logger.info(
            "[UPLOAD] multipart_complete returned no key but object is "
            "present — idempotent recovery: job=%s key=%s size=%d",
            body.job_id, job_row.input_r2_key, recovered_size,
        )
        # Object is durable → fall through to the shared size-gate +
        # upload_id clear + 200 below, exactly as a first-time success.
        completed_key = job_row.input_r2_key
    # Server-side size gate. The 413 in /upload-url trusts the CLIENT-
    # declared size_bytes and the presigned part URLs don't constrain
    # the body, so this HEAD is the first moment we can check what
    # actually landed. Small tolerance: multipart overhead is zero, but
    # don't 413 a legitimate file over a rounding artifact.
    real_size = storage.head_object_size(job_row.input_r2_key)
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if real_size is not None and real_size > max_bytes:
        logger.warning(
            "[UPLOAD] 413 post-complete: job=%s key=%s real=%.1f MB > %d MB "
            "tenant=%s user=%s",
            body.job_id, job_row.input_r2_key, real_size / 1048576,
            MAX_UPLOAD_MB, current_user["tenant_id"], current_user["id"],
        )
        storage.delete_object(job_row.input_r2_key)
        job_row.multipart_upload_id = None
        db.commit()
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({real_size / 1048576:.1f} MB). "
                   f"Max allowed: {MAX_UPLOAD_MB} MB.",
        )
    # Clear the upload_id so the row is recognisably "complete" but
    # input_r2_key + status still need /transcribe-uploaded.
    job_row.multipart_upload_id = None
    db.commit()
    return {"job_id": body.job_id, "key": job_row.input_r2_key}


class _MultipartAbortReq(BaseModel):
    job_id: str = Field(..., max_length=12)            # DB Job.job_id = VARCHAR(12)


@app.post("/upload-multipart-abort")
@limiter.limit("30/minute")
async def upload_multipart_abort(
    request: Request,
    body: _MultipartAbortReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User cancelled the upload. Tell R2 to garbage-collect the parts
    and drop the Job row. Idempotent — calling twice is fine."""
    from jobs import get_job_model
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        return {"ok": True}  # idempotent
    if job_row.multipart_upload_id and job_row.input_r2_key:
        storage.multipart_abort(job_row.input_r2_key, job_row.multipart_upload_id)
    if job_row.status == "awaiting_upload":
        db.delete(job_row)
        db.commit()
    return {"ok": True}


class _TranscribeUploadedReq(BaseModel):
    job_id: str = Field(..., max_length=12)
    language: str = Field(default="", max_length=16)
    # Toggle del operador: "es una versión en vivo" — arma la auditoría de
    # sufijo aunque el título no tenga marcador live (06/07).
    live: bool = False
    artist: str = Field(default="", max_length=255)    # DB Job.artist = VARCHAR(255)
    title: str = Field(default="", max_length=500)     # DB Job.song_title = VARCHAR(500)


@app.post("/transcribe-uploaded")
@limiter.limit("60/minute")
async def transcribe_uploaded(
    request: Request,
    body: _TranscribeUploadedReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Promote an awaiting_upload job to transcribed_pending, downloading
    the audio from R2 to local disk for Whisper / lrclib lookup.

    Returns the same shape as the legacy /transcribe (segments,
    reference_lyrics, plus job_id) so the frontend's editor flow
    plugs in unchanged.
    """
    from jobs import get_job_model
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    # `transcription_failed` added 2026-06-09: honours the reaper's customer-
    # facing "apretá Reintentar para volver a transcribir" promise
    # (reaper.py:_reason_for_transcription). The audio still lives in R2
    # (input_r2_key set; the transcription reap never clears it), so this
    # re-runs the transcribe step from storage with no re-upload — the exact
    # CTA the message offers. Before this, /retry rejected the state and the
    # only path was re-uploading through the wizard (P0 follow-up: the message
    # over-promised a one-click retry the API refused).
    if job_row.status not in ("awaiting_upload", "transcribed_pending",
                              "transcription_failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is in state {job_row.status!r}, cannot transcribe.",
        )
    if job_row.multipart_upload_id:
        raise HTTPException(
            status_code=409,
            detail="Multipart upload not completed yet.",
        )
    if not job_row.input_r2_key:
        raise HTTPException(
            status_code=409,
            detail="Job has no associated upload.",
        )

    # SNAPSHOT + release (incidente agus77 06/07): la descarga de R2 de
    # abajo tarda decenas de segundos con un WAV de 45-150 MB, y este
    # handler mantenía la sesión de DB checked-out EN TRANSACCIÓN todo ese
    # tiempo. Postgres la mataba por idle-in-transaction timeout
    # ("Transient DB error on POST /transcribe-uploaded"), el pool se
    # agotaba, TODA la API pasaba a latencias de 20-40 s y el edge cortaba
    # los demás requests con 502 sin CORS → el wizard mostraba "Sin
    # respuesta del servidor". Capturamos lo que necesitamos del row y
    # soltamos la conexión ANTES del I/O largo; los flips de status de más
    # abajo abren su propia sesión corta.
    _r2_key = job_row.input_r2_key
    _row_filename = job_row.filename
    _row_artist = job_row.artist or ""
    _row_title = job_row.song_title or ""
    db.close()

    _enforce_disk_capacity()
    _enforce_memory_pressure()

    # Feature flag — 2026-05-23. Default ON. Flipear a "0" para rollback al
    # path sync inline si rompe en staging. Rollout plan:
    #   1. Merge a staging con ASYNC_TRANSCRIBE_ENABLED=1 en .env staging.
    #   2. Smoke 1-2 días. Si todo OK, prender en prod (.env prod) + monitorear.
    #   3. Borrar la rama legacy + el flag después de 1 semana sin incidentes.
    _async_enabled = os.environ.get(
        "ASYNC_TRANSCRIBE_ENABLED", "1"
    ).strip().lower() not in ("0", "false", "no", "off")

    # Materialize the audio onto local disk for Whisper / ffmpeg / etc.
    # En el path async, esto sigue siendo necesario porque el handler valida
    # el archivo (corrupt detection) antes de enqueue — fail-fast en el
    # request, no en el worker (que daría error opaco al usuario via polling).
    job_id = body.job_id
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    audio_path = os.path.join(job_dir, _row_filename)

    if not os.path.exists(audio_path):
        import asyncio as _asyncio
        # Size gate against the REAL stored object before pulling it to
        # local disk. The single-PUT path never passes through
        # /upload-multipart-complete, so a client that under-declared
        # size_bytes in /upload-url could otherwise land an arbitrarily
        # large object and have us download it whole (disk + Whisper).
        _real_size = storage.head_object_size(_r2_key)
        if _real_size is not None and _real_size > MAX_UPLOAD_MB * 1024 * 1024:
            logger.warning(
                "[UPLOAD] 413 at transcribe: job=%s key=%s real=%.1f MB > %d MB "
                "tenant=%s user=%s",
                body.job_id, _r2_key, _real_size / 1048576,
                MAX_UPLOAD_MB, current_user["tenant_id"], current_user["id"],
            )
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({_real_size / 1048576:.1f} MB). "
                       f"Max allowed: {MAX_UPLOAD_MB} MB.",
            )
        _loop = _asyncio.get_event_loop()
        for _attempt in range(5):
            # boto3 es síncrono: correrlo inline dentro de este handler
            # async bloqueaba el event loop de uvicorn el tiempo entero
            # de la descarga (segundos con un WAV de 150 MB) y un batch
            # de 5 serializaba TODA la API. Mismo patrón executor que
            # /upload-part-proxy.
            _ok = await _loop.run_in_executor(
                None, storage.download_object, _r2_key, audio_path,
            )
            if _ok:
                break
            if _attempt < 4:
                await _asyncio.sleep(0.5 * (2 ** _attempt))
        else:
            raise HTTPException(
                status_code=502,
                detail="No pudimos leer el archivo subido. Reintentá en unos segundos.",
            )
    _validate_audio_file_on_disk(_row_filename, audio_path)

    if _async_enabled:
        # Async path — enqueue + 202 + status polling.
        # Flippeamos status a "transcribing_queued" para que /transcription-status
        # devuelva un estado coherente desde el momento del enqueue.
        from database import SessionLocal as _SL
        _db2 = _SL()
        try:
            _row2 = get_job_model(_db2, job_id)
            if _row2 is None:
                raise HTTPException(status_code=404, detail="Job not found.")
            _row2.status = "transcribing_queued"
            _row2.current_step = "transcribing"
            # Reset the reaper clock. find_stuck_transcriptions anchors on
            # coalesce(last_progress_at, created_at) with a 120-min threshold
            # (reaper.py). A retried `transcription_failed` job has an OLD
            # created_at, so without this NOW() bump the very next reaper pass
            # would re-kill it instantly (same class of bug retry_job guards at
            # main.py:9107). Harmless for fresh awaiting_upload jobs.
            _row2.last_progress_at = datetime.now(timezone.utc)
            _db2.commit()
        finally:
            _db2.close()
        try:
            from queue_jobs import enqueue_transcription
            # HOTFIX 2026-05-27: use the Job row as the single source of
            # truth for artist/title. Previously we accepted body.artist /
            # body.title from the request, which let the frontend send
            # different metadata than what /upload-url committed to the
            # row — opening a path where two transcription jobs ended up
            # in queue with swapped metadata (agus.cafisi incident 16:42).
            # Any client-side correction must now go through PATCH
            # /jobs/{id} BEFORE calling /transcribe-uploaded.
            enqueue_transcription(
                job_id,
                audio_path,
                language=body.language,
                artist=_row_artist,
                title=_row_title,
                filename=_row_filename,
                tenant_id=current_user.get("tenant_id", ""),
                live=bool(body.live),
            )
        except Exception as exc:
            logger.exception("[TRANSCRIBE] enqueue failed for job=%s", job_id)
            # Rollback el status para no dejar el job colgado en queued.
            from jobs import update_job
            update_job(job_id, status="transcription_failed",
                       current_step="error", error=str(exc)[:300])
            raise HTTPException(status_code=503, detail="Cola de transcripción no disponible. Reintentá.")
        # 202 Accepted con el job_id para polling. No incluye segments —
        # el frontend pollea /transcription-status hasta status=transcribed.
        return {
            "job_id": job_id,
            "status": "transcribing_queued",
        }

    # Legacy sync path (fallback con ASYNC_TRANSCRIBE_ENABLED=0).
    transcription_lease = _try_acquire_transcription_slot()
    try:
        # Reuse the existing Whisper / lrclib machinery from the legacy
        # /transcribe handler. Keeping the implementation in one place via
        # the helper below means the lyrics-recovery / hallucination logic
        # stays in lockstep with the legacy fallback.
        # Sesión corta para el flip (la del request se soltó antes de la
        # descarga); BEFORE the ~15-20 s transcription so it doesn't starve
        # /usage and /jobs (dashboard freeze).
        from database import SessionLocal as _SL
        _db3 = _SL()
        try:
            _row3 = get_job_model(_db3, job_id)
            if _row3 is not None:
                _row3.status = "transcribed_pending"
                _row3.current_step = "editing"
                _db3.commit()
        finally:
            _db3.close()

        # HOTFIX 2026-05-27: same source-of-truth fix as the async path —
        # use job_row.artist / job_row.song_title (committed at /upload-url
        # time), NOT body.artist / body.title which can diverge from the
        # row and create ghost jobs.
        _result = await _run_transcription_for_job(
            request, current_user, job_id, audio_path,
            language=body.language,
            artist=_row_artist,
            title=_row_title,
        )
        _result = await _maybe_ctc_retime(_result, audio_path, job_id,
                                          _row_artist, _row_title)
        _result = await _maybe_adlib_filter(
            _result, audio_path, job_id,
            live_hint=bool(getattr(body, "live", False))
            or _looks_live(_row_title, _row_filename))
        from lyrics_format import format_lyrics_pass as _fmt
        return await _fmt(_result, language=body.language or "es")
    finally:
        _release_transcription_slot(transcription_lease)


# ---------------------------------------------------------------------------
# Transcription status polling — 2026-05-23
# El frontend pollea acá tras un POST /transcribe-uploaded (async). Devuelve
# el ciclo de vida del job: transcribing_queued → transcribing →
# transcribed | transcription_failed. Cuando llega a transcribed, incluye
# segments + reference_lyrics para que el editor cargue sin segundo request.
# ---------------------------------------------------------------------------
@app.get("/transcription-status/{job_id}")
@limiter.limit("600/minute")  # polling holgado: 1 req/seg × ~10 archivos simultáneos
def transcription_status(
    request: Request,
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Devuelve el estado actual de una transcripción async.

    Forma del response:
      {
        "job_id": str,
        "status": "transcribing_queued" | "transcribing" | "transcribed" | "transcription_failed",
        "segments": [...] | null,
        "reference_lyrics": str | null,
        "coverage_warning": bool,
        "recovery_source": str | null,
        "error": str | null
      }

    El frontend pollea con backoff (~1.5s → 5s). Cuando status == "transcribed"
    el polling para y carga el editor con los segments. Si "transcription_failed",
    muestra el error inline con botón Reintentar.
    """
    # Try-except amplio porque slowapi/middleware tiran 500 bare en lugar de
    # JSONResponse con detail si una excepción burbujea fuera de FastAPI.
    # Capturamos acá para devolver siempre JSON con stack trace summary.
    try:
        from jobs import get_job_model
        job_row = get_job_model(db, job_id)
        if (not job_row
                or job_row.user_id != current_user["id"]
                or job_row.tenant_id != current_user["tenant_id"]):
            raise HTTPException(status_code=404, detail="Job not found.")

        status = job_row.status or ""
        # Mapeo de status del DB a los valores que espera el frontend. El path
        # legacy sync usa "transcribed_pending" como estado final
        # post-transcribe; lo normalizamos a "transcribed" en la respuesta.
        if status == "transcribed_pending":
            status = "transcribed"

        payload = {
            "job_id": job_id,
            "status": status,
            "segments": None,
            "reference_lyrics": None,
            "coverage_warning": bool(getattr(job_row, "coverage_warning", False)),
            "recovery_source": getattr(job_row, "recovery_source", None),
            "error": None,
        }
        if status == "transcribed":
            payload["segments"] = job_row.segments_json or []
            # reference_lyrics no es columna del modelo Job (la transcripción
            # vieja la devolvía inline; ahora no la persistimos). Defer a
            # otro PR si el editor la necesita post-render. Default "" para
            # no romper el frontend que la lee.
            payload["reference_lyrics"] = getattr(job_row, "reference_lyrics", "") or ""
        elif status == "transcription_failed":
            payload["error"] = (getattr(job_row, "error", None) or
                                "Error desconocido durante la transcripción.")

        return payload
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[TRANSCRIBE-STATUS] job_id=%s 500: %s", job_id, exc)
        # Devolver JSON con detail en lugar de "Internal Server Error" plano
        # para que el frontend (y este smoke script) puedan diagnosticar.
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
        )


# ---------------------------------------------------------------------------
# Background pre-generation — Capa C del wizard refactor (2026-05-24)
# Mientras el operador edita lyrics, Veo/Imagen ya están generando el fondo.
# Cuando llega POST /generate, la pipeline reusa el cache. Ver bg_preview.py
# para el worker + cache key logic.
# ---------------------------------------------------------------------------
class _GeneratePreviewReq(BaseModel):
    """Background params — TODOS los campos que entran al hash determinístico
    del cache. Cualquier campo del request /generate que NO afecte el bg NO
    va acá (audio, lyrics, font, animation, transition...)."""
    artist: str = Field(default="", max_length=255)
    song_title: str = Field(default="", max_length=500)
    style: str = Field(default="auto", max_length=50)
    movement_style: str = Field(default="", max_length=64)
    effect: str = Field(default="", max_length=32)
    custom_colors: str = Field(default="", max_length=200)
    genre: str = Field(default="", max_length=64)
    concept: str = Field(default="", max_length=2000)
    background_hint: str = Field(default="", max_length=2000)
    bg_verbatim: bool = False
    background_mode: str = Field(default="veo", max_length=16)
    animate_image: bool = False
    match_lyrics: bool = True
    target_duration_s: float = Field(default=30.0, ge=5, le=600)


@app.post("/generate-preview")
@limiter.limit("30/minute")
async def generate_preview(
    request: Request,
    body: _GeneratePreviewReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pre-genera el background (Veo/Imagen) mientras el operador edita lyrics.

    Comportamiento:
      - Computa `bg_cache_key` = sha256-12 de los params normalizados.
      - Si `bg_cache/{key}.mp4` existe en R2 → 200 OK con `cached=true`. El
        operador ya pidió este background con esos params; reusamos.
      - Si no existe → crea job + enqueue a la queue `bg_preview` + retorna
        202 Accepted con `job_id` para que el frontend pollee status.
      - Idempotente: dos requests paralelos con mismos params → primero
        enqueue, segundo ve el cache_key + status existente y devuelve el
        mismo job_id. (Race window dentro del worker está cubierta por el
        idempotency check en run_bg_preview_job.)

    Plan-tier guard (Capa C 2026-05-24): free-tier devuelve `skipped:true`
    sin tocar la cola — cada preview descartado gasta $0.80-3.20 Veo y un
    trial toquetea opciones más que un paid customer. Paid pasan normal.
    """
    from auth import PLANS
    plan_id = (current_user.get("plan") or "free").strip()
    plan_cfg = PLANS.get(plan_id, PLANS["free"])
    if not plan_cfg.get("bg_preview_enabled", False):
        return {
            "skipped": True,
            "reason": "plan_tier",
            "message": "El pre-render del fondo está disponible en planes paid. El video se genera igual al apretar 'Crear video'.",
        }

    from bg_preview import (
        compute_bg_cache_key, cache_check, cache_r2_key, track_request,
    )

    params = body.model_dump()
    bg_cache_key = compute_bg_cache_key(params)

    # Fast path — cache hit.
    if cache_check(bg_cache_key):
        track_request(cache_hit=True)
        return {
            "bg_cache_key": bg_cache_key,
            "cached": True,
            "r2_key": cache_r2_key(bg_cache_key),
            "status": "bg_preview_done",
        }

    # Crear un job "ghost" en la DB sólo para tracking del status; tiene
    # tenant_id del current_user. Filename placeholder porque no hay audio.
    #
    # HOTFIX 2026-05-24: the previous call had THREE bugs that crashed the
    # endpoint with `TypeError: create_job() got an unexpected keyword
    # argument 'plan'`, blocking the WHOLE upload flow because the
    # frontend's `useBackgroundPreview` fires this in parallel with
    # /upload-url (which then 429'd as the user retried). The bugs were:
    #   (1) `plan=...` kwarg — doesn't exist on `create_job` (plan lives
    #       on the User row, not the Job).
    #   (2) `status=...` should be `initial_status=...` per signature.
    #   (3) The first positional arg `db` was missing.
    # Plus the value "bg_preview_queued" wasn't in `valid_states` — added
    # alongside this fix in jobs.py.
    from jobs import create_job
    job_id = create_job(
        db,
        artist=body.artist or "preview",
        song_title=body.song_title or "preview",
        style=body.style or "auto",
        filename=f"bgpreview_{bg_cache_key}.preview",
        user_id=current_user["id"],
        tenant_id=current_user["tenant_id"],
        delivery_profile="youtube",
        initial_status="bg_preview_queued",
    )

    try:
        from queue_jobs import enqueue_bg_preview
        enqueue_bg_preview(job_id, bg_cache_key, params)
    except Exception as exc:
        logger.exception("[BG_PREVIEW] enqueue failed for %s", job_id)
        from jobs import update_job
        update_job(job_id, status="bg_preview_failed", current_step="error",
                   error=str(exc)[:300])
        raise HTTPException(503, detail="Cola de pre-gen no disponible.")

    track_request(cache_hit=False)
    return {
        "bg_cache_key": bg_cache_key,
        "cached": False,
        "job_id": job_id,
        "status": "bg_preview_queued",
    }


@app.get("/admin/bg-preview-metrics")
async def admin_bg_preview_metrics(
    current_user: dict = Depends(get_current_user),
):
    """Métricas del feature bg-preview — admin only.

    Returns:
        {
          "requests_total": int,
          "cache_hits": int,
          "cache_misses": int,
          "cache_hit_rate": float (0..1),
          "estimated_wasted_cost_usd": float,
          "last_reset_ts": float
        }

    Process-local counters: en horizontal scale cada API replica reporta los
    suyos. Para una vista global, agregar manual (sumar requests/hits/misses
    de todas las réplicas; hit_rate = sum_hits / sum_requests).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(403, detail="Admin only.")
    from bg_preview import get_metrics
    return get_metrics()


@app.get("/generate-preview-status/{job_id}")
@limiter.limit("600/minute")
async def generate_preview_status(
    request: Request,
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Polling endpoint para el frontend. Backoff típico 1.5s → 5s."""
    try:
        from jobs import get_job_model
        job_row = get_job_model(db, job_id)
        if (not job_row
                or job_row.user_id != current_user["id"]
                or job_row.tenant_id != current_user["tenant_id"]):
            raise HTTPException(status_code=404, detail="Job not found.")
        return {
            "job_id": job_id,
            "status": job_row.status or "",
            "error": getattr(job_row, "error", None),
        }
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        logger.exception("[BG_PREVIEW_STATUS] job=%s 500: %s", job_id, exc)
        raise HTTPException(
            500,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
        )


# Deprecation metadata for the legacy multipart-form endpoints. RFC 8594
# `Sunset` + RFC 9745 `Deprecation` so any tooling that monitors the API
# (curl scripts, custom clients) gets a structured signal. Frontend now
# uses /upload-url + /transcribe-uploaded.
_DEPRECATION_DATE = "2026-08-01"  # mid-target removal
_DEPRECATION_HEADERS = {
    # Deprecation: signed integer (epoch seconds, or "true" per draft)
    "Deprecation": "true",
    "Sunset": "Mon, 01 Aug 2026 00:00:00 GMT",
    "Link": (
        '</docs/upload-url>; rel="successor-version", '
        '</docs/upload-url>; rel="deprecation"'
    ),
}


def _set_deprecation_headers(response: Response, endpoint: str) -> None:
    """Attach deprecation headers + log once per request so we can grep
    Sentry / Railway logs to find any remaining legacy callers before
    the sunset date."""
    for k, v in _DEPRECATION_HEADERS.items():
        response.headers[k] = v
    logger.warning(
        "[DEPRECATED] %s called — sunset %s. Use the presigned-R2 flow "
        "(/upload-url + /transcribe-uploaded) instead.",
        endpoint, _DEPRECATION_DATE,
    )


@app.post("/upload")
@limiter.limit("120/minute")
async def upload(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    # Alineados con database.py:Job columns:
    artist: str = Form(..., max_length=255),         # Job.artist = VARCHAR(255)
    song_title: str = Form("", max_length=500),      # Job.song_title = VARCHAR(500)
    style: str = Form("oscuro", max_length=50),      # Job.style = VARCHAR(50)
    language: str = Form("", max_length=16),
    delivery_profile: str = Form("youtube", max_length=20),  # Job.delivery_profile = VARCHAR(20)
    umg_frame_size: str = Form("", max_length=16),
    umg_fps: str = Form("", max_length=16),
    umg_prores_profile: str = Form("", max_length=4),
    background_id: int = Form(None),
    background_mode: str = Form("as_is", max_length=16),
    background_file: UploadFile = File(None),
    genre: str = Form("", max_length=64),
    font: str = Form("", max_length=64),
    concept: str = Form("", max_length=2000),
    movement_style: str = Form("", max_length=64),
    effect: str = Form("", max_length=32),
    animate_image: str = Form("", max_length=8),
    text_case: str = Form("upper", max_length=16),
    frame_format: str = Form("full", max_length=16),
    font_scale: str = Form("1.0", max_length=8),
    lyric_transition: str = Form("cut", max_length=16),
    text_motion: str = Form("none", max_length=16),
    lyrics_animation: str = Form("none", max_length=16),
    line_transition: str = Form("none", max_length=16),
    text_contrast: str = Form("medium", max_length=16),
    # Lyric text colors 2026-05-25. Hex `#RRGGBB` (7 chars), invalid input
    # normalized to "" by the call site so build_ass falls back to defaults.
    # max_length=8 keeps the validator's hex regex authoritative while
    # rejecting payload abuse. INCIDENT 2026-05-26: missing Form params
    # made every POST /upload reference the unbound local `lyric_color` →
    # NameError → 500. The endpoint is deprecated (removal 2026-08-01) but
    # still serviced; keep parity with /generate.
    lyric_color: str = Form("", max_length=8),
    lyric_sung_color: str = Form("", max_length=8),
    match_lyrics: bool = Form(True),
    background_hint: str = Form("", max_length=2000),
    bg_verbatim: bool = Form(False),
    custom_colors: str = Form("", max_length=200),
    # Add-on premium "Escenas" (multi-escena). Parity con /generate; la
    # elegibilidad se valida con has_scenes_access antes de forwardear.
    enable_scenes: bool = Form(False),
    # Title-card customization (Full Rotor v1). Defaults = historical look.
    title_template: str = Form("auto", max_length=16),
    title_size: str = Form("1.0", max_length=8),
    title_artist_font: str = Form("", max_length=64),
    title_song_font: str = Form("", max_length=64),
    # UI v1.1 (2026-05-30): manual song split. "" = auto wrap (default).
    # When set, contains the 2 lines joined by "\n" — capped at 200 chars
    # to match the song title's effective range.
    title_song_break: str = Form("", max_length=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Receive an MP3 and start processing.

    DEPRECATED: prefer POST /upload-url (presigned-R2 flow). This endpoint
    still works for direct-API callers but the API container now bears
    the upload memory + bandwidth cost. Removal: 2026-08-01.
    """
    background_mode = background_mode if background_mode in ("as_is", "variation") else "as_is"
    _set_deprecation_headers(response, "/upload")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    name_lower = file.filename.lower()
    if not name_lower.endswith(_AUDIO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="Only MP3 and WAV files are accepted.",
        )

    # Backfill artist/title from the filename when the client omitted them.
    # Two formats are supported (the operator picks whichever is convenient):
    #   "Artist - Title.ext"     → artist="Artist", title="Title"
    #   "Title_Artist.ext"       → title="Title",  artist="Artist"
    # The `_` form is what the YouTube/Suno export tool emits and what the
    # operator was uploading when the title was lost end-to-end.
    artist = (artist or "").strip()
    song_title = (song_title or "").strip()
    if not artist or not song_title:
        parsed_artist, parsed_title = _parse_filename_artist_title(
            file.filename or "", db=db, tenant_id=current_user.get("tenant_id", "")
        )
        if not artist:
            artist = parsed_artist
        if not song_title:
            song_title = parsed_title

    _enforce_plan_quota(db, current_user,
                        credits_needed=(scenes_credit_cost()
                                        if enable_scenes and has_scenes_access(current_user)
                                        else 1))
    _enforce_daily_volume_cap(db, current_user)
    _enforce_tenant_backlog(db, current_user)
    _enforce_disk_capacity()
    _enforce_memory_pressure()
    # Every submission is accepted as queued; RQ gives it to a worker the
    # moment one is free, and pipeline.run_pipeline flips status to
    # "processing" on its first line. No 429 for capacity reasons.
    initial_status = "queued"

    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile, current_user=current_user)

    # Check AI authorization (UMG Guideline 5). The skip applies only when
    # the operator picks a library asset AND uses it as-is — no AI invoked.
    # Variation mode still calls Veo image-to-video on a frame of the
    # source, which IS AI generation, so the auth gate must apply.
    _needs_ai_auth = (not background_id) or (background_mode == "variation")
    if _needs_ai_auth and current_user.get("role") != "admin":
        user_model = db.query(User).filter(User.id == current_user["id"]).first()
        if user_model and not user_model.ai_authorized:
            raise HTTPException(status_code=403, detail="AI tool usage not authorized. Contact admin for approval.")

    # Check plan limits
    usage_info = get_plan_usage(db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"),
                                billing_group=current_user.get("billing_group"))
    if usage_info["alert_100"] and current_user.get("plan") == "free":
        raise HTTPException(status_code=429, detail="Free plan limit reached. Upgrade to continue.")

    tenant_id = current_user["tenant_id"]
    # SECURITY + DEDUP: sanitize filename BEFORE create_job so the row's
    # canonical name matches what `supersede_sibling_drafts` will filter
    # on. PR #388 bug #2: filename equality is literal, so unsanitized
    # vs sanitized forms silently miss every sibling. _safe_basename
    # strips directory components + control chars + length-caps.
    safe_name = _safe_basename(file.filename)
    job_id = create_job(
        db,
        artist=artist, style=style, filename=safe_name,
        user_id=current_user["id"], tenant_id=tenant_id,
        delivery_profile=delivery_profile, umg_spec=umg_spec,
        initial_status=initial_status,
        song_title=song_title,
    )
    # 2026-05-28 dedup gap (audit #88): mirror the /generate pattern
    # (main.py ~5688) for the legacy multipart /upload path. No-op when
    # no siblings; catches double-fires.
    try:
        from jobs import supersede_sibling_drafts
        supersede_sibling_drafts(
            db, keep_job_id=job_id, user_id=current_user["id"],
            tenant_id=tenant_id, filename=safe_name,
        )
    except Exception as e:
        logger.warning("[DEDUP] supersede sibling drafts failed: %s", e)

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    mp3_path = os.path.join(job_dir, safe_name)
    await _stream_upload_to_disk(file, mp3_path)
    _validate_audio_file_on_disk(safe_name, mp3_path)

    # Upload the input MP3 to R2 regardless of whether the job will run now
    # or wait in the queue — the worker container fetches from R2 (the API
    # container disk is ephemeral and may be gone by the time a queued job
    # promotes to processing minutes/hours later).
    input_r2_key = None
    if storage.is_enabled():
        input_r2_key = storage.upload_input(
            mp3_path, tenant_id, job_id, file.filename,
        )

    # Resolve background: library asset > custom upload > AI generation
    bg_path = None
    bg_r2_key = None
    variation_source_path = None
    variation_source_r2_key = None
    variation_parent_id = None
    if background_id:
        bg_path, bg_r2_key, variation_source_path, variation_source_r2_key, variation_parent_id = (
            _resolve_library_background(
                background_id, background_mode, current_user, db, job_dir, job_id,
            )
        )
    elif background_file and background_file.filename:
        # Valida (magic bytes + tamaño), sube a R2 y persiste
        # bg_r2_key_cached para que edits y /retry preserven el archivo.
        bg_path, bg_r2_key = _save_custom_background(
            background_file, job_dir, job_id, tenant_id,
        )

    lang = language.strip() if language.strip() else None

    # Always enqueue. RQ's per-priority worker pool naturally caps how many
    # jobs run at once — the rest wait in Redis. UMG (plan=unlimited) goes
    # to the enterprise queue, which workers drain before the default queue.
    _font_scale = 1.0
    try:
        _font_scale = max(0.6, min(1.5, float(font_scale or "1.0")))
    except (ValueError, TypeError):
        pass

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=current_user.get("plan", "100"),
        tenant_id=current_user.get("tenant_id", ""),
        language=lang,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
        background_path=bg_path,
        input_r2_key=input_r2_key,
        bg_r2_key=bg_r2_key,
        variation_source_path=variation_source_path,
        variation_source_r2_key=variation_source_r2_key,
        variation_parent_asset_id=variation_parent_id,
        genre=genre,
        font=font,
        concept=concept,
        movement_style=movement_style,
        effect=effect,
        animate_image=str(animate_image).strip().lower() in ("true", "1", "yes", "on"),
        song_title=song_title,
        text_case=text_case if text_case in ("upper", "title", "lower", "original", "sentence") else "upper",
        frame_format=frame_format if frame_format in ("full", "cine") else "full",
        font_scale=_font_scale,
        # lyric_transition + text_motion: deprecados 2026-05-23 (ver run_pipeline).
        # Aceptamos los Form params por back-compat pero coerce a defaults.
        lyric_transition="cut",
        text_motion="none",
        lyrics_animation=lyrics_animation if lyrics_animation in ("none", "karaoke", "word_reveal", "pop", "glow") else "none",
        line_transition=line_transition if line_transition in ("none", "slide_up", "slide_side", "wipe", "dissolve_blur") else "none",
        # Lyric text colors 2026-05-25. Hex #RRGGBB validado acá; cualquier
        # otro valor se normaliza a "" (= backend usa blanco default en
        # build_ass). Para karaoke: lyric_color = palabra no cantada,
        # lyric_sung_color = palabra cantada. Para otras animaciones:
        # lyric_color = único color del texto.
        lyric_color=(lyric_color.strip() if lyric_color and re.match(r"^#[0-9a-fA-F]{6}$", lyric_color.strip() or "") else ""),
        lyric_sung_color=(lyric_sung_color.strip() if lyric_sung_color and re.match(r"^#[0-9a-fA-F]{6}$", lyric_sung_color.strip() or "") else ""),
        text_contrast=text_contrast if text_contrast in ("subtle", "medium", "strong") else "medium",
        match_lyrics=match_lyrics,
        background_hint=(background_hint.strip() or None),
        bg_verbatim=bg_verbatim,
        custom_colors=(custom_colors.strip() or ""),
        # Escenas (multi-escena): opt-in AND elegibilidad real (parity /generate).
        enable_scenes=bool(enable_scenes) and has_scenes_access(current_user),
        title_template=title_template if title_template in ("auto", "centered", "lower_third", "badge") else "auto",
        title_size=_clamp_title_size(title_size),
        title_artist_font=(title_artist_font.strip() or ""),
        title_song_font=(title_song_font.strip() or ""),
        # UI v1.1: pass-through. Empty string preserves auto-wrap.
        title_song_break=(title_song_break or ""),
    )

    return {"job_id": job_id, "status": initial_status}


@app.post("/transcribe")
@limiter.limit("20/minute")
async def transcribe_endpoint(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    language: str = Form("", max_length=16),
    artist: str = Form("", max_length=255),     # Job.artist = VARCHAR(255)
    title: str = Form("", max_length=500),      # Job.song_title = VARCHAR(500)
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Transcribe an MP3 or WAV and return segments for review/editing.

    DEPRECATED: prefer the presigned-R2 flow (/upload-url +
    /transcribe-uploaded). This endpoint still works but the audio body
    flows through the API container, defeating the OOM fix. Removal:
    2026-08-01.
    """
    _set_deprecation_headers(response, "/transcribe")
    if not file.filename.lower().endswith(_AUDIO_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Only MP3 and WAV files are accepted.")

    _enforce_disk_capacity()
    _enforce_memory_pressure()

    import tempfile
    import asyncio

    artist_form = (artist or "").strip()
    title_form = (title or "").strip()
    parsed_artist, parsed_title = _parse_filename_artist_title(
        file.filename or "", db=db, tenant_id=current_user.get("tenant_id", "")
    )
    job_artist = artist_form or parsed_artist or "Unknown"
    job_song_title = title_form or parsed_title

    # SECURITY + DEDUP (audit #90): canonicalize filename BEFORE
    # create_job so the row's persisted name matches the form
    # supersede_sibling_drafts will compare against (PR #388 bug #2).
    safe_audio_name = _safe_basename(file.filename)
    job_id = create_job(
        db,
        artist=job_artist,
        style="oscuro",                    # set for real in /generate
        filename=safe_audio_name,
        user_id=current_user["id"],
        tenant_id=current_user["tenant_id"],
        delivery_profile="youtube",        # set for real in /generate
        initial_status="transcribed_pending",
        song_title=job_song_title,
    )
    # 2026-05-28 dedup gap: mirror /generate (main.py ~5688) on the
    # legacy /transcribe path. No-op when no siblings.
    try:
        from jobs import supersede_sibling_drafts
        supersede_sibling_drafts(
            db, keep_job_id=job_id, user_id=current_user["id"],
            tenant_id=current_user["tenant_id"], filename=safe_audio_name,
        )
    except Exception as e:
        logger.warning("[DEDUP] supersede sibling drafts failed: %s", e)

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    audio_path = os.path.join(job_dir, safe_audio_name)
    # Stream the body in 1 MiB chunks. Lossless WAVs (~30-50 MB for a
    # 3-min track) used to OOM the API container under concurrent load
    # because we buffered the full payload in RAM.
    await _stream_upload_to_disk(file, audio_path)
    _validate_audio_file_on_disk(safe_audio_name, audio_path)

    # Cross-replica handoff. When the API and worker run in separate
    # containers (Railway production) the file written above is invisible
    # to /generate if the next request lands on a different replica. Push
    # to R2 so the worker can fetch it later.
    if storage.is_enabled():
        try:
            input_r2_key = storage.upload_input(
                audio_path, current_user["tenant_id"], job_id, file.filename,
            )
            if input_r2_key:
                from jobs import get_job_model
                job_row = get_job_model(db, job_id)
                if job_row:
                    job_row.input_r2_key = input_r2_key
                    db.commit()
        except Exception as e:
            # Best-effort. Same-replica fallback still works via local disk;
            # the cross-replica failure mode is the user re-doing the upload.
            logger.error("[TRANSCRIBE] R2 upload failed for %s: %s", job_id, e)

    # Per-request scratch dir for intermediate slices (intro/body cuts).
    # The main audio file lives under job_dir and stays around until
    # /generate enqueues it (or the reaper cleans it up).
    # Release the pooled connection before the long transcription (pool
    # starvation fix) — the function opens its own short-lived session.
    db.close()
    _result = await _run_transcription_for_job(
        request, current_user, job_id, audio_path,
        language=language, artist=artist, title=title,
        filename=file.filename,
    )
    _result = await _maybe_ctc_retime(_result, audio_path, job_id, artist, title)
    _result = await _maybe_adlib_filter(_result, audio_path, job_id,
                                        live_hint=_looks_live(title, file.filename))
    from lyrics_format import format_lyrics_pass as _fmt
    return await _fmt(_result, language=language or "es")


from ctc_cascade_veto import _ctc_cascade_veto  # noqa: E402


_LIVE_MARKER_RE = re.compile(
    r"\b(live|en\s+vivo|vivo|ac[uú]stic[oa]|unplugged|directo|concert|"
    r"session(?:es)?|sesi[oó]n)\b", re.IGNORECASE)


def _looks_live(*texts) -> bool:
    """¿Algún texto (título/filename) sugiere una versión en vivo/alternativa?
    Señal barata para armar la auditoría de sufijo. El catálogo etiqueta los
    vivos en el título ('Live In Buenos Aires 2001'); para archivos sin
    etiquetar existe el toggle del operador (body.live)."""
    return any(t and _LIVE_MARKER_RE.search(str(t)) for t in texts)


async def _maybe_adlib_filter(result, audio_path: str, job_id: str,
                              live_hint: bool = False):
    """Paso post-cascada (gate ADLIB_CONSENSUS_ENABLED, default off): descarta
    líneas fantasma alucinadas en zonas de ad-lib y colapsa los 'uh'
    fragmentados. Corre en TODOS los caminos de la cascada (whisperx,
    reconcile, CTC, scaffold) — a diferencia del retime de CTC, que solo
    actúa en su rama. Así el output queda limpio sin importar qué
    segmentación produjo la cascada (que es no-determinista por la
    variabilidad de whisperX en Replicate).

    No-op verdadero en canciones sin ad-libs (cero candidatas → cero
    regresión, verificado en 12 UMG gold). Never raises: ante cualquier
    fallo devuelve el result intacto. Consigue el stem del cache R2 (la
    cascada lo dejó ahí); si no está y no se puede computar, declina."""
    if not isinstance(result, dict):
        return result
    # pop SIEMPRE (aunque el gate esté off): wx_raw es transporte interno,
    # no debe persistirse ni llegar al response del endpoint legacy.
    _wx_raw = result.pop("wx_raw", None)
    if os.environ.get("ADLIB_CONSENSUS_ENABLED", "0").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return result
    segs = result.get("segments") or []
    if len(segs) < 3:
        return result
    _stem = None
    try:
        import adlib_consensus as _ac
        _has_adlib = any(_ac.is_adlib_text(s.get("text", "")) for s in segs)
        _tail_on = os.environ.get("ADLIB_TAIL_CHECK_ENABLED", "1") \
            .strip().lower() in ("1", "true", "yes", "on")
        # Auditoría de sufijo (Perro Amor Explota LIVE, 06/07): el final del
        # audio es de OTRA versión (vivo↔estudio) con voz real en la cola —
        # el VAD no lo ve. Solo se arma con live_hint (título con marcador
        # live o toggle del operador): medido contra el gold (06/07), en
        # canciones normales los finales quietos/en capas dan falsos
        # positivos. Y MARCA (review), no borra — ver filter_and_collapse.
        _audit_on = live_hint and os.environ.get(
            "ADLIB_SUFFIX_AUDIT_ENABLED", "1") \
            .strip().lower() in ("1", "true", "yes", "on")
        # Barato primero: sin ad-libs y sin ningún chequeo de final, ni
        # tocamos el stem.
        if not _has_adlib and not _tail_on and not _audit_on:
            return result
        import vocal_sep as _vs
        _stem = await asyncio.wait_for(
            asyncio.to_thread(_vs.separate_vocals, audio_path, cache_only=True),
            timeout=120,
        )
        # Computar el stem solo si hay ad-libs (el camino histórico). Para
        # el chequeo de cola solo, un cache miss no justifica Demucs.
        if not _stem and _has_adlib and os.environ.get(
                "CTC_ALIGN_COMPUTE_STEM", "1").strip().lower() in ("1", "true", "yes", "on"):
            _stem = await asyncio.wait_for(
                asyncio.to_thread(_vs.separate_vocals, audio_path), timeout=360)
        if not _stem:
            logger.info("[ADLIB] sin stem — omito el filtro (job=%s)", job_id)
            return result
        # Cola muda: fin del último canto según VAD de energía del stem.
        # Caso El Riesgo (05/07): lrclib trajo la letra de otra edición con
        # un outro cantado que este audio no tiene; el scaffold puso esas
        # líneas sobre 76s de música instrumental. Si la cola muda es larga
        # (>10s), las líneas que caen ahí se verifican acústicamente.
        _tail_after = None
        if _tail_on:
            try:
                from anchor_align import vocal_regions as _vr
                _regs = await asyncio.to_thread(_vr, _stem)
                if _regs:
                    _last_voice = _regs[-1][1]
                    _last_line_end = max(
                        (float(s.get("end", 0)) for s in segs), default=0.0)
                    if _last_line_end - _last_voice > 10.0:
                        _tail_after = _last_voice
                        logger.info(
                            "[ADLIB] cola muda: voz hasta %.1fs, líneas hasta "
                            "%.1fs — verificando la cola (job=%s)",
                            _last_voice, _last_line_end, job_id)
            except Exception as e:
                logger.warning("[ADLIB] VAD de cola falló: %r — sin chequeo "
                               "de cola (job=%s)", e, job_id)
        if not _has_adlib and _tail_after is None and not _audit_on:
            return result
        _tw = _make_stem_window_transcriber(_stem)
        _before = len(segs)
        filtered = await asyncio.to_thread(
            _ac.filter_and_collapse, segs, _tw, tail_after=_tail_after,
            audit_suffix=_audit_on)
        # MODO VIVO (Perro live, 06/07): el sufijo que la auditoría marcó
        # se reemplaza por los segmentos crudos de whisperX de esa zona —
        # la letra de estudio no puede representar el final de un vivo
        # (call-response, presentaciones de la banda). Las líneas
        # insertadas conservan review=True.
        if (live_hint and _wx_raw
                and os.environ.get("ADLIB_LIVE_SWAP_ENABLED", "1")
                .strip().lower() in ("1", "true", "yes", "on")):
            filtered = _ac.live_swap_tail(filtered, _wx_raw)
        if filtered != segs:
            result = dict(result)
            result["segments"] = filtered
            logger.info("[ADLIB] consenso: %d → %d líneas (job=%s)",
                        _before, len(filtered), job_id)
    except Exception as e:
        logger.warning("[ADLIB] filtro post-cascada declinó: %r (job=%s)", e, job_id)
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass
    return result


def _make_stem_window_transcriber(stem_path: str):
    """Devuelve transcribe_window(start, end) -> str: recorta esa ventana del
    stem de voz y la transcribe con whisper-1. Para el filtro de consenso
    (adlib_consensus): solo se llama en líneas candidatas (pocas por canción).
    Recorta con un pad chico y compensa el lead-in para caer sobre el onset
    real. Devuelve '' ante cualquier problema (el filtro conserva la línea)."""
    import subprocess
    import tempfile

    def _transcribe_window(start: float, end: float) -> str:
        a = max(0.0, float(start) + 0.4 - 0.25)   # +0.4 deshace el lead; -0.25 pad
        dur = max(1.8, float(end) - float(start) + 0.3)
        fd, clip = tempfile.mkstemp(suffix=".wav", prefix="genly_adlib_")
        os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", stem_path,
                 "-ss", str(a), "-t", str(dur), clip],
                check=True, timeout=30,
            )
            from pipeline import _transcribe_via_openai_api as _wx
            segs = _wx(clip, language="es") or []
            return " ".join((s.get("text") or "").strip() for s in segs).strip()
        except Exception as e:
            logger.warning("[ADLIB] window %.1f-%.1f transcribe failed: %s",
                           start, end, e)
            return ""
        finally:
            try:
                os.unlink(clip)
            except OSError:
                pass

    return _transcribe_window


async def _maybe_ctc_retime(result, audio_path: str, job_id: str,
                            artist: str = "", title: str = ""):
    """Gated post-pass over the cascade's FINAL output (CTC_ALIGN_ENABLED,
    default OFF): re-time every line by full-song monotonic CTC forced
    alignment on the vocal stem (`ctc_align.py`). Texts pass through
    verbatim — only start/end + word stamps change. Declines (returns
    `result` untouched) on any failure, so behaviour with the flag off
    is byte-identical.

    Lives OUTSIDE `_run_transcription_for_job` on purpose: the
    orchestrator deletes its vocal stem in its `finally`, and its exit
    paths all funnel through `_emit_segments` (AST-guarded — adding a
    second mutation point inside would weaken that contract). Wrapping
    the result at the call sites re-fetches the stem via the R2 stem
    cache (a hit — the cascade computed it seconds ago), so no second
    demucs run is paid."""
    _stem = None
    try:
        # Everything (the import too) lives inside the try: a broken
        # ctc_align module must degrade to "no retime", never to a 500
        # on every transcription — including with the flag OFF.
        import ctc_align as _ctc
        if not _ctc.is_enabled() or not isinstance(result, dict):
            return result
        segs = result.get("segments") or []
        if len(segs) < 3:
            return result
        import vocal_sep as _vs
        # cache_only primero: si la cascada computó el stem hace segundos,
        # esto es solo una descarga de R2.
        _stem = await asyncio.wait_for(
            asyncio.to_thread(_vs.separate_vocals, audio_path, cache_only=True),
            timeout=120,
        )
        if not _stem and os.environ.get(
                "CTC_ALIGN_COMPUTE_STEM", "1").strip().lower() in ("1", "true", "yes", "on"):
            # Sin stem cacheado → COMPUTARLO (regresión Amanda Pujó,
            # 03/07): cuando la transcripción viene del cache, la cascada
            # nunca corre demucs, el wrapper caía a la mezcla, y en mezclas
            # indie (voz enterrada) CTC declinaba estructural (13/29 skips)
            # dejando el timing del reconcile (línea fantasma + puente
            # 2.6s tarde). Sobre el stem la misma canción alinea 29/29.
            # Costo: ~60-180s de Replicate + ~$0.005, UNA vez por canción
            # (separate_vocals sube el stem al cache R2). El timeout deja
            # margen para el peor caso de Replicate.
            logger.info("[CTC] no cached stem — computing it (job=%s)", job_id)
            _stem = await asyncio.wait_for(
                asyncio.to_thread(_vs.separate_vocals, audio_path),
                timeout=360,
            )
        # Fallback a la MEZCLA (medido en el gold set, 03/07): la
        # declinación de CTC varía según la fuente — Grignani alineó
        # med 0.105s/74.5%≤0.3s y PROVENZA 0.177s/76% SOBRE LA MEZCLA
        # habiendo declinado sobre el stem (demucs sobre-separa algunas
        # mezclas); Rata Blanca es el caso inverso (mezcla declina por
        # score, stem alinea perfecto). La corrida completa del gold set
        # (40 canciones, 100% sobre mezcla) dio 50.7% ≤0.3s — la mezcla
        # como fuente está validada. Costo del retry: solo CPU local.
        _mix_fallback = os.environ.get(
            "CTC_ALIGN_MIX_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")
        retimed = None
        _stem_structural = False
        if _stem:
            retimed = await asyncio.wait_for(
                asyncio.to_thread(_ctc.retime_segments, _stem, segs, job_id,
                                  audio_path),  # mix_path — M5 crowd recovery
                timeout=420,
            )
            # Solo confiar en last_decline_reason (global de módulo) si
            # ESTE call corrió — sin stem quedaría el valor del job anterior.
            _stem_structural = (retimed is None
                                and _ctc.last_decline_reason == "structural")
        elif not _mix_fallback:
            logger.info("[CTC] no cached stem — skipping retime (job=%s)", job_id)
            return result
        else:
            logger.info("[CTC] no cached stem — aligning on the MIX (job=%s)", job_id)
        if (_stem_structural
                and os.environ.get("CTC_ALIGN_PERF_TEXT", "0").strip().lower()
                in ("1", "true", "yes", "on")):
            # ETAPA A — the cascade's text doesn't match what this live
            # actually performs (extra verse/chorus repetitions, crowd
            # variants). Transcribe THE PERFORMANCE (Gemini chunked,
            # ~$0.03 / 60-90 s) and retime that libretto instead. Higher
            # skip tolerance: crowd lines ARE in the libretto but
            # invisible in the stem until M5/transplant pick them up.
            import performance_text as _pt
            texts = await asyncio.wait_for(
                asyncio.to_thread(_pt.transcribe_performance, audio_path,
                                  artist, title,
                                  result.get("reference_lyrics") or ""),
                timeout=300,
            )
            if texts:
                psegs = [{"text": t, "start": 0.0, "end": 0.0} for t in texts]
                retimed = await asyncio.wait_for(
                    asyncio.to_thread(_ctc.retime_segments, _stem, psegs,
                                      job_id, audio_path, 0.65),
                    timeout=600,
                )
                if retimed:
                    # Rotor-style: crowd repetitions the stem can't anchor
                    # individually condense into one block line; leftover
                    # unplaced lines drop instead of stacking.
                    retimed = _ctc.condense_repeated_skips(retimed)
                    # Veto: protect high-confidence cascade segments from
                    # Gemini hallucinations at repeated melodic phrases.
                    retimed = _ctc_cascade_veto(retimed, segs)
                    logger.info("[CTC] performance-libretto retime: %d líneas "
                                "(reemplaza el texto de la cascada, job=%s)",
                                len(retimed), job_id)
        # Retry sobre la MEZCLA cuando el stem declinó por score (no
        # estructural — eso ya tiene su camino perf-text arriba, y una
        # mezcla no arregla un libreto que no matchea). También cubre el
        # caso sin stem cacheado (retimed sigue None y nunca corrimos el
        # primer align).
        if retimed is None and _mix_fallback and not _stem_structural:
            retimed = await asyncio.wait_for(
                asyncio.to_thread(_ctc.retime_segments, audio_path, segs, job_id),
                timeout=420,
            )
            if retimed:
                logger.info("[CTC] retime sobre la MEZCLA OK (stem %s, job=%s)",
                            "declinó" if _stem else "ausente", job_id)
        if retimed:
            # Re-aplicar el lead-in: la cascada lo aplicó dentro de
            # _emit_segments, pero el retime de CTC produce onsets
            # acústicos frescos SIN lead — sin esta línea, justo las
            # canciones donde CTC actúa perdían el lead-in (#801) y
            # ambas mejoras se anulaban entre sí. No hay doble
            # aplicación: en el camino de declive los segmentos de la
            # cascada (ya con lead) pasan intactos.
            # El filtro de ad-libs se mudó a _maybe_adlib_filter (paso
            # post-cascada, corre en TODOS los caminos — no solo cuando CTC
            # retima). Acá solo re-aplicamos el lead-in sobre los onsets
            # frescos de CTC (#801).
            import lead_in as _lead_in
            result = dict(result)
            result["segments"] = _lead_in.polish(retimed)
            from jobs import set_timing_source
            from timing_sources import CTC_ALIGN
            set_timing_source(job_id, CTC_ALIGN)
    except Exception as e:
        logger.warning("[CTC] retime wrapper declined: %r (job=%s)", e, job_id)
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass
    return result


async def _run_transcription_for_job(
    request, current_user, job_id: str, audio_path: str,
    *, language: str = "", artist: str = "", title: str = "",
    filename: str = "",
):
    """Shared transcription pipeline: lrclib synced/plain → Whisper →
    hallucination recovery → segments. Used by both /transcribe (legacy
    multipart upload) and /transcribe-uploaded (presigned-R2 path).

    NOTE (pool starvation fix, 2026-05-21): this runs ~15-20 s of Whisper +
    Vertex work. It must NOT hold a pooled DB connection across that span —
    the per-process pool is only ~10 slots (Railway 100-conn cap / ~8
    procs), so a held connection here starves /usage and /jobs and freezes
    the dashboard. The only DB touch is the lrclib cache lookup below, which
    is wrapped in a short-lived scoped_db() block. Callers release their
    request session (db.close()) before calling this.

    Returns the standard `{job_id, segments, reference_lyrics, ...}`
    dict. Cleans up its own scratch dir but never touches `audio_path`
    (caller owns that file)."""
    import tempfile
    import asyncio

    if not filename:
        filename = os.path.basename(audio_path)

    tmp_dir = tempfile.mkdtemp()
    tmp_path = audio_path
    _vocal_stem = None   # demucs output path (lazy), cleaned up in finally

    try:
        lang = language.strip() if language.strip() else None
        loop = asyncio.get_event_loop()

        # Progress emission helper. The render pipeline already writes
        # current_step + progress so the frontend SSE/poll can render a
        # multi-step UI; this gives the transcription pipeline the same
        # surface. Labels are i18n keys the frontend maps to localised
        # step names (`transcribe.prepare`, `.lyrics_lookup`,
        # `.isolate_vocals`, `.verify`, `.align`, `.transcribe`,
        # `.transcribe_word`, `.recover`, `.done`). Fire-and-forget via
        # to_thread so a slow DB write never blocks the pipeline.
        from jobs import update_job as _update_job
        async def _step(label: str, pct: int):
            try:
                await asyncio.to_thread(
                    _update_job, job_id, current_step=label, progress=pct,
                )
            except Exception as e:
                logger.warning("[PROGRESS] %s/%d failed: %s", label, pct, e)
        await _step("transcribe.prepare", 5)

        # Vocal source separation (demucs) — LAZY. Previously demucs ran at the
        # top of every job (~30-90s) regardless of which downstream engine
        # actually needed the stem. On songs where lrclib synced verifies, no
        # forced-align / whisperX call ever happens, so the stem was burned for
        # nothing. The wrapper below runs demucs at most ONCE, on first request,
        # and caches the result. If nothing calls it, demucs never runs and the
        # job is 60-90s faster.
        #
        # The stem feeds FA / whisperX / verification ONLY — NOT the bare
        # whisper-1 pass, which is more hallucination-prone on isolated vocals
        # (long-form caveat, arXiv 2506.15514).
        import vocal_sep
        _stem_state = {"computed": False, "path": None}

        # Final post-processing chain applied to EVERY return path so all
        # timing sources (forced_align / whisperx / whisperx_reconciled /
        # whisper-1 fallback) get the same polish:
        #   1. split_long_segments — break >6s lines at biggest word gap
        #      (only when per-word stamps are present; no-op otherwise)
        #   2. beat_snap — snap starts to the song's beat grid (±200ms)
        #   3. mark_repetitions — tag chorus loops with repetition_group
        #   4. lead_in — adelantar la aparición de la línea (karaoke lead;
        #      LYRIC_LEAD_IN_S, default 0 = off). Va ÚLTIMO para operar
        #      sobre los starts ya definitivos; los word-stamps no se
        #      tocan, así el highlight sigue clavado al onset real.
        # See whisperx_transcribe.py, beat_snap.py, chorus_trim.py, lead_in.py.
        import beat_snap as _beat_snap
        import chorus_trim as _chorus_trim
        import lead_in as _lead_in
        from whisperx_transcribe import _split_long_segments as _split_long
        def _snap(segs):
            return _lead_in.polish(
                _chorus_trim.mark_repetitions(
                    _beat_snap.apply(tmp_path,
                        _split_long(segs)
                    )
                )
            )

        # ─── single chokepoint for every segments-bearing return ──────
        # `_emit_segments` is the ONE allowed exit point of this
        # function for any return that ships segments. It guarantees:
        #   1. `source` is in `VALID_TIMING_SOURCES` (banishes Bug D —
        #      the four fallback paths that historically returned
        #      `timing_source=NULL`).
        #   2. `set_timing_source(job_id, source)` is called ALWAYS.
        #   3. `normalize_words` runs (banishes Bug B — FA wordstamps
        #      with score get preserved, Whisper-1 raw words get
        #      stripped; before, the tail return stripped ALL words
        #      unconditionally).
        #   4. `_snap` (split → beat-snap → chorus repetitions) runs.
        #   5. Returns the canonical dict shape.
        #
        # Adding a new return path? Use `return _emit_segments(...)` —
        # the AST regression test in `test_emit_segments.py` will fail
        # if a raw `return {"segments": ...}` slips into this function.
        from timing_sources import VALID_TIMING_SOURCES, WHISPER_RAW
        from transcribe_postprocess import normalize_words as _normalize_words
        from transcribe_postprocess import dedup_collisions as _dedup_collisions

        def _emit_segments(segments, source, *,
                            reference_lyrics: str = "",
                            recovery_source=None,
                            coverage_warning: bool = False,
                            extra=None):
            if source not in VALID_TIMING_SOURCES:
                logger.error("[EMIT] invalid timing_source=%r — forcing %r "
                             "(job=%s)", source, WHISPER_RAW, job_id)
                source = WHISPER_RAW
            try:
                from jobs import set_timing_source
                set_timing_source(job_id, source)
            except Exception as e:  # set_timing_source already swallows; defensive
                logger.warning("[EMIT] set_timing_source(%s, %s) raised: %s",
                               job_id, source, e)
            deduped = _dedup_collisions(segments)
            if deduped and segments and len(deduped) != len(segments):
                logger.info("[EMIT] deduped collisions: %d → %d segments (job=%s)",
                            len(segments), len(deduped), job_id)
            polished = _snap(_normalize_words(deduped))
            out = {"job_id": job_id, "segments": polished,
                   "reference_lyrics": reference_lyrics}
            # Segmentos crudos de whisperX (la performance REAL): viajan en
            # result para que el modo vivo pueda reemplazar el sufijo
            # divergente por lo que se canta (live_swap_tail). Se hace pop
            # en _maybe_adlib_filter — no se persisten ni llegan al cliente.
            try:
                if _wx_segs:
                    out["wx_raw"] = [
                        {"start": float(s.get("start", 0)),
                         "end": float(s.get("end", 0)),
                         "text": (s.get("text") or "").strip()}
                        for s in _wx_segs if (s.get("text") or "").strip()]
            except NameError:
                pass  # camino que emite antes de correr whisperX
            if recovery_source:
                out["recovery_source"] = recovery_source
            if coverage_warning:
                out["coverage_warning"] = True
            if extra:
                out.update(extra)
            return out

        async def _get_align_audio() -> str:
            """Return the path that alignment/transcription engines should read.
            Lazy-separates the vocal stem on first call. Falls back to the mix
            (`tmp_path`) when vocal_sep is disabled / fails.

            UI smoothness: previously emitted `progress=25` once and then sat
            silent during the 60-180 s Demucs call on Replicate, so the bar
            looked frozen. We now pass an `on_progress` callback that maps
            Demucs' 0..1 fraction (parsed from Replicate logs / time-based
            fallback) into the 25..48 range and schedules `_step` writes via
            `asyncio.run_coroutine_threadsafe` so the callback (running in a
            worker thread, not the event loop) can update job.progress
            without blocking. We stop at 48 (not 50) so the genuine handoff
            to the next stage produces a visible jump.
            """
            nonlocal _vocal_stem
            if not _stem_state["computed"]:
                _stem_state["computed"] = True
                await _step("transcribe.isolate_vocals", 25)

                # Bridge the sync callback (called from inside the thread
                # running call_with_budget) back into our async _step. The
                # event loop lives on the main thread; we schedule the
                # coroutine on it via run_coroutine_threadsafe.
                _loop_for_progress = asyncio.get_running_loop()
                _last_pct = {"value": 25}

                def _on_demucs_progress(fraction: float) -> None:
                    pct = 25 + int(round(max(0.0, min(1.0, fraction)) * (48 - 25)))
                    if pct <= _last_pct["value"]:
                        return                       # monotonic — never go backwards
                    _last_pct["value"] = pct
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _step("transcribe.isolate_vocals", pct),
                            _loop_for_progress,
                        )
                    except RuntimeError:
                        # Loop is closing (request cancelled). Drop quietly.
                        pass

                stem = await asyncio.to_thread(
                    vocal_sep.separate_vocals, tmp_path, _on_demucs_progress,
                )
                _stem_state["path"] = stem
                _vocal_stem = stem
                if stem:
                    logger.info("[LYRICS] isolated vocal stem (lazy) — using for alignment/whisperX/verification")
            return _stem_state["path"] or tmp_path

        # Resolve artist + title for the reference-lyrics fetch. Source order:
        #   1) explicit form fields (frontend already collects `artist` per
        #      file in UploadZone — we now forward it),
        #   2) "Artist - Title" pattern in the filename (legacy fallback),
        #   3) bare filename as title with no artist (Gemini-search will be
        #      skipped — see the empty-artist guard inside the fetcher).
        # Suffixes like "(Official Video)" are scrubbed in either case.
        basename = os.path.splitext(filename)[0]
        artist_hint = artist.strip()
        song_hint = title.strip() or basename
        if not artist_hint and " - " in basename:
            artist_hint, song_hint = basename.split(" - ", 1)
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(En Vivo)", "(Live)", "(Lyrics)",
                     "- River Plate", "- Luna Park", "- En Vivo"]:
            song_hint = song_hint.replace(sfx, "").strip()
        if not artist_hint:
            logger.warning("[LYRICS] no artist supplied for %r — Gemini fetch will be skipped, falling through to lyrics.ovh", filename)

        # Fast-path: lrclib.net often has synced (LRC) lyrics for popular
        # songs — when that's the case we skip Whisper entirely. Whisper
        # API is the source of the hallucination problems UMG hit on Karol G
        # ("¡Karol!" repeated 174x then dropped, leaving the second half
        # of the song without subtitles). Community-curated synced lyrics
        # have no such failure mode.
        #
        # Caveat: lrclib's synced timestamps are tied to a SPECIFIC version
        # of the audio (usually the studio mix). If the user uploads the
        # "Official Video" version with a dialogue intro added, every
        # subtitle will be ~30 s early. We compare audio duration against
        # lrclib's reported duration:
        #   diff <= 3 s          → use synced as-is
        #   3 s < diff <= 60 s   → assume intro added; offset all timestamps
        #                          by +diff (reasonable for the common case)
        #   diff > 60 s          → fall back to plain + Whisper (live /
        #                          extended / remix versions are too risky
        #                          to auto-align)
        from pipeline import (
            _fetch_lrclib, _fetch_lrclib_with_swap_retry,
            _lrc_to_segments, _audio_duration,
            _slice_audio_prefix, _slice_audio_window, _verify_lrclib_alignment,
            _detect_hallucination, _synthesize_segments_from_plain,
            _align_whisper_to_plain, _fill_gaps_with_reference,
        )
        # Short-lived DB session JUST for the lrclib cache lookup, released
        # immediately so the connection is free during the long Whisper /
        # Vertex work below (see the pool-starvation note in the docstring).
        await _step("transcribe.lyrics_lookup", 15)
        # Audio duration lets the lrclib picker prefer the version that
        # matches THIS upload (radio edit / extended / cover) instead of
        # grabbing a wrong-length record under the same title. Best-effort:
        # a failed probe just falls back to duration-agnostic picking.
        try:
            _audio_dur_for_lrc = await asyncio.to_thread(_audio_duration, tmp_path)
        except Exception:
            _audio_dur_for_lrc = None
        try:
            with scoped_db() as _lrc_db:
                lrc, _lrc_meta = await asyncio.to_thread(
                    _fetch_lrclib_with_swap_retry, artist_hint, song_hint, _lrc_db,
                    _audio_dur_for_lrc,
                )
        except Exception as _lrc_db_err:
            # Transient Postgres SSL drop (Neon cold-start after idle period).
            # Same fallback as the genius/gemini blocks: treat as a cache miss
            # so the pipeline continues with whisperX + fallbacks.
            logger.warning(
                "[LYRICS] lrclib DB lookup raised (%s) — treating as miss",
                _lrc_db_err,
            )
            lrc, _lrc_meta = None, {"swapped": False, "artist_used": artist_hint, "song_used": song_hint}
        # Auto-correct inverted metadata: when the swap-retry hit, the upload
        # had artist/title swapped (incident 2026-05-24 Viejas Locas /
        # Legalícenla in staging — frontend parser assumes Title_Artist for
        # underscore filenames, but most users name files Artist_Title).
        # Persist the corrected order so the editor renders clean metadata,
        # and update the local hints so Gemini grounding / log lines use the
        # right values for the rest of this run. We use `update_job` (which
        # owns its own short-lived session) instead of reopening scoped_db
        # — same pool-starvation rationale as the lookup above.
        if _lrc_meta.get("swapped"):
            artist_hint = _lrc_meta["artist_used"]
            song_hint = _lrc_meta["song_used"]
            try:
                from jobs import update_job as _update_job
                _update_job(job_id, artist=artist_hint, song_title=song_hint)
                logger.info("[LYRICS] auto-corrected swapped metadata for job %s: "
                            "artist=%r song_title=%r", job_id, artist_hint, song_hint)
            except Exception as e:
                logger.warning("[LYRICS] metadata auto-correction persist failed: %s", e)

        # Track which source the plain text came from (for the gap-driven
        # re-fetch logic further down). lrclib first, fallbacks only if
        # plain is empty. The 2026-05-25 incident showed lrclib can return
        # *incomplete* plain (the canonical Legalícenla case had lrclib's
        # `plain` populated but missing the intro chorus); for that case
        # we don't refire fallbacks here — we let forced_align run, see
        # the gaps in its output, and re-fetch THEN. See the
        # "FA gap-driven re-fetch" block below `if fa_segs:`.
        lyrics_source: str | None = "lrclib" if (lrc and (lrc.get("plain") or "").strip()) else None

        # GENIUS FALLBACK (2026-05-25): when lrclib trae nothing OR trae
        # only `synced` without `plain` and the synced is suspiciously
        # short, try Genius as a second source. Genius's editorial
        # curation produces more complete lyrics for mainstream catalogue
        # (UMG/Sony/Warner releases) than lrclib's community uploads.
        #
        # Genius doesn't ship timestamps — we use it ONLY for text.
        # forced_align will pin the timing against the audio as usual.
        # If Genius also misses, fall through to Gemini (next block)
        # and finally bare Whisper.
        #
        # We patch the lrc dict in place so downstream code (the FA path
        # below, the synced path, the Whisper fallback) doesn't need to
        # know which source we used. The `recovery_source` in the final
        # _emit_segments will record `forced_align` either way; we log
        # the source so post-mortems can trace back.
        if not lrc or not (lrc.get("plain") or "").strip():
            try:
                import genius_fetch
                if genius_fetch.is_enabled():
                    with scoped_db() as _genius_db:
                        genius_text = await asyncio.to_thread(
                            genius_fetch.fetch_genius_plain,
                            artist_hint, song_hint, _genius_db,
                        )
                    if genius_text:
                        logger.info("[LYRICS] genius fallback hit for %r - %r (%d chars)",
                                    artist_hint, song_hint, len(genius_text))
                        if lrc is None:
                            lrc = {}
                        lrc["plain"] = genius_text
                        lyrics_source = "genius"
                        # synced stays None — Genius doesn't ship timestamps.
                        # That's fine: forced_align takes plain text and the
                        # audio, no synced needed.
                    else:
                        logger.info("[LYRICS] genius fallback found nothing for %r - %r",
                                    artist_hint, song_hint)
            except Exception as e:
                logger.warning("[LYRICS] genius fallback raised: %s — continuing without it", e)

        # GEMINI FALLBACK (2026-05-25, 3rd source): if both lrclib and
        # Genius came up empty, try Gemini's grounded search as a third
        # line of defence. Same shape as Genius — text only, no
        # timestamps, forced_align does the timing.
        #
        # Why 3rd and not 2nd: Gemini is an LLM that can hallucinate
        # entire stanzas on obscure tracks (no grounding hit → it
        # invents). Genius's editorial DB has fewer false positives.
        # Both are flagged behind their own kill switches so we can
        # disable independently if either misbehaves.
        #
        # The existing `_fetch_lyrics_via_gemini_search` already caches
        # to LyricsCache (same table Genius and lrclib use, separate
        # keyspace) so subsequent fetches of the same song skip the
        # API call.
        if not lrc or not (lrc.get("plain") or "").strip():
            try:
                from pipeline import _fetch_lyrics_via_gemini_search
                with scoped_db() as _gemini_db:
                    gemini_text = await asyncio.to_thread(
                        _fetch_lyrics_via_gemini_search,
                        artist_hint, song_hint, job_id, _gemini_db,
                    )
                if gemini_text:
                    logger.info("[LYRICS] gemini fallback hit for %r - %r (%d chars)",
                                artist_hint, song_hint, len(gemini_text))
                    if lrc is None:
                        lrc = {}
                    lrc["plain"] = gemini_text
                    lyrics_source = "gemini"
                else:
                    logger.info("[LYRICS] gemini fallback found nothing for %r - %r",
                                artist_hint, song_hint)
            except Exception as e:
                logger.warning("[LYRICS] gemini fallback raised: %s — continuing without it", e)

        # ─────────────────────────────────────────────────────────────────
        # WORLD-CLASS audio-as-truth pipeline (default 2026-05-25).
        #
        # whisperX provides timing (word-level acoustic anchors), the canonical
        # lyrics (lrclib/Genius/Gemini, in that fallback order) provide text.
        # `whisperx_reconcile.reconcile()` merges them: timing from audio,
        # orthography from reference. With phonetic-aware anchoring (see
        # forced_align.wordstamps_to_segments, 2026-05-25), even acoustic
        # mishears like "Le realizan la" → "Legalícenla" anchor to the
        # correct timestamps so the editor never shows "first lyric @ 0:45"
        # when the song actually starts at 0:17.
        #
        # No forced_align. No is_suspiciously_repetitive guard. No hybrid
        # rescue. No gap-driven re-fetch. Those scaffolds existed to detect
        # and compensate for FA's greedy-monotonic cramming of repeated
        # chorus lines into a single audio region — a class of bug that
        # cannot happen when the timing source is per-word acoustic anchors.
        #
        # `AUDIO_AS_TRUTH=0` reverts to the legacy FA-primary pipeline
        # below as an emergency rollback. Plan: validate on staging,
        # delete the legacy branch once stable (~1500 lines come out).
        # ─────────────────────────────────────────────────────────────────
        _wc_enabled = (
            os.environ.get("AUDIO_AS_TRUTH", "1").strip().lower()
            in ("1", "true", "yes", "on", "y", "t")
        )
        if _wc_enabled:
            import whisperx_transcribe as _wx_mod
            if _wx_mod.is_enabled():
                # Canonical text from whichever source brought it in.
                _canonical = ""
                if lrc:
                    _plain = (lrc.get("plain") or "").strip()
                    _synced = lrc.get("synced")
                    if _plain:
                        _canonical = _plain
                    elif _synced:
                        import forced_align as _fa_lrc
                        _canonical = _fa_lrc.lrc_to_plain_text(_synced)

                # Gemini lyrics cleanup (2026-05-26): lrclib has the
                # canonical text but with predictable defects (missing
                # accents, misspellings, wrong chorus repetition counts).
                # Black-box test of Rotor Videos confirmed they use
                # LyricFind's licensed catalog post-acquisition (Dec
                # 2023) which doesn't have these defects. This call
                # closes the gap without licensing cost — Gemini 2.5
                # Flash listens to the audio + the lrclib text and
                # returns corrected lyrics. Gated behind
                # GEMINI_LYRICS_CLEANUP_ENABLED, off by default. Cost:
                # ~$0.01/song. Cache content-addressable; second call
                # on the same audio is free.
                if _canonical:
                    try:
                        from pipeline import _gemini_cleanup_lyrics as _gcl
                        # `_run_transcription_for_job`'s signature uses `title`,
                        # not `song` — passing `song=song` raised NameError on
                        # every call, the cleanup silently fell back to lrclib
                        # raw, and the feature flag was effectively a no-op in
                        # prod (incident 2026-05-26: every transcription log
                        # showed "[WC] Gemini cleanup raised: name 'song' is
                        # not defined — using lrclib raw"). _gcl's kwarg stays
                        # `song=` because that's the public contract documented
                        # in pipeline.py:3727; we just feed it the right local.
                        _cleaned = await asyncio.to_thread(
                            _gcl, tmp_path, _canonical,
                            artist=artist, song=title,
                        )
                    except Exception as _e:
                        logger.warning("[WC] Gemini cleanup raised: %s — using lrclib raw", _e)
                        _cleaned = None
                    if _cleaned:
                        _canonical = _cleaned

                # whisperX over the vocal stem. `lyrics_hint` biases the
                # model toward the canonical lexicon (kills the "¡Karol!"
                # stuck-phoneme and similar mishears in well-known regions).
                await _step("transcribe.transcribe_word", 50)
                _aa = await _get_align_audio()
                # Divergent live/extended detection (2026-06-04, LIVE_NO_HINT_ENABLED,
                # default off). When the upload is much longer than the lrclib record
                # (the documented diff>60s "live / extended" case, see ~4082), the
                # lrclib STUDIO text poisons whisperX's initial_prompt — the model
                # parrots the prompt in studio order and scrambles the live (lab: Coti
                # "Nada" live → offset +75s, first verse at 1:29 instead of 0:39) — AND
                # the downstream reconcile/scaffold drift against the wrong structure.
                # Lab (7 songs, 3 Rotor ground-truths): clean NO-hint whisperX matches
                # Rotor's own timing (median 0.03-0.8 s); Rotor itself transcribes blind
                # the same way. So for divergent audio: drop the hint + emit the clean
                # transcription raw, skipping the canonical cascade. Reversible; falsy
                # when we can't measure (missing lrclib duration) so default behavior
                # is untouched.
                _lrc_dur = (lrc or {}).get("duration") if isinstance(lrc, dict) else None
                _live_no_hint = bool(
                    os.environ.get("LIVE_NO_HINT_ENABLED", "0").strip().lower()
                    in ("1", "true", "yes", "on")
                    and _audio_dur_for_lrc and _lrc_dur
                    and (float(_audio_dur_for_lrc) - float(_lrc_dur)) > 60.0
                )
                if _live_no_hint:
                    logger.info(
                        "[WC] divergent live/extended (audio %.0fs vs lrclib %.0fs, "
                        "diff %.0fs) — clean whisperX, no hint, raw emit",
                        float(_audio_dur_for_lrc), float(_lrc_dur),
                        float(_audio_dur_for_lrc) - float(_lrc_dur),
                    )
                # Phase 2 (WHISPERX_NO_HINT_ALWAYS, default off): drop the lrclib
                # hint for EVERY song, not just divergent lives. The hint
                # (initial_prompt) scrambles whisperX whenever lrclib diverges from
                # the audio — which happens on some STUDIO recordings too (lab: "Me
                # Gustas Mucho", "De A Ratitos" started mid-song WITH the hint, clean
                # WITHOUT it). Clean (no-hint) whisperX gives Rotor-level timing, then
                # the reconcile step below restores lrclib's correct text over that
                # timing ("lrclib's clean text = best of both" — whisperx_reconcile
                # docstring), which ALSO repairs rare-word mishears for free
                # (Legalícenla: clean wx hears "Le realicen la" → reconcile emits the
                # canonical "Legalícenla", beating Rotor's own blind "Legaliza en la").
                # Non-divergent songs keep going through reconcile/cascade; only the
                # divergent-live raw emit above is gated on _live_no_hint.
                _no_hint_always = (
                    os.environ.get("WHISPERX_NO_HINT_ALWAYS", "0").strip().lower()
                    in ("1", "true", "yes", "on")
                )
                _drop_hint = _live_no_hint or _no_hint_always
                if _no_hint_always and not _live_no_hint:
                    logger.info("[WC] WHISPERX_NO_HINT_ALWAYS — clean whisperX, reconcile restores canonical text")
                try:
                    _wx_segs = await asyncio.to_thread(
                        _wx_mod.transcribe_whisperx, _aa, lang,
                        None if _drop_hint else (_canonical or None),
                    )
                except Exception as e:
                    logger.warning("[WC] whisperX raised: %s — falling through to legacy", e)
                    _wx_segs = None

                if _wx_segs:
                    # Generic hallucination filter (mega-segment, fuzzy
                    # intra-loops, ¡Karol!×174 family). Pure structural
                    # check on whisperX output — runs regardless of
                    # canonical presence.
                    from pipeline import _filter_whisper_hallucinations as _fwh
                    _wx_segs, _dropped = _fwh(_wx_segs)
                    if _dropped:
                        logger.warning("[WC] dropped %d whisperX hallucination phrase(s)", _dropped)

                if _wx_segs:
                    from timing_sources import (
                        WHISPERX_RECONCILED as _WC_WX_REC,
                        WHISPERX as _WC_WX,
                    )
                    # Divergent live/extended: the clean (no-hint) transcription IS
                    # the truth — its order/timing track the actual performance (lab:
                    # Rotor-level). The canonical cascade below would drift it against
                    # the studio structure, so emit raw and return.
                    if _live_no_hint:
                        logger.info(
                            "[WC] divergent live — emitting clean whisperX raw "
                            "(%d segs, Rotor-level timing)", len(_wx_segs))
                        # LLM line-segmentation (LLM_SEGMENT_ENABLED, default off):
                        # the no-hint whisperX has Rotor-level TIMING but native
                        # VAD LINE breaks (merges/splits at the wrong words, e.g.
                        # "noticia No"). Gemini re-groups the live's OWN words into
                        # clean phrase lines + fixes orthography, mapped back to the
                        # exact whisperX timing — no reference template to drift
                        # (reconcile aborts here). Self-declining → keeps _wx_segs on
                        # any failure. Lab on "Nada Fue Un Error (En Vivo)": matches
                        # Rotor line-for-line, timing byte-identical.
                        from pipeline import (
                            _llm_segment_words as _llm_seg,
                            _recover_gap_lyrics as _recover_gap,
                        )
                        # Offloaded to a thread: these do blocking file I/O,
                        # librosa decode + a Gemini call (up to ~90 s). Running
                        # them inline would freeze the API event loop for the
                        # whole job (starves /usage, /jobs — dashboard freeze),
                        # so use to_thread like every other heavy step here.
                        _wx_segs = await asyncio.to_thread(
                            _llm_seg, _wx_segs, audio_path=_aa,
                        )
                        # Gap-recovery (GAP_RECOVERY_ENABLED, default off): whisperX
                        # drops lyrics in loud live passages, leaving multi-second
                        # holes (lab "Nada Fue Un Error En Vivo": an 84 s hole where
                        # the chorus keeps going + the outro). Re-transcribe a SHORT
                        # bounded clip at each hole's first voiced run → recovers the
                        # real line without the long-clip hallucination loop. Runs
                        # AFTER segmentation (already-clean lines) + self-declines.
                        _wx_segs = await asyncio.to_thread(
                            _recover_gap, _wx_segs,
                            audio_path=_aa, canonical=_canonical,
                        )
                        return _emit_segments(
                            _wx_segs, _WC_WX, reference_lyrics=_canonical,
                        )
                    # Reconcile when we have canonical text; emit raw otherwise.
                    if _canonical:
                        import whisperx_reconcile as _wxr
                        # Audio-as-truth (LINE_TEXT_CORRECT_ENABLED): keep the
                        # raw transcription's OWN segments + timing (incl. ad-lib
                        # "uh") and only swap each line's TEXT to the best
                        # reference line. Avoids reconcile's word-rebucketing,
                        # which scatters chorus words across a missing ad-lib gap.
                        #
                        # GATED to lyrics_source == "gemini" — i.e. UNKNOWN songs
                        # where lrclib + Genius both missed and reconcile would
                        # smear the ad-lib. For KNOWN songs (lrclib/genius) the
                        # whisperX-reconcile path is strictly better (word-level
                        # timing + clean line structure); this Whisper-1 base
                        # would merge lines and mistime them (regressed "La
                        # Leyenda del Hada y el Mago", an lrclib song). So those
                        # fall through to reconcile, exactly as in production.
                        if (lyrics_source == "gemini"
                                and os.environ.get("LINE_TEXT_CORRECT_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")):
                            # Base = Whisper-1 on the RAW mix (`audio_path`), NOT WhisperX
                            # on the stem. Measured on "No Hay Santos": Whisper-1-raw
                            # captures sustained ad-libs as "uh"/"oh" and segments into
                            # clean full lines; WhisperX fragments and its VAD drops the
                            # ad-lib (recall 79% vs 74%, max-gap 13s vs 19s). text_correct
                            # then only swaps each line's TEXT to the best reference line,
                            # leaving ad-lib lines (no match) as-heard. pipeline.transcribe
                            # carries the single-pass + collapse-fallback hardening.
                            import pipeline as _pl
                            _base = await asyncio.to_thread(
                                _pl.transcribe, audio_path, language=(lang or None),
                                job_id=job_id, return_words=True,
                            )
                            # Sustained ad-libs ("uh uh uh") that Whisper forced into
                            # words (e.g. a 21 s block heard as "¿Para qué? ¿Para qué?")
                            # → relabel to "Uh" BEFORE text-correct so they don't get
                            # matched onto a chorus line. Long-segment + few-words = ad-lib.
                            _base = _wxr.relabel_long_adlibs(_base)
                            # ORDER MATTERS: split merged lines at musical pauses
                            # (word-gaps) BEFORE correcting text. Whisper merges e.g.
                            # "Tomás del miedo tu don, frágil espejo de vos" into one
                            # segment; _post_reconcile_cleanup splits it at the internal
                            # gap so text_correct can name BOTH lines. This is how ROTOR
                            # separates them. (return_words=True can roughen base TEXT,
                            # but text_correct overwrites it from the reference anyway.)
                            _base = _pl._post_reconcile_cleanup(_base)
                            _corrected = _wxr.text_correct_segments(_base, _canonical)
                            # Whisper sometimes anchors the first line at 0:00 despite a
                            # long instrumental intro (operator had to click the editor's
                            # "corrección automática"). Relocate it to the real vocal
                            # onset automatically. Pure segment-cadence heuristic; no-ops
                            # when the first line is already placed sanely.
                            _corrected, _ = _pl._fix_lrc_first_line_at_zero(_corrected)
                            logger.info(
                                "[WC] line-text-correct on Whisper-1 base (%d segs, canonical=%s) — audio-as-truth path",
                                len(_corrected),
                                "lrclib" if lyrics_source == "lrclib" else (lyrics_source or "unknown"),
                            )
                            return _emit_segments(
                                _corrected, _WC_WX_REC, reference_lyrics=_canonical,
                            )
                        _reconciled = _wxr.reconcile(_wx_segs, _canonical)
                        if _reconciled:
                            logger.info("[WC] whisperX reconciled (%d/%d lines, canonical=%s) — audio-as-truth path",
                                        len(_reconciled),
                                        len([l for l in _canonical.splitlines() if l.strip()]),
                                        "lrclib" if lyrics_source == "lrclib"
                                        else (lyrics_source or "unknown"))
                            # Correct timing of segments displaced after an
                            # invisible adlib block BEFORE stretch-trim fills
                            # the gap (post_reconcile pass 3). The gap is
                            # visible here (raw reconciler end-times); after
                            # stretch-trim it closes to ~80ms.
                            # Runs in a thread: makes blocking librosa + OpenAI
                            # calls that would otherwise stall the event loop.
                            logger.info(
                                "[WC] gap-cluster: invoking on %d segs, audio=%s",
                                len(_reconciled), _aa,
                            )
                            try:
                                from whisperx_transcribe import _correct_large_gap_cluster as _clgc
                                _reconciled = await asyncio.to_thread(
                                    _clgc, _reconciled, _aa,
                                )
                            except Exception as _clgc_err:
                                logger.warning("[WC] gap-cluster FAILED: %s", _clgc_err)
                            from pipeline import _post_reconcile_cleanup as _prc
                            _reconciled = _prc(_reconciled)
                            return _emit_segments(
                                _reconciled, _WC_WX_REC,
                                reference_lyrics=_canonical,
                            )
                        # ── Stage 3 (2026-06-01): VAD-validated synced scaffold ──
                        # Reconcile aborted. BEFORE the fragile fallbacks below
                        # (whisper_align → Whisper-1 outro hallucination on
                        # guitar solos; forced_align/Cureau → 10-line pile-up on
                        # the post-solo verse, the operator's "se ilumina donde
                        # no canta"), try lrclib's HUMAN karaoke timing anchored
                        # to this recording (offset from whisperX's first sung
                        # word) and VALIDATED against where the stem actually has
                        # voice (energy VAD) + a duration span gate. When the
                        # offset-corrected lines land on singing it gives clean,
                        # well-distributed lines with NO pile-up and NO outro
                        # hallucination (lab: Rata Blanca 47 lines). It declines
                        # (falls through) when the synced version doesn't match
                        # THIS recording (cumbia +73s overshoot, Soda live
                        # arrangement). Gated + reversible.
                        _synced_sc = (
                            (lrc or {}).get("synced") if isinstance(lrc, dict) else None
                        )
                        if (
                            # Default ON 2026-06-03: lab-validated on a 7-song
                            # Argentine-rock batch (Me Gustas Mucho, Una Vez Mas,
                            # Rata Blanca, Intoxicados, …) — every one has a long
                            # instrumental intro (13–55s) and the lrclib synced
                            # timing matches the real vocal onset within ~1s
                            # (frac_in_voice 82–100%). The OLD path places lyrics
                            # during the intro → "destiempo al principio" (Agus
                            # prod report). The scaffold anchors to the real onset.
                            # SELF-DECLINING: build_synced_scaffold returns None
                            # (→ caller keeps the old fallbacks) when the synced
                            # version doesn't match THIS recording (covers/lives),
                            # so a mismatch is a no-op, not a regression. REQUIRES
                            # the vocal stem (VOCAL_SEP on); on the bare mix the
                            # VAD anchors to the band, so keep VOCAL_SEP enabled.
                            # Override with ANCHOR_SCAFFOLD_ENABLED=0 to disable.
                            os.environ.get("ANCHOR_SCAFFOLD_ENABLED", "1")
                            .strip().lower() in ("1", "true", "yes", "on")
                            and _synced_sc
                        ):
                            try:
                                import anchor_align as _anchor
                                import lrclib_aligner as _lca_s
                                _sc_pairs = _lca_s._parse_lrc_to_line_times(_synced_sc)
                                _sc_regions = _anchor.vocal_regions(_aa)
                                _sc_segs, _sc_meta = _anchor.build_synced_scaffold(
                                    _sc_pairs, _wx_segs, _audio_dur_for_lrc,
                                    vocal_regions=_sc_regions,
                                )
                                if _sc_segs:
                                    from timing_sources import (
                                        SYNCED_SCAFFOLD as _WC_SS,
                                    )
                                    logger.info(
                                        "[WC] synced-scaffold: %d lines, "
                                        "offset=%+.1fs, voice=%.0f%% — human "
                                        "karaoke timing anchored to this audio",
                                        len(_sc_segs),
                                        _sc_meta.get("offset", 0.0),
                                        max(0.0, _sc_meta.get("frac_in_voice", 0.0)) * 100,
                                    )
                                    return _emit_segments(
                                        _sc_segs, _WC_SS,
                                        reference_lyrics=_canonical,
                                    )
                                logger.info(
                                    "[WC] synced-scaffold declined (%s) — "
                                    "continuing cascade",
                                    _sc_meta.get("reason"),
                                )
                            except Exception as _e_sc:
                                logger.warning(
                                    "[WC] synced-scaffold raised: %s — "
                                    "continuing cascade", _e_sc,
                                )
                        # Reconcile aborted = this recording's structure DIVERGES
                        # from lrclib's studio lyric (live/extended/cover). The
                        # forced_align / whisper_align fallbacks below force the
                        # studio LINE STRUCTURE onto it, so the live's extra content
                        # (repeated verses, ad-libs) is dropped — operator on "Nada
                        # Fue Un Error En Vivo": forced_align placed only the 49
                        # studio lines over 366s and left a 2-minute hole where the
                        # live keeps singing. Before those, try LLM line-segmentation
                        # of whisperX's OWN words: it captures what the recording
                        # ACTUALLY sings, with Rotor-level timing + clean phrase
                        # lines. Self-declining (flag off / Gemini fail / gates) →
                        # returns _wx_segs unchanged → cascade continues to FA.
                        # Gated by LLM_SEGMENT_ENABLED. (The _live_no_hint raw path
                        # above also runs it, for lives detected by duration.)
                        from pipeline import (
                            _llm_segment_words as _llm_seg2,
                            _recover_gap_lyrics as _recover_gap2,
                            _recording_diverges as _rec_diverges,
                            _env_float as _env_float_p,
                        )
                        # Reconcile aborting does NOT prove the recording diverges
                        # from the studio lyric — plain whisperX MISHEARS trip the
                        # same drift/coverage abort (incident "Viejas Locas — 638":
                        # FA below recovered 19/19 canonical lines whisperX mangled).
                        # LLM-segment's gates only compare against whisperX, never
                        # _canonical, so it would ship those mishears and preempt FA.
                        # Only let it win when the recording actually sings MORE than
                        # the studio lyric (live/extended: repeated verses, ad-libs,
                        # long outros — the "Nada" case). Otherwise fall through so
                        # whisper-align/FA recover the canonical text.
                        _div_ratio = _env_float_p(
                            "LLM_SEGMENT_DIVERGENCE_RATIO", 1.25
                        )
                        _is_divergent = _rec_diverges(
                            _wx_segs, _canonical, _div_ratio
                        )
                        _llm_segs = (
                            await asyncio.to_thread(
                                _llm_seg2, _wx_segs, audio_path=_aa,
                            )
                            if _is_divergent else _wx_segs
                        )
                        if _llm_segs is not _wx_segs and len(_llm_segs) >= 2:
                            # Same gap-recovery as the no-hint path: fill the
                            # multi-second holes whisperX leaves in loud lives with
                            # a short bounded re-transcription (default off).
                            _llm_segs = await asyncio.to_thread(
                                _recover_gap2, _llm_segs,
                                audio_path=_aa, canonical=_canonical,
                            )
                            logger.info(
                                "[WC] reconcile aborted + recording diverges from "
                                "studio lyric → LLM line-segmentation of whisperX "
                                "(%d lines); FA would force the wrong studio "
                                "structure", len(_llm_segs))
                            return _emit_segments(
                                _llm_segs, _WC_WX, reference_lyrics=_canonical,
                            )
                        # Otherwise: try forced_align as a fallback before
                        # falling all the way back to whisperX raw — FA has
                        # a different anchoring strategy (greedy monotonic
                        # alignment over the full audio against the full
                        # canonical text) and recovers cases where whisperX
                        # mishears confuse the word-by-word reconciler.
                        #
                        # INCIDENT 2026-05-26 "Viejas Locas — 638": whisperX
                        # mishears like "780465" / "738-0465" instead of
                        # "638" plus intra-segment duplications ("y empecé
                        # y empecé") tripped reconcile's drift abort.
                        # Audio-as-truth fell back to whisperX raw and the
                        # editor saw only 8/19 canonical lines. FA on the
                        # same audio recovered 19/19 — operator reported
                        # "le faltan lyrics en bastantes lineas".
                        # First (world-class) — Whisper-1 word-level
                        # alignment against the cleaned canonical. Whisper
                        # hears every word, returns ±0.5 s timestamps; DP
                        # picks the best mapping from each cleaned token
                        # to a Whisper word; each line's start = first
                        # matched token's Whisper time. Empirically
                        # 27/27 lines anchored on "638" with avg Δ 2.6 s
                        # vs lrclib synced ground truth (probe 2026-05-26).
                        # Linear interpolation between lrclib synced
                        # anchors (cleanup_anchored, below) only kicks in
                        # if this returns None — acoustic anchors strictly
                        # dominate lexical anchors + interpolation. Cost
                        # ~$0.018/song. Gated on cleanup-expanded
                        # canonical AND OPENAI_API_KEY available.
                        _canonical_lines = [
                            ln.strip() for ln in (_canonical or "").splitlines()
                            if ln.strip()
                        ]
                        if (
                            _cleaned
                            and _canonical_lines
                            and os.environ.get("OPENAI_API_KEY")
                        ):
                            try:
                                from lyrics_whisper_align import (
                                    whisper_word_align as _wwa,
                                )
                                # Stage 0 (2026-06-01): feed Whisper-1 the
                                # ISOLATED VOCAL STEM (`_aa`) instead of the full
                                # mix (`tmp_path`). On guitar-heavy songs the mix
                                # makes Whisper-1 hallucinate the instrumental
                                # outro ("Oh oh no/Yeah", "Amara org") and drift
                                # the end timing — this is the ONLY transcription
                                # path that read the mix (whisperX/forced_align
                                # already use `_aa`). The stem has no guitar so it
                                # stays clean. `_aa` already falls back to
                                # `tmp_path` when demucs is unavailable, so this is
                                # safe-by-design. Gated + reversible via env;
                                # validate in staging (re-run Rata Blanca → no
                                # "Oh oh no/Yeah", timing tightens) before prod.
                                _wa_audio = (
                                    _aa
                                    if os.environ.get(
                                        "WHISPER_ALIGN_USE_STEM", "0"
                                    ).strip().lower() in ("1", "true", "yes", "on")
                                    else tmp_path
                                )
                                _wa_segs = await asyncio.to_thread(
                                    _wwa, _wa_audio, _canonical_lines,
                                    language=lang or "es",
                                    job_id=job_id,
                                )
                                if _wa_segs:
                                    from timing_sources import (
                                        WHISPER_ALIGN as _WC_WA,
                                    )
                                    logger.info(
                                        "[WC] whisper-align path: %d segs "
                                        "(cleaned=%d) — acoustic alignment "
                                        "via Whisper-1 word timestamps",
                                        len(_wa_segs), len(_canonical_lines),
                                    )
                                    return _emit_segments(
                                        _wa_segs, _WC_WA,
                                        reference_lyrics=_canonical,
                                    )
                            except Exception as _e_wa:
                                logger.warning(
                                    "[WC] whisper-align path raised: %s — "
                                    "falling through to cleanup_anchored",
                                    _e_wa,
                                )

                        # Second — when cleanup expanded the canonical beyond
                        # what lrclib synced has, try the cleanup-anchored
                        # path BEFORE forced_align. Rationale: FA's
                        # `wordstamps_to_segments` clamps unmatchable extra
                        # lines to the end of the word stream, producing
                        # pile-up at a single timestamp (incident 2026-05-26
                        # "638" operator report: 5 lines stuck at 1:15.5).
                        # The cleanup-anchored path preserves lrclib synced
                        # timestamps for matched lines and linearly
                        # interpolates new lines between adjacent anchors,
                        # avoiding the pile-up. Gated tightly: only when
                        # cleanup actually expanded the canonical AND synced
                        # is available. See
                        # `/Users/tomi/.claude/plans/image-24-la-letra-robust-moonbeam.md`.
                        _synced_for_anchor: str | None = (
                            (lrc or {}).get("synced") if isinstance(lrc, dict) else None
                        )
                        if (
                            _cleaned  # cleanup ran and produced output
                            and _synced_for_anchor
                            and len(_canonical_lines) > 0
                        ):
                            try:
                                import lrclib_aligner as _lca_ca
                                _ca_pairs = _lca_ca._parse_lrc_to_line_times(_synced_for_anchor)
                                if (
                                    _ca_pairs
                                    and len(_canonical_lines) > len(_ca_pairs)
                                ):
                                    from lyrics_cleanup_alignment import (
                                        align_cleaned_against_synced as _ca_align,
                                    )
                                    from pipeline import _audio_duration as _ca_dur
                                    _ca_audio_dur = _ca_dur(tmp_path)
                                    if _ca_audio_dur and _ca_audio_dur > 0:
                                        _ca_segs = _ca_align(
                                            _canonical_lines, _ca_pairs, _ca_audio_dur,
                                        )
                                        if _ca_segs:
                                            from timing_sources import (
                                                CLEANUP_ANCHORED as _WC_CA,
                                            )
                                            logger.info(
                                                "[WC] cleanup-anchored path: %d segs "
                                                "(cleaned=%d, synced=%d, audio=%.1fs) "
                                                "— skipping FA pile-up",
                                                len(_ca_segs),
                                                len(_canonical_lines),
                                                len(_ca_pairs),
                                                _ca_audio_dur,
                                            )
                                            return _emit_segments(
                                                _ca_segs, _WC_CA,
                                                reference_lyrics=_canonical,
                                            )
                            except Exception as _e_ca:
                                logger.warning(
                                    "[WC] cleanup-anchored path raised: %s — "
                                    "falling through to forced_align",
                                    _e_ca,
                                )

                        logger.info("[WC] reconcile aborted — trying forced_align fallback before whisperX raw")
                        _fa_segs = None
                        try:
                            import forced_align as _fa_fb
                            _fa_segs = await asyncio.to_thread(
                                _fa_fb.forced_align_lyrics, _aa, _canonical,
                            )
                        except Exception as e:
                            logger.warning("[WC] forced_align fallback raised: %s — emitting whisperX raw", e)
                        if _fa_segs:
                            from timing_sources import FORCED_ALIGN as _WC_FA
                            logger.info(
                                "[WC] forced_align fallback succeeded (%d/%d lines) — emitting FA segments",
                                len(_fa_segs),
                                len([l for l in _canonical.splitlines() if l.strip()]),
                            )
                            return _emit_segments(
                                _fa_segs, _WC_FA,
                                reference_lyrics=_canonical,
                            )
                        # FA also failed. Last canonical-aware attempt
                        # before whisperX raw: emit lrclib SYNCED
                        # timestamps directly, anchored to whisperX's
                        # first detected word so a global offset (e.g.
                        # "Cosas Mías" 2026-05-22) gets normalised.
                        #
                        # INCIDENT 2026-05-26 (issue #357 — Sin Gamulán
                        # / Mujer Amante): cureau crashes with
                        # `Expected 2D or 3D tensor … got [1, 2, 0]` on
                        # lyrics with extreme line repetition (>40%
                        # duplicates). FA fallback can't help, and
                        # lrclib_aligner's per-line cursor-based
                        # matching also breaks on chorus repeats (PR
                        # #362, reverted: ~50 s avg timing error).
                        #
                        # The trick: lrclib synced has the correct
                        # RELATIVE timing for every line in song order,
                        # so we just need ONE good audio anchor to fix
                        # any global offset. whisperX's first detected
                        # word + the first synced line gives us that
                        # anchor essentially for free — empirically
                        # within 1 s of truth on both Sin Gamulán and
                        # Mujer Amante. Drops avg error from ~50 s
                        # (linear interp) to <1 s.
                        _synced_hint: str | None = (
                            (lrc or {}).get("synced") if isinstance(lrc, dict) else None
                        )
                        _sync_segs: list[dict] = []
                        if _synced_hint:
                            try:
                                import lrclib_aligner as _lca
                                _pairs = _lca._parse_lrc_to_line_times(_synced_hint)
                                # Anchor offset: whisperX first word vs first synced line.
                                _first_wx_t: float | None = None
                                for _s in _wx_segs:
                                    for _w in _s.get("words") or []:
                                        if isinstance(_w, dict) and "start" in _w:
                                            _first_wx_t = float(_w["start"])
                                            break
                                    if _first_wx_t is not None:
                                        break
                                if _pairs:
                                    # Decide offset + trust. When the audio
                                    # duration matches lrclib's, the synced
                                    # timeline is for THIS exact version → use
                                    # offset 0 and TRUST it (no amber review
                                    # spam). Otherwise anchor whisperX's first
                                    # word to the first synced line. This fixes
                                    # the first-word anchor misfiring when
                                    # whisperX skips a soft intro ad-lib (its
                                    # "first word" is then a LATER line → a huge
                                    # bogus offset, e.g. Enanitos "Mi Primer Día
                                    # Sin Ti": +36 s with every line amber).
                                    _lrc_dur_val = (
                                        (lrc or {}).get("duration")
                                        if isinstance(lrc, dict) else None
                                    )
                                    _offset, _trust = _lca.synced_offset_decision(
                                        _audio_dur_for_lrc, _lrc_dur_val,
                                        _first_wx_t, _pairs[0][0],
                                    )
                                    # Only ship a synced timeline when we have a
                                    # real basis: durations match (trust) OR
                                    # whisperX gave an anchor. Otherwise leave
                                    # _sync_segs empty → fall through to whisperX
                                    # raw (this audio's own word timing).
                                    if _trust or _first_wx_t is not None:
                                        # Build segments: each line spans up
                                        # to the next line's start − 50 ms.
                                        for _i, (_t, _txt) in enumerate(_pairs):
                                            _s_t = round(_t + _offset, 2)
                                            if _i + 1 < len(_pairs):
                                                _e_t = round(_pairs[_i + 1][0] + _offset - 0.05, 2)
                                            else:
                                                _e_t = round(_s_t + 3.0, 2)
                                            if _e_t <= _s_t:
                                                _e_t = _s_t + 0.5
                                            _seg = {
                                                "start": max(0.0, _s_t),
                                                "end": max(_s_t + 0.1, _e_t),
                                                "text": _txt,
                                            }
                                            # Trusted (duration-matched) synced is
                                            # as reliable as the old fast-path —
                                            # don't flag every line amber. Only the
                                            # estimated-offset case needs review.
                                            if not _trust:
                                                _seg["review"] = True
                                            _sync_segs.append(_seg)
                                        logger.info(
                                            "[WC] synced direct fallback: %d segs, offset=%+.2fs, trust=%s "
                                            "(audio=%.1fs lrclib=%s; first whisperX word @%s vs synced @%.2fs)",
                                            len(_sync_segs), _offset, _trust,
                                            _audio_dur_for_lrc if _audio_dur_for_lrc is not None else -1.0,
                                            _lrc_dur_val,
                                            ("%.2f" % _first_wx_t) if _first_wx_t is not None else "n/a",
                                            _pairs[0][0],
                                        )
                            except Exception as e:
                                logger.warning("[WC] synced direct fallback raised: %s — emitting whisperX raw", e)
                        if _sync_segs:
                            # Stage 1 (2026-06-01): duration sanity before we ship
                            # an lrclib SYNCED timeline. The synced lyrics can
                            # belong to a LONGER / foreign edit of the song (cumbia
                            # "Luz de Día": synced runs to 248 s on a 169 s audio;
                            # the "Cosas Mías" 35 s-off incident) — emitting it
                            # overshoots the recording. `span_gate` rejects that so
                            # we fall through to whisperX raw (THIS audio's own
                            # word timing) instead of shipping a foreign timeline.
                            # Gated + reversible; only the egregious-overshoot case
                            # is rejected (a real instrumental outro still passes).
                            _span_ok = True
                            if os.environ.get(
                                "SPAN_GATE_ENABLED", "0"
                            ).strip().lower() in ("1", "true", "yes", "on"):
                                from timing_confidence import span_gate as _span_gate
                                _sv = _span_gate(_sync_segs, _audio_dur_for_lrc)
                                _span_ok = _sv.ok
                                if not _sv.ok:
                                    logger.warning(
                                        "[WC] span_gate rejected synced timeline "
                                        "(%s) — falling through to whisperX raw",
                                        _sv.reason,
                                    )
                            if _span_ok:
                                from timing_sources import WHISPERX_LRCLIB as _WC_WXL
                                return _emit_segments(
                                    _sync_segs, _WC_WXL,
                                    reference_lyrics=_canonical,
                                )
                        logger.info("[WC] no synced hint — emitting whisperX raw with mishear text (operator edits)")
                    from pipeline import _post_reconcile_cleanup as _prc
                    _wx_segs = _prc(_wx_segs)
                    return _emit_segments(
                        _wx_segs, _WC_WX,
                        reference_lyrics=_canonical,
                    )
                # whisperX produced nothing usable — fall through to legacy
                # (whisper-1 last-resort path further down).
                logger.info("[WC] whisperX returned no segments — falling through to legacy")
            else:
                logger.info("[WC] WHISPERX_ENABLED off — using legacy FA path")

        # ─────────────────────────────────────────────────────────────────
        # LEGACY FA-primary pipeline (below). Kept as a safety net during
        # validation of the world-class path above. Slated for removal
        # once the new path proves stable in prod (~1500 lines come out).
        # ─────────────────────────────────────────────────────────────────
        if lrc:
            synced = lrc.get("synced")
            plain = lrc.get("plain") or ""
            lrc_dur = lrc.get("duration")

            # Forced alignment (preferred when enabled): align the KNOWN
            # lyrics to THIS audio at ±50ms instead of trusting lrclib's
            # community timestamps (often drifted / missing lines — the
            # Intoxicados case) or Whisper's loose timing. Falls back to
            # the existing synced/Whisper logic on any failure.
            #
            # PR #299 WARM-START (2026-05-24, rev 2026-05-25):
            #
            # **REV: warm-start uses whisperX, NOT Whisper-1.**
            #
            # The original PR #299 launched Whisper-1 in parallel to FA
            # to cut worst-case wallclock from ~16 min to ~6 min when
            # Replicate degraded. That worked BUT quality dropped
            # noticeably: when FA fails (e.g. `[1, 2, 0]` bug on
            # certain audios), Whisper-1 standalone hallucinates
            # ("sepa que sepa que sepa", repeticiones, líneas
            # larguísimas) because it doesn't reconcile against the
            # lrclib canonical text.
            #
            # World-class fix: use **whisperX** as the warm-start.
            # whisperX gives word-level stamps that we then re-bucket
            # against the lrclib plain text via
            # `whisperx_reconcile.reconcile()` — that's timing from
            # audio + text from reference = the same audio-as-truth
            # promise PR-G made. NOT a degraded fallback.
            #
            # Cost trade-off: whisperX (~75-180s, $0.005) is more
            # expensive than Whisper-1 (~15-30s, $0.003) but produces
            # WorldClass output. Worth the ~$0.30/mo extra for UMG-grade
            # quality vs hallucinated text.
            #
            # If whisperX ALSO fails (rare — different Replicate model
            # than cureau, less likely to flake together), the original
            # whisper-1 fallback path below kicks in as the true
            # last-resort.
            import forced_align
            import whisperx_transcribe
            if forced_align.is_enabled():
                fa_text = plain or forced_align.lrc_to_plain_text(synced)
                if fa_text and plain and whisperx_transcribe.is_enabled():
                    await _step("transcribe.align", 55)
                    _aa = await _get_align_audio()

                    # Warm-start whisperX in parallel with FA. Both go
                    # through Replicate but use DIFFERENT models
                    # (cureau/force-align vs victor-upmeet/whisperx),
                    # so a model-specific outage in one doesn't usually
                    # affect the other. We pass the SAME stem path so
                    # both align against the isolated vocals.
                    async def _warm_whisperx():
                        # Pass fa_text as initial_prompt — biases whisperX
                        # towards the canonical lyrics, kills the
                        # "Le realizan la" → "Legalícenla" mishear.
                        return await asyncio.to_thread(
                            whisperx_transcribe.transcribe_whisperx, _aa, lang,
                            fa_text,
                        )
                    wx_warm_task = asyncio.create_task(_warm_whisperx())

                    fa_segs = None
                    try:
                        fa_segs = await asyncio.to_thread(
                            forced_align.forced_align_lyrics, _aa, fa_text,
                        )
                    except Exception as e:
                        logger.warning("[LYRICS] forced_align raised: %s — using warm-start whisperX", e)

                    if fa_segs:
                        logger.info("[LYRICS] forced alignment used (%s lines, lrclib text) for %r - %r", len(fa_segs), artist_hint, song_hint)

                        # FA GAP-DRIVEN RE-FETCH (2026-05-25, Option C):
                        # the user asked the right question: "if lrclib
                        # always wins when it has SOMETHING, the trilogy
                        # of sources only matters when lrclib is fully
                        # empty — what about when lrclib has incomplete
                        # text?" That's the original Legalícenla
                        # incident: lrclib's `plain` was non-empty but
                        # MISSED the intro chorus.
                        #
                        # Strategy: let lrclib win the first round. Then
                        # measure forced_align's output. If the FA result
                        # has > GAP_REFETCH_THRESHOLD_S of suspicious
                        # gaps (intro late + internal gaps), try Genius
                        # and Gemini for a MORE COMPLETE version, re-run
                        # FA, and use whichever has fewer gaps. Pays for
                        # the extra round-trip (+ ~$0.007 FA + 75 s)
                        # ONLY when there's a real problem to fix.
                        #
                        # No-op when:
                        #   - The lyrics_source is already a fallback
                        #     (we already tried Genius/Gemini once and
                        #     it didn't help — re-trying would just spin)
                        #   - The original FA result has minimal gaps
                        #     (lrclib was already complete enough)
                        GAP_REFETCH_THRESHOLD_S = 30.0
                        _gap_threshold_inner = 15.0
                        def _gap_score(segs) -> float:
                            """Sum of gaps > threshold + intro-late penalty.
                            INCIDENT 2026-05-25 (post-PR #312 audit): the
                            original only handled positive gaps. When FA
                            emits overlapping segments (`next.start <
                            prev.end`, an aligner artefact common with
                            repeated chorus lines), `gap` is NEGATIVE and
                            was silently skipped. That meant a song with
                            lots of overlaps scored 0 — re-fetch never
                            fired even when forced_align was clearly
                            confused. Now we treat overlaps > 500 ms as
                            equally suspicious as a gap of the same size:
                            both signal "the aligner is in trouble with
                            this lyric source"."""
                            if not segs:
                                return 0.0
                            s = sorted(segs, key=lambda x: float(x.get("start") or 0))
                            score = 0.0
                            first = float(s[0].get("start") or 0)
                            if first > _gap_threshold_inner:
                                score += first
                            for p, n in zip(s, s[1:]):
                                gap = float(n.get("start") or 0) - float(p.get("end") or 0)
                                if gap > _gap_threshold_inner:
                                    score += gap
                                elif gap < -0.5:
                                    # Overlap > 500 ms → signal of aligner stress.
                                    score += abs(gap)
                            return score

                        gap_score_initial = _gap_score(fa_segs)
                        if (gap_score_initial > GAP_REFETCH_THRESHOLD_S
                                and lyrics_source == "lrclib"):
                            logger.info(
                                "[LYRICS] FA gap_score=%.1fs > %.0fs threshold "
                                "— racing Genius + Gemini in parallel for more complete lyrics "
                                "(current source=%s, %d chars)",
                                gap_score_initial, GAP_REFETCH_THRESHOLD_S,
                                lyrics_source, len(fa_text),
                            )
                            # INCIDENT 2026-05-25 (Legalícenla, job 9911c2f3ab16):
                            # Genius returned 48 lines (vs lrclib 45) — just
                            # +6 %, below the old +15 % threshold, so the
                            # code declared "not worth re-running FA" AND
                            # NEVER CALLED GEMINI (it was guarded behind
                            # `if not better_text`). Gemini's grounded
                            # search would have brought the intro chorus
                            # block that both lrclib and Genius omit. Two
                            # fixes in one block:
                            #
                            #   1) Race Genius + Gemini in PARALLEL — pick
                            #      whichever returns more lines. Cost: one
                            #      extra Gemini call (~$0.001), worth it
                            #      vs the regression.
                            #   2) Lower the re-run-FA threshold from +15 %
                            #      to +5 %. The downstream gap_score check
                            #      (must halve to adopt) already gates
                            #      against bad alternates — the +15 % was
                            #      double-gating and over-conservative.
                            async def _try_genius():
                                try:
                                    import genius_fetch
                                    if not genius_fetch.is_enabled():
                                        return None
                                    with scoped_db() as _refetch_db:
                                        gt = await asyncio.to_thread(
                                            genius_fetch.fetch_genius_plain,
                                            artist_hint, song_hint, _refetch_db,
                                        )
                                    return ("genius", gt) if gt else None
                                except Exception as e:
                                    logger.warning("[LYRICS] re-fetch Genius failed: %s", e)
                                    return None

                            async def _try_gemini():
                                try:
                                    from pipeline import _fetch_lyrics_via_gemini_search
                                    with scoped_db() as _refetch_db:
                                        gmt = await asyncio.to_thread(
                                            _fetch_lyrics_via_gemini_search,
                                            artist_hint, song_hint, job_id, _refetch_db,
                                        )
                                    return ("gemini", gmt) if gmt else None
                                except Exception as e:
                                    logger.warning("[LYRICS] re-fetch Gemini failed: %s", e)
                                    return None

                            genius_res, gemini_res = await asyncio.gather(
                                _try_genius(), _try_gemini(),
                            )

                            # Score candidates by line count — more lines
                            # = more coverage of intro/middle/outro chorus
                            # repeats that both providers sometimes omit.
                            # Ties go to Gemini (editorial > scraped).
                            def _lines(t: str) -> int:
                                return len([l for l in (t or "").splitlines() if l.strip()])
                            candidates = [r for r in (genius_res, gemini_res) if r is not None]
                            better_text: str | None = None
                            better_source: str | None = None
                            if candidates:
                                candidates.sort(
                                    key=lambda c: (_lines(c[1]), 1 if c[0] == "gemini" else 0),
                                    reverse=True,
                                )
                                better_source, better_text = candidates[0]
                                logger.info(
                                    "[LYRICS] re-fetch parallel: genius=%s gemini=%s — picked %s (%d lines)",
                                    f"{_lines(genius_res[1])}L" if genius_res else "miss",
                                    f"{_lines(gemini_res[1])}L" if gemini_res else "miss",
                                    better_source, _lines(better_text),
                                )

                            # Re-run FA if the new text has ≥ 5 % more
                            # lines than lrclib (loosened from 15 %). The
                            # gap_score halving check downstream still
                            # gates adoption — so a marginal but
                            # SUBSTANTIVELY-better source (Gemini bringing
                            # the missing intro chorus block) gets
                            # measured against gap reduction, not raw line
                            # count.
                            if better_text:
                                orig_lines = len([l for l in (fa_text or "").splitlines() if l.strip()])
                                new_lines = _lines(better_text)
                                if new_lines > orig_lines * 1.05:
                                    logger.info(
                                        "[LYRICS] re-fetch from %s found %d lines (was %d, +%.0f%%) "
                                        "— re-running FA",
                                        better_source, new_lines, orig_lines,
                                        (new_lines / max(1, orig_lines) - 1) * 100,
                                    )
                                    try:
                                        new_fa_segs = await asyncio.to_thread(
                                            forced_align.forced_align_lyrics, _aa, better_text,
                                        )
                                    except Exception as e:
                                        logger.warning("[LYRICS] re-fetch FA retry raised: %s", e)
                                        new_fa_segs = None
                                    if new_fa_segs:
                                        new_gap_score = _gap_score(new_fa_segs)
                                        # Accept the new FA only if it
                                        # halves the gap_score. Marginal
                                        # improvements aren't worth the
                                        # risk of switching to a different
                                        # lyrics version of the song.
                                        if new_gap_score < gap_score_initial * 0.5:
                                            logger.info(
                                                "[LYRICS] re-fetch IMPROVED: gap %.1fs → %.1fs, "
                                                "%d → %d segs — adopting %s source",
                                                gap_score_initial, new_gap_score,
                                                len(fa_segs), len(new_fa_segs), better_source,
                                            )
                                            fa_segs = new_fa_segs
                                            fa_text = better_text
                                            lyrics_source = f"lrclib_then_{better_source}"
                                        else:
                                            logger.info(
                                                "[LYRICS] re-fetch did NOT improve: gap %.1fs → %.1fs "
                                                "— keeping lrclib result",
                                                gap_score_initial, new_gap_score,
                                            )
                                else:
                                    logger.info(
                                        "[LYRICS] re-fetch %s returned similar length (%d vs %d lines) "
                                        "— not worth re-running FA",
                                        better_source, new_lines, orig_lines,
                                    )
                            else:
                                logger.info("[LYRICS] re-fetch: no better source found, keeping lrclib")

                        # GAP RESCUE (2026-05-25, rev 2): lrclib's community-
                        # curated lyrics OMIT chorus repeats at both ENDS and
                        # in the MIDDLE of songs whose chorus repeats many
                        # times. Forced-align honours the text it gets, so
                        # every omitted chorus block becomes a gap in the
                        # FA output.
                        #
                        # Original PR #307 only rescued the INTRO. The
                        # "Legalícenla" review on 2026-05-25 showed that
                        # the body also had 25-s gaps between FA segments
                        # where lrclib omitted entire chorus blocks. Same
                        # symptom, different position. This rev sweeps
                        # EVERY gap (intro + body) > GAP_THRESHOLD_S.
                        #
                        # For each gap we ask whisperX what it transcribed
                        # in that window, filter for hallucinations + the
                        # "stuck-phoneme" repetitive pattern (the canonical
                        # "Le realizan la × 3" failure that ate the intro
                        # of Legalícenla after PR #307 shipped), and merge
                        # into fa_segs. `dedup_collisions` in
                        # `_emit_segments` catches any rescued line that
                        # happens to duplicate an FA line.
                        GAP_THRESHOLD_S = 15.0
                        GAP_BUFFER_S = 1.0
                        from transcribe_postprocess import (
                            filter_rescue_candidates,
                            is_suspiciously_repetitive,
                        )

                        # Always await the warm whisperX result now (we
                        # need it to fill the gaps even when FA wins on
                        # the lines lrclib provided).
                        try:
                            wx_intro_segs = await wx_warm_task
                        except Exception as e:
                            logger.warning("[LYRICS] gap-rescue whisperX await failed: %s", e)
                            wx_intro_segs = None

                        # Enumerate every gap > threshold. The first one
                        # is the intro (0 → first_fa_start). The middle
                        # ones are between consecutive FA segments. We
                        # skip the tail gap (after the last FA seg) —
                        # songs end with instrumental outros all the time,
                        # rescuing there is more likely to produce
                        # hallucinations than truth.
                        fa_sorted = sorted(
                            (dict(s) for s in fa_segs),
                            key=lambda s: float(s.get("start") or 0),
                        )
                        gaps: list[tuple[float, float]] = []
                        first_start = float(fa_sorted[0].get("start") or 0)
                        if first_start > GAP_THRESHOLD_S:
                            gaps.append((0.0, first_start))
                        for prev_seg, next_seg in zip(fa_sorted, fa_sorted[1:]):
                            g_start = float(prev_seg.get("end") or 0)
                            g_end = float(next_seg.get("start") or 0)
                            if g_end - g_start > GAP_THRESHOLD_S:
                                gaps.append((g_start, g_end))

                        rescued_total: list = []
                        if gaps and wx_intro_segs:
                            try:
                                from pipeline import _filter_whisper_hallucinations
                            except Exception as e:
                                logger.warning("[LYRICS] gap-rescue: could not import hallucination filter (%s)", e)
                                _filter_whisper_hallucinations = lambda s: (s, 0)  # noqa: E731

                            for g_start, g_end in gaps:
                                cand = filter_rescue_candidates(
                                    wx_intro_segs,
                                    start_s=g_start,
                                    end_s=g_end,
                                    buffer_s=GAP_BUFFER_S,
                                )
                                if not cand:
                                    continue
                                try:
                                    cand, _drops = _filter_whisper_hallucinations(cand)
                                except Exception as e:
                                    logger.warning("[LYRICS] gap-rescue hallucination filter failed: %s", e)
                                # Pass `fa_text` as reference: if the
                                # repeated tokens appear in the canonical
                                # lyrics (e.g. "Legalícenla / Legalícenla
                                # / Legalícenla / Oh-oh-oh" repeats
                                # verbatim in lrclib), the repetition is
                                # a legitimate chorus — NOT hallucination.
                                # See `is_suspiciously_repetitive` docstring
                                # for the INCIDENT 2026-05-25 #2 details.
                                if cand and is_suspiciously_repetitive(cand, reference_text=fa_text):
                                    # INCIDENT 2026-05-25 #3 (Legalícenla,
                                    # UMG dry-run): the guard correctly
                                    # detects "Le realizan la × 3" as
                                    # whisperX hallucination, BUT
                                    # discarding `cand` loses the only
                                    # signal we had for WHERE the intro
                                    # chorus is in time. PROD (no guard)
                                    # emitted "Le realizan la" segments
                                    # with correct 0:17/0:19/0:21
                                    # timestamps — bad text, good timing.
                                    # Staging dropped them — no text, no
                                    # timing — and the user lost the
                                    # whole intro chorus.
                                    #
                                    # HYBRID FALLBACK: if the rejected
                                    # gap is the INTRO ([0, first_fa])
                                    # AND the canonical lyrics start with
                                    # text not in fa_segs (= chorus lines
                                    # lrclib synced omitted), keep the
                                    # whisperX TIMESTAMPS but REPLACE
                                    # their text with the canonical
                                    # lyrics. Best of both: PROD-grade
                                    # timing accuracy + correct text.
                                    is_intro_gap = (g_start <= 0.5)
                                    rescued_hybrid = []
                                    if is_intro_gap:
                                        fa_texts_norm = {
                                            (s.get("text") or "").strip().lower()
                                            for s in fa_segs
                                        }
                                        plain_lines = [
                                            l.strip()
                                            for l in (fa_text or "").splitlines()
                                            if l.strip()
                                        ]
                                        intro_text_lines: list[str] = []
                                        for ln in plain_lines:
                                            if ln.lower() in fa_texts_norm:
                                                break
                                            intro_text_lines.append(ln)
                                        if intro_text_lines:
                                            for c, txt in zip(cand, intro_text_lines):
                                                rescued_hybrid.append({
                                                    **c,
                                                    "text": txt,
                                                })
                                    if rescued_hybrid:
                                        rescued_total.extend(rescued_hybrid)
                                        logger.info(
                                            "[LYRICS] gap-rescue HYBRID for [%.1f,%.1f]: kept %d whisperX timestamps + canonical text (guard rejected hallucinated text, preserved timing)",
                                            g_start, g_end, len(rescued_hybrid),
                                        )
                                    else:
                                        logger.warning("[LYRICS] gap-rescue REJECTED for [%.1f,%.1f]: stuck-phoneme hallucination (%d lines, no ref match)",
                                                       g_start, g_end, len(cand))
                                    continue
                                if cand:
                                    rescued_total.extend(cand)
                                    logger.info("[LYRICS] gap-rescue [%.1f,%.1f]: +%d line(s)",
                                                g_start, g_end, len(cand))

                        # Sort the union by start so the editor list is
                        # chronological. dedup_collisions in
                        # `_emit_segments` cleans up any overlap that
                        # slipped through.
                        merged = sorted(
                            rescued_total + list(fa_segs),
                            key=lambda s: float(s.get("start") or 0),
                        )

                        from timing_sources import FORCED_ALIGN
                        return _emit_segments(
                            merged,
                            FORCED_ALIGN,
                            reference_lyrics=fa_text,
                            recovery_source=(
                                "forced_align+gap_rescue" if rescued_total
                                else "forced_align"
                            ),
                        )

                    # FA failed / non-retryable / timed out. Use the
                    # warm-start whisperX result (likely done by now —
                    # whisperX is ~75-180s, FA budget is 480s). Reconcile
                    # against lrclib plain text so the OUTPUT IS WORLDCLASS:
                    # whisperX gives word-level timing pinned to the audio
                    # + lrclib plain gives canonical text (no hallucinations).
                    try:
                        wx_warm_segs = await wx_warm_task
                        if wx_warm_segs and len(wx_warm_segs) >= 2:
                            from pipeline import _filter_whisper_hallucinations
                            wx_warm_segs, _ = _filter_whisper_hallucinations(wx_warm_segs)
                            # INCIDENT 2026-05-25 (post-PR #308): the
                            # `is_suspiciously_repetitive` guard was applied
                            # ONLY in the gap_rescue branch above. The
                            # FA-failed → whisperX standalone path didn't
                            # have it, so a Legalícenla retry showed
                            # ["Le realizan la"] × 4 at the top of the
                            # transcription. Same guard, same rationale —
                            # apply it here too. If reconcile fails AND
                            # raw whisperX looks like a stuck-phoneme
                            # hallucination, refuse to emit it; the
                            # whisper-1 fallback path below takes over.
                            from transcribe_postprocess import is_suspiciously_repetitive
                            if is_suspiciously_repetitive(wx_warm_segs, reference_text=fa_text):
                                logger.warning("[LYRICS] FA failed AND warm-start whisperX is stuck on a phoneme pattern (%d near-identical lines, no ref match) — refusing to emit, falling to whisper-1",
                                               len(wx_warm_segs))
                            else:
                                import whisperx_reconcile
                                _reconciled = whisperx_reconcile.reconcile(wx_warm_segs, fa_text) if fa_text else None
                                final_segs = _reconciled if _reconciled else wx_warm_segs
                                _src_tag_str = "whisperx_reconciled" if _reconciled else "whisperx"
                                logger.info("[LYRICS] FA failed — warm-start whisperX took over with %s segments [%s]",
                                            len(final_segs), _src_tag_str)
                                from timing_sources import WHISPERX_RECONCILED, WHISPERX
                                return _emit_segments(
                                    final_segs,
                                    WHISPERX_RECONCILED if _reconciled else WHISPERX,
                                    reference_lyrics=fa_text if _reconciled else "",
                                )
                    except Exception as e:
                        logger.warning("[LYRICS] warm-start whisperX also failed: %s — falling through to whisper-1", e)
                    # Both FA + warm whisperX failed — continue to the
                    # whisper-1 last-resort path below.
                elif fa_text:
                    # `synced` without `plain` — no warm-start path, run FA solo.
                    await _step("transcribe.align", 55)
                    _aa = await _get_align_audio()
                    fa_segs = await asyncio.to_thread(
                        forced_align.forced_align_lyrics, _aa, fa_text,
                    )
                    if fa_segs:
                        logger.info("[LYRICS] forced alignment used (%s lines, synced-only path) for %r - %r", len(fa_segs), artist_hint, song_hint)
                        from timing_sources import FORCED_ALIGN
                        return _emit_segments(
                            fa_segs, FORCED_ALIGN,
                            reference_lyrics=fa_text,
                            recovery_source="forced_align",
                        )
            # WORLD-CLASS audio-as-truth principle (Rotor architecture):
            # lrclib provides TEXT, never timing. Synced timestamps in lrclib are
            # community-curated and can be globally mis-aligned to a specific
            # master (incident "Cosas Mías": lrclib placed the first line at
            # 49.4s while the user's audio sang it ~35s earlier; verify at 0.54
            # confidence was borderline-accepted by the old threshold). We
            # ELIMINATED that path. When lrclib has text but forced-align
            # didn't succeed above (or wasn't enabled), try whisperX next —
            # the AUDIO decides timing, lrclib only contributes canonical
            # text via reconcile.
            if synced or plain:
                import whisperx_transcribe
                if whisperx_transcribe.is_enabled():
                    await _step("transcribe.transcribe_word", 70)
                    _aa = await _get_align_audio()
                    fa_text_for_wx = plain or forced_align.lrc_to_plain_text(synced)
                    # Pass reference text as initial_prompt (anti-hallucination)
                    wx_segs = await asyncio.to_thread(
                        whisperx_transcribe.transcribe_whisperx, _aa, lang,
                        fa_text_for_wx,
                    )
                    if wx_segs:
                        from pipeline import _filter_whisper_hallucinations as _fwh
                        wx_segs, _ = _fwh(wx_segs)
                        _wx_dur = await asyncio.to_thread(_audio_duration, tmp_path)
                        _hall, _why = _detect_hallucination(wx_segs, _wx_dur, language=lang)
                        # Same is_suspiciously_repetitive guard as above:
                        # whisperX standalone can latch onto a phoneme
                        # pattern and emit the same garbage line N times.
                        # `_detect_hallucination` catches some shapes but
                        # not this one (the lines look plausible
                        # individually; the bug is the repetition).
                        from transcribe_postprocess import is_suspiciously_repetitive as _suspicious
                        if _suspicious(wx_segs, reference_text=fa_text_for_wx):
                            logger.warning("[LYRICS] lrclib-text whisperX path: stuck-phoneme hallucination detected (%d near-identical lines, no ref match) — falling through",
                                           len(wx_segs))
                        elif not _hall and len(wx_segs) >= 2:
                            # Reconcile: whisperX timing + lrclib canonical text
                            import whisperx_reconcile
                            _reconciled = whisperx_reconcile.reconcile(wx_segs, fa_text_for_wx) if fa_text_for_wx else None
                            final_segs = _reconciled if _reconciled else wx_segs
                            from timing_sources import WHISPERX_RECONCILED, WHISPERX
                            _src_tag = WHISPERX_RECONCILED if _reconciled else WHISPERX
                            logger.info("[LYRICS] lrclib-text fallback via whisperX — %s segments [%s]", len(final_segs), _src_tag)
                            return _emit_segments(
                                final_segs, _src_tag,
                                reference_lyrics=fa_text_for_wx if _reconciled else "",
                            )
                        else:
                            logger.warning("[LYRICS] lrclib-text whisperX rejected (%s) — falling through to plain+Whisper", _why or "thin")

            # No synced (or too few segments / unreliable timestamps) — but
            # we still have plain text from lrclib. Use it as the reference
            # so the editor's suggestion engine fires, and skip the Gemini-
            # grounded search step entirely (lrclib already gave us a clean
            # source).
            if plain:
                logger.info("[LYRICS] lrclib plain hit (%s chars) — running Whisper for timestamps (lyrics_hint primed), skipping Gemini", len(plain))

                # Pre-Whisper intro trim. The "Video Oficial" cuts of many
                # tracks add 30-90s of dialogue / extra music at the start
                # that the studio version (which lrclib indexes) doesn't
                # have. Feeding all of that to Whisper poisons its context
                # and causes it to hallucinate or under-segment the actual
                # song (verified end-to-end on "El Plan de la Mariposa —
                # El Riesgo": 12 segments on full audio vs 19 on trimmed).
                # When the user's audio is materially longer than lrclib's
                # studio length, we slice off the prefix and only send the
                # body to Whisper, then shift the returned timestamps back
                # so they align with the user's full file in the editor.
                user_dur = await asyncio.to_thread(_audio_duration, tmp_path)
                intro_offset = 0.0
                transcribe_path = tmp_path
                trimmed_path = None
                intro_segments: list[dict] = []
                # Trigger the intro-trim path ONLY when the user's audio
                # is *materially* longer than lrclib's studio cut. The
                # original threshold (3 s) misfired on live recordings
                # (Airbag "Blues del Infierno - River Plate" is 221 s vs
                # lrclib's 200 s; the 21-s gap is outro applause, NOT an
                # intro). A 30-s floor still catches the genuine cases —
                # "El Plan de la Mariposa - El Riesgo" Video Oficial has
                # 73 s of spoken-word preamble — without slicing every
                # live track. Threshold is env-overridable for diagnosis.
                _trim_floor = float(os.environ.get("INTRO_TRIM_FLOOR_SEC", "30"))
                if (lrc_dur and user_dur
                        and _trim_floor < (user_dur - lrc_dur) <= 120.0):
                    intro_offset = float(user_dur - lrc_dur)
                    candidate = os.path.join(tmp_dir, "body_only.mp3")
                    sliced = await asyncio.to_thread(
                        _slice_audio_window, tmp_path, candidate,
                        intro_offset, user_dur - intro_offset,
                    )
                    if sliced:
                        transcribe_path = candidate
                        trimmed_path = candidate
                        logger.info("[LYRICS] trimmed %.1fs intro before Whisper (user=%.1fs, lrclib=%.1fs)", intro_offset, user_dur, lrc_dur)
                    else:
                        intro_offset = 0.0  # slice failed — fall through

                # Hybrid intro Whisper. The intro region we sliced off may
                # contain a spoken dialogue / narration that previews the
                # song's lyrics (verified case: "El Plan de la Mariposa —
                # El Riesgo" Video Oficial has 73 s of voice-over reciting
                # the first verse before the song starts). Run Whisper on
                # the intro chunk with the same lyrics_hint so it
                # transcribes the spoken text against the known vocabulary
                # and emits real timestamps for it. The segments returned
                # here are kept as-is in the user's full-audio frame
                # (they were never shifted) and prepended to the final
                # output so the operator sees the dialogue subtitled at
                # 0:00–intro_offset and the song body subtitled
                # afterwards.
                #
                # PARALELIZATION (PR #298, 2026-05-24): intro Whisper and
                # body Whisper used to run SERIALLY here — intro completed
                # (~10-15 s) and only then body started (~30-60 s). On
                # tracks with an intro, the user paid for the full sum.
                # Now we launch both as `asyncio.create_task` and
                # `asyncio.gather` them. Trade-off: 2 OpenAI Whisper-1
                # calls concurrent on this path (only fires when
                # lrclib-plain hit AND intro_offset > 0 — rare). Cost
                # impact is negligible (~$0.005 extra per fire), latency
                # reduction is ~10-15 s per affected job.
                aligner_enabled = (
                    os.environ.get("LRCLIB_PLAIN_ALIGNER_ENABLED", "0")
                    .strip().lower() in ("1", "true", "yes", "on", "y", "t")
                )
                await _step("transcribe.transcribe", 50)

                intro_path = None
                intro_path_to_clean = None
                if intro_offset > 0:
                    intro_path_candidate = os.path.join(tmp_dir, "intro_only.mp3")
                    if await asyncio.to_thread(
                        _slice_audio_prefix, tmp_path, intro_path_candidate,
                        intro_offset + 1.0,
                    ):
                        intro_path = intro_path_candidate
                        intro_path_to_clean = intro_path_candidate

                async def _run_intro_whisper():
                    if not intro_path:
                        return []
                    try:
                        raw = await loop.run_in_executor(
                            None, transcribe, intro_path, lang, plain,
                        )
                        # Keep only segments that fully sit in the intro
                        # window; defensive against ffmpeg frame-boundary
                        # slop.
                        kept = [s for s in raw if s["end"] <= intro_offset + 0.5]
                        logger.info("[LYRICS] intro Whisper produced %s segment(s) for the %.0fs dialogue prefix", len(kept), intro_offset)
                        return kept
                    except Exception as e:
                        logger.error("[LYRICS] intro Whisper failed: %s", e, exc_info=True)
                        return []

                async def _run_body_whisper():
                    return await loop.run_in_executor(
                        None,
                        lambda: transcribe(
                            transcribe_path, lang, plain,
                            return_words=aligner_enabled,
                        ),
                    )

                try:
                    # gather() preserves order: intro_segments first, body
                    # second. exceptions surface as their respective
                    # default fallbacks (intro→[], body→propagates).
                    intro_segments, segments = await asyncio.gather(
                        _run_intro_whisper(),
                        _run_body_whisper(),
                    )
                finally:
                    if intro_path_to_clean:
                        try:
                            os.unlink(intro_path_to_clean)
                        except OSError:
                            pass
                    if trimmed_path:
                        try:
                            os.unlink(trimmed_path)
                        except OSError:
                            pass

                # Shift body-Whisper timestamps back into full-audio
                # frame so the song subtitles appear at the right moment.
                # Shift `words` together with segment times — the aligner
                # below reads from them.
                if intro_offset > 0:
                    def _shift(s):
                        out = {**s,
                               "start": float(s["start"]) + intro_offset,
                               "end":   float(s["end"])   + intro_offset}
                        if "words" in s:
                            out["words"] = [
                                {**w,
                                 "start": float(w["start"]) + intro_offset,
                                 "end":   float(w["end"])   + intro_offset}
                                for w in s["words"]
                            ]
                        return out
                    segments = [_shift(s) for s in segments]

                # LRCLib-plain aligner: re-bucket Whisper words against
                # LRCLib's human-curated line structure. The renderer
                # otherwise uses Whisper's segmentation, which merges
                # short adjacent lines and splits long ones differently
                # than LRCLib — producing karaoke where one subtitle
                # covers two musical phrases or vice versa. The aligner
                # keeps LRCLib's line boundaries and pulls timing from
                # the first/last word in each matched span.
                #
                # Falls through to raw Whisper segments when:
                #   - the env flag is off (default)
                #   - the aligner couldn't match enough lines (< 50%
                #     coverage) — usually means Whisper missed most of
                #     the song, in which case downstream hallucination
                #     recovery is the better fallback.
                if aligner_enabled:
                    try:
                        from lrclib_aligner import align_lrclib_to_whisper
                        plain_lines_count = sum(
                            1 for ln in plain.splitlines() if ln.strip()
                        )
                        aligned = align_lrclib_to_whisper(plain, segments)
                        coverage = (
                            len(aligned) / plain_lines_count
                            if plain_lines_count else 0.0
                        )
                        if coverage >= 0.5 and len(aligned) >= 8:
                            logger.info("[LYRICS] aligner: %s/%s LRCLib lines aligned (%.0f%% coverage) — replacing Whisper segmentation", len(aligned), plain_lines_count, coverage * 100)
                            segments = [
                                {"start": a["start"],
                                 "end": a["end"],
                                 "text": a["text"]}
                                for a in aligned
                            ]
                        else:
                            logger.warning("[LYRICS] aligner: low coverage (%s/%s = %.0f%%) — keeping raw Whisper segments and falling through to hallucination recovery", len(aligned), plain_lines_count, coverage * 100)
                    except Exception as e:
                        # Opt-in and conservative: any aligner failure
                        # must NOT break the existing pipeline.
                        logger.error("[LYRICS] aligner error: %r — keeping raw Whisper segments", e, exc_info=True)

                # Auto-recover: when Whisper still hallucinates after the
                # trim (instrumental-passage mega-segments, synonym loops,
                # implausibly low count), fall back to distributing lrclib
                # plain lyrics across the SONG REGION only. start_time =
                # intro_offset prevents the synthesizer from compressing
                # the song lines into the spoken-intro region — that would
                # show 3 lyric lines at 0:00 even though the song hasn't
                # started yet (the bug the operator reported).
                hallucinated, reason = _detect_hallucination(segments, user_dur, language=lang)
                if hallucinated and user_dur:
                    anchors = _align_whisper_to_plain(segments, plain)
                    recovered = _synthesize_segments_from_plain(
                        plain, user_dur, anchors=anchors,
                        start_time=intro_offset,
                    )
                    if recovered:
                        from pipeline import _filter_intro_song_overlap
                        intro_segments, _dup = _filter_intro_song_overlap(
                            intro_segments, recovered,
                        )
                        if _dup:
                            logger.info("[LYRICS] discarded %s intro seg(s) as song-line hallucinations (recovery)", _dup)
                        combined = intro_segments + recovered
                        logger.warning("[LYRICS] hallucination detected (%s) — auto-recovered with %s lines from lrclib plain (%s time anchors, start=%.1fs, dur=%.1fs) + %s intro-Whisper segment(s)", reason, len(recovered), len(anchors), intro_offset, user_dur, len(intro_segments))
                        from timing_sources import WHISPER_LRCLIB_REC
                        return _emit_segments(
                            combined, WHISPER_LRCLIB_REC,
                            reference_lyrics=plain,
                            recovery_source="lrclib_plain",
                            coverage_warning=True,
                        )
                # Happy path: Whisper returned plausibly-many segments.
                # Combine intro Whisper (if any) with the body output.
                from pipeline import _filter_intro_song_overlap
                intro_segments, _dup = _filter_intro_song_overlap(
                    intro_segments, segments,
                )
                if _dup:
                    logger.info("[LYRICS] discarded %s intro seg(s) as song-line hallucinations", _dup)
                combined = intro_segments + segments
                from pipeline import _filter_whisper_hallucinations
                combined, _dropped = _filter_whisper_hallucinations(combined)
                if _dropped:
                    logger.warning("[TRANSCRIBE] dropped %s Whisper hallucination phrase(s)", _dropped)
                from timing_sources import WHISPER_LRCLIB
                return _emit_segments(
                    combined, WHISPER_LRCLIB, reference_lyrics=plain,
                )

        # Kick off Gemini-grounded lyrics fetch in parallel with Whisper.
        # The fetcher is best-effort (returns None on any failure); we wrap
        # its result-getter with asyncio.wait_for after Whisper completes
        # so a slow Gemini doesn't block /transcribe forever.
        #
        # The bg thread gets its OWN DB session, not the request-scoped one
        # — if the asyncio.wait_for below times out, the thread keeps running
        # in the background to populate the cache for the next call, and we
        # don't want it touching a session FastAPI already closed.
        from pipeline import _fetch_lyrics_via_gemini_search
        from database import SessionLocal as _SessionLocal

        def _bg_fetch_lyrics(artist, song):
            s = _SessionLocal()
            try:
                return _fetch_lyrics_via_gemini_search(artist, song, db=s)
            finally:
                s.close()

        gemini_task = asyncio.create_task(asyncio.to_thread(
            _bg_fetch_lyrics, artist_hint, song_hint,
        ))

        # When the plain-lyrics aligner is enabled, request word-level
        # timestamps so we can re-bucket Whisper's output against the
        # Gemini/lyrics.ovh reference's line structure (aligner pass below).
        # Same flag the lrclib-hit path uses. Default off.
        aligner_enabled = (
            os.environ.get("LRCLIB_PLAIN_ALIGNER_ENABLED", "0")
            .strip().lower() in ("1", "true", "yes", "on", "y", "t")
        )

        # ── Pre-fetch Gemini lyrics BEFORE running Whisper ──────────────────
        # Without this, Whisper is vocabulary-blind: it doesn't know what words
        # to expect and confabulates (e.g. "tanto miedo tu don" for "Frágil
        # espejo de voz"). Passing the reference text as lyrics_hint to each
        # chunk tells Whisper the vocabulary and dramatically improves accuracy.
        #
        # asyncio.shield() prevents the wait_for timeout from cancelling the
        # background task — Gemini keeps running and the result is cached in
        # Postgres for the next request regardless.
        # Timeout: 10s — typical Gemini latency is 2-4 s on a warm request;
        # we tolerate up to 10 s before running Whisper without a hint.
        _gemini_pre = ""
        try:
            _gemini_pre = (
                await asyncio.wait_for(asyncio.shield(gemini_task), timeout=10.0)
                or ""
            )
            if _gemini_pre:
                logger.info(
                    "[LYRICS] Gemini returned %d chars before Whisper — using as lyrics_hint",
                    len(_gemini_pre),
                )
        except asyncio.TimeoutError:
            logger.info("[LYRICS] Gemini hint not ready in 10s — Whisper runs without hint")
        except Exception as _e_gem:
            logger.info("[LYRICS] Gemini pre-fetch error (%s) — Whisper runs without hint", _e_gem)

        # Pre-fetch vocal stem so the chunked Whisper-1 transcription uses
        # clean audio (no backing music). The full mix causes timing compression
        # in uh/adlib sections — music fills every frame so Whisper can't anchor
        # phrase onsets against real silence. The stem has actual silence between
        # phrases, which gives Whisper accurate onset timestamps.
        #
        # _get_align_audio() is lazy + cached: subsequent calls (whisperX, FA)
        # return instantly. Gated by VAD_CHUNK_USE_STEM (default on); falls back
        # to full mix on any demucs error so the path never hard-fails.
        _whisper_audio = tmp_path
        if os.environ.get("VAD_CHUNK_USE_STEM", "1").strip().lower() in (
            "1", "true", "yes", "on"
        ):
            try:
                _stem = await _get_align_audio()
                # _get_align_audio() returns tmp_path on demucs failure, so
                # check that we got a *different* file before switching over.
                if _stem and _stem != tmp_path and os.path.exists(_stem):
                    _whisper_audio = _stem
                    logger.info(
                        "[LYRICS] no-lrclib Whisper using vocal stem (%s) for timing accuracy",
                        os.path.basename(_stem),
                    )
            except Exception as _e_stem:
                logger.warning(
                    "[LYRICS] vocal stem unavailable for Whisper (%s) — using full mix",
                    _e_stem,
                )

        segments = await loop.run_in_executor(
            None,
            lambda: transcribe(
                _whisper_audio, lang,
                lyrics_hint=_gemini_pre or None,
                return_words=aligner_enabled,
            ),
        )

        # reference: reuse what Gemini already returned (instant), or wait
        # up to 2s more if it didn't complete within the pre-fetch window.
        reference = ""
        if _gemini_pre:
            reference = _gemini_pre
        else:
            try:
                result = await asyncio.wait_for(gemini_task, timeout=2.0)
                reference = result or ""
            except asyncio.TimeoutError:
                # Gemini still pending — let it finish in the background and
                # cache the result for the next request. Don't block the user.
                logger.warning("[LYRICS] gemini fetch slower than Whisper+2s — moving on")
                reference = ""
            except Exception as e:
                logger.error("[LYRICS] gemini task failed: %s", e, exc_info=True)
                reference = ""

        # Final fallback: lyrics.ovh (free, no auth, thin catalogue but
        # covers some mainstream songs Gemini might miss or block).
        if not reference and artist_hint and song_hint:
            try:
                import requests as _req
                res = _req.get(
                    f"https://api.lyrics.ovh/v1/{artist_hint}/{song_hint}",
                    timeout=5,
                )
                if res.status_code == 200:
                    reference = res.json().get("lyrics", "").strip()
            except Exception:
                pass

        # Defense-in-depth recovery for the Gemini fallback path. We
        # don't have lrclib's duration here, so we can't compute
        # intro_offset — instead we use the gap-filling model that
        # works for any audio shape:
        #
        #   - keep Whisper segments that pass per-segment plausibility
        #     (preserves the spoken-intro transcription with REAL
        #     timestamps when present)
        #   - drop hallucinated segments (mega-segments, fuzzy
        #     intra-loops)
        #   - if kept Whisper covers > 70 % of the audio, ship as-is
        #   - otherwise, distribute reference lines into the
        #     UNCOVERED gaps proportionally to each gap's duration
        #
        # This is generic enough to handle El Plan de la Mariposa
        # (Whisper captures the dialogue intro at 0–14 s, hallucinates
        # the song body), Karol G "Si Antes Te Hubiera Conocido"
        # (similar dialogue prefix), and any future song with the
        # same "good prefix + bad body" pattern.
        if reference:
            user_dur = await asyncio.to_thread(_audio_duration, tmp_path)

            # Forced alignment (preferred when enabled): align the Gemini/
            # lyrics.ovh reference to THIS audio at ±50ms. Falls back to the
            # Whisper-word aligner / gap-fill below on any failure.
            import forced_align
            if forced_align.is_enabled():
                await _step("transcribe.align", 55)
                _aa = await _get_align_audio()
                fa_segs = await asyncio.to_thread(
                    forced_align.forced_align_lyrics, _aa, reference,
                )
                if fa_segs:
                    logger.info("[LYRICS] forced alignment used (%s lines, gemini text)", len(fa_segs))
                    from timing_sources import FORCED_ALIGN
                    return _emit_segments(
                        fa_segs, FORCED_ALIGN,
                        reference_lyrics=reference,
                        recovery_source="forced_align",
                    )

            # WhisperX fallback — Rotor-grade audio-as-truth path. When
            # forced-align failed but Gemini gave reference text, transcribe
            # the audio directly with whisperX instead of cascading through
            # Whisper-1 + hallucination recovery that ends in uniform 7s
            # distribution. Incident "El Arbol De La Vida / Voy A Dejarte"
            # (Viejas Locas): forced-align Replicate rejected the audio with
            # `[1, 2, 0]` tensor error, Whisper-1 hallucinated "Música de
            # presentación" 346s/3 words, recovery distributed 48 lines
            # uniformly. WhisperX (verified live against the same audio)
            # returns word-level segments matching Rotor's output (first
            # vocal at 53.27s vs Rotor's 53.01s). When whisperX returns a
            # clean result we adopt ITS text — its transcription is usually
            # cleaner than Gemini's pre-chunked plain (different sources
            # phrase line-breaks differently; whisperX follows the audio).
            import whisperx_transcribe
            if whisperx_transcribe.is_enabled():
                await _step("transcribe.transcribe_word", 70)
                _aa = await _get_align_audio()
                # Pass `reference` (Gemini/lyrics.ovh text) as initial_prompt
                wx_segs = await asyncio.to_thread(
                    whisperx_transcribe.transcribe_whisperx, _aa, lang,
                    reference,
                )
                if wx_segs:
                    from pipeline import _filter_whisper_hallucinations as _fwh
                    wx_segs, _ = _fwh(wx_segs)
                    # Same adlib-split guard as the no-reference whisperX path.
                    try:
                        from post_reconcile import post_reconcile_cleanup
                        wx_segs = post_reconcile_cleanup(wx_segs)
                    except Exception:
                        pass
                    _hall, _why = _detect_hallucination(wx_segs, user_dur, language=lang)
                    # 3rd whisperX path — same `is_suspiciously_repetitive`
                    # guard as the other two. Catches the stuck-phoneme
                    # pattern that `_detect_hallucination` misses.
                    from transcribe_postprocess import is_suspiciously_repetitive as _suspicious
                    if _suspicious(wx_segs, reference_text=reference):
                        logger.warning("[LYRICS] gemini-FA whisperX fallback: stuck-phoneme hallucination detected (%d near-identical lines, no ref match) — falling through",
                                       len(wx_segs))
                    elif not _hall and len(wx_segs) >= 2:
                        # RECONCILIATION: whisperX gave us word-level timing
                        # pinned to the audio; reference (Gemini/lrclib) gives
                        # us curated text with clean line breaks. Re-bucket
                        # whisperX words into reference lines to get TEXT
                        # from reference + TIMING from whisperX (better than
                        # either alone — this beats Rotor on the text side).
                        import whisperx_reconcile
                        _reconciled = whisperx_reconcile.reconcile(wx_segs, reference)
                        final_segs = _reconciled if _reconciled else wx_segs
                        from timing_sources import WHISPERX_RECONCILED, WHISPERX
                        _source_tag = WHISPERX_RECONCILED if _reconciled else WHISPERX
                        logger.info("[LYRICS] whisperX took over (gemini-FA fallback) — %s segments [%s]", len(final_segs), _source_tag)
                        return _emit_segments(
                            final_segs, _source_tag,
                            reference_lyrics=reference if _reconciled else "",
                        )
                    else:
                        logger.warning("[LYRICS] whisperX rejected at gemini-FA fallback (%s) — falling through", _why or "thin")

            # Plain-lyrics aligner on the Gemini/lyrics.ovh reference —
            # mirrors the lrclib-hit path. Whisper merges/splits lines by
            # audio pauses, which on live recordings (crowd, reverb)
            # collapses ~50 sung lines into ~19 long segments. The aligner
            # keeps the reference's curated line boundaries and pulls timing
            # from the first/last Whisper word in each matched span. Behind
            # LRCLIB_PLAIN_ALIGNER_ENABLED; replaces only when coverage is
            # high enough, else falls through to the gap-fill recovery.
            if aligner_enabled:
                try:
                    from lrclib_aligner import align_lrclib_to_whisper
                    ref_lines = sum(
                        1 for ln in reference.splitlines() if ln.strip()
                    )
                    # keep_unmatched: insert the lines Whisper missed in
                    # place (interpolated timing + review flag) so the
                    # operator nudges/deletes instead of re-typing.
                    aligned = align_lrclib_to_whisper(
                        reference, segments,
                        keep_unmatched=True, total_duration=user_dur,
                    )
                    # Coverage is judged on CONFIDENT matches only (lines
                    # without the review flag), not the interpolated ones.
                    matched = sum(1 for s in aligned if not s.get("review"))
                    review = len(aligned) - matched
                    coverage = matched / ref_lines if ref_lines else 0.0
                    if coverage >= 0.5 and matched >= 8:
                        logger.info("[LYRICS] aligner (gemini): %s/%s lines matched (%.0f%% coverage), %s inserted for review — replacing Whisper segmentation", matched, ref_lines, coverage * 100, review)
                        segments = aligned
                    else:
                        logger.warning("[LYRICS] aligner (gemini): low coverage (%s/%s = %.0f%%) — keeping raw Whisper, trying hallucination recovery", matched, ref_lines, coverage * 100)
                except Exception as e:
                    # Opt-in and conservative: any aligner failure must NOT
                    # break the existing pipeline.
                    logger.error("[LYRICS] aligner (gemini) error: %r — keeping raw Whisper segments", e, exc_info=True)

            hallucinated, reason = _detect_hallucination(segments, user_dur, language=lang)
            if hallucinated and user_dur:
                merged = _fill_gaps_with_reference(
                    segments, reference, user_dur,
                    audio_path=tmp_path,
                )
                if merged is not None:
                    src = "gemini_or_lyrics_ovh"
                    plausible_count = sum(
                        1 for s in merged
                        if (s.get("text") or "") not in
                           [r.strip() for r in (reference or "").splitlines() if r.strip()]
                    )
                    logger.warning("[LYRICS] hallucination detected on fallback path (%s) — gap-fill produced %s segments from %s (~%s kept-Whisper, %s synthesized, dur=%.1fs)", reason, len(merged), src, plausible_count, len(merged) - plausible_count, user_dur)
                    from timing_sources import WHISPER_GEMINI_REC
                    return _emit_segments(
                        merged, WHISPER_GEMINI_REC,
                        reference_lyrics=reference,
                        recovery_source=src,
                        coverage_warning=True,
                    )

        # No-lyrics path — the audio is the only source of truth. Prefer
        # whisperX (Whisper large-v2 + wav2vec2 alignment + VAD) over the
        # whisper-1 segments above: word-level <100ms timing and far less
        # prone to the single-mega-segment hallucination. Behind
        # WHISPERX_ENABLED; falls back to whisper-1 on None / if the result
        # still looks hallucinated. Returns word stamps (persisted for a
        # future word-level editor).
        if not reference:
            import whisperx_transcribe
            if whisperx_transcribe.is_enabled():
                await _step("transcribe.transcribe_word", 70)
                _aa = await _get_align_audio()
                wx_segs = await asyncio.to_thread(
                    whisperx_transcribe.transcribe_whisperx, _aa, lang,
                )
                if wx_segs:
                    from pipeline import _filter_whisper_hallucinations as _fwh
                    wx_segs, _ = _fwh(wx_segs)
                    # Split adlib mega-blocks (e.g. "uh, uh, uh…" × 26s) BEFORE
                    # the hallucination check. _has_fuzzy_intra_loop fires on any
                    # segment with 12+ identical short tokens (Jaccard 1.0 on a
                    # 4-token window) — a 26s uh section is legitimate ad-lib, not
                    # a hallucination. Splitting into ~3.5s chunks gives each sub-
                    # segment ≤ 3 tokens, safely below the 12-token detection floor.
                    try:
                        from post_reconcile import post_reconcile_cleanup
                        wx_segs = post_reconcile_cleanup(wx_segs)
                    except Exception:
                        pass
                    _wx_dur = await asyncio.to_thread(_audio_duration, tmp_path)
                    _hall, _why = _detect_hallucination(wx_segs, _wx_dur, language=lang)
                    if not _hall and len(wx_segs) >= 2:
                        logger.info("[LYRICS] whisperX no-lyrics path — %s segments (word-level)", len(wx_segs))
                        from timing_sources import WHISPERX
                        return _emit_segments(
                            wx_segs, WHISPERX, reference_lyrics="",
                        )
                    logger.warning("[LYRICS] whisperX result rejected (%s) — keeping whisper-1", _why or "thin")

        from pipeline import _filter_whisper_hallucinations
        segments, _dropped = _filter_whisper_hallucinations(segments)
        if _dropped:
            logger.warning("[TRANSCRIBE] dropped %s Whisper hallucination phrase(s)", _dropped)
        # Safety net: with the aligner flag on but no usable reference (or
        # low coverage), split over-long Whisper segments by their word
        # timestamps so live/instrumental mega-lines don't reach the editor.
        # No-op on already-aligned segments (they carry no `words`).
        if aligner_enabled:
            from lrclib_aligner import split_long_segments_by_words
            _before = len(segments)
            segments = split_long_segments_by_words(segments)
            if len(segments) != _before:
                logger.info("[LYRICS] length-split: %s → %s segments", _before, len(segments))
        # Final fallback path: no reference, no whisperX usable. The
        # `_emit_segments` chokepoint runs `normalize_words` which keeps
        # any FA word-stamps (score present) and strips Whisper-1 raw
        # words (no score) — replaces the previous unconditional strip
        # that broke karaoke (Bug B).
        from timing_sources import WHISPER_RAW
        return _emit_segments(
            segments, WHISPER_RAW, reference_lyrics=reference,
        )
    except Exception as exc:
        # OBSERVABILITY (audit 2026-05-24): the orchestrator used to be
        # try/finally with NO except. Any uncaught exception (OOM, an
        # unexpected dict shape from Replicate, IndexError in split-long,
        # etc.) propagated to the caller, leaving the job in `processing`
        # with `timing_source=NULL` and zero context in Sentry.
        #
        # This block captures the exception with Sentry (worker process
        # doesn't have FastAPI's auto-capture), tags the job_id, AND
        # re-raises so the caller's own error path still runs. The
        # downstream caller is responsible for marking the job failed
        # (transcription_worker._fail).
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("job_id", job_id)
                scope.set_tag("audio_path", os.path.basename(audio_path or ""))
                scope.set_context("transcribe", {
                    "language": language, "artist": artist, "title": title,
                })
                sentry_sdk.capture_exception(exc)
        except Exception:
            pass  # Sentry must never break the error path
        logger.exception("[TRANSCRIBE] uncaught exception in _run_transcription_for_job for job=%s",
                         job_id)
        raise
    finally:
        # tmp_dir holds intermediate slices (intro/body cuts) only — the
        # main audio (audio_path) is under job_dir and must survive until
        # /generate enqueues it (or the reaper cleans it up).
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        # The demucs vocal stem is a standalone temp file (system temp dir),
        # not under tmp_dir — remove it explicitly.
        if _vocal_stem:
            try:
                os.unlink(_vocal_stem)
            except OSError:
                pass


@app.post("/generate")
@limiter.limit("120/minute")
async def generate_with_segments(
    request: Request,
    file: UploadFile = File(None),
    job_id: str = Form("", max_length=12),       # Job.job_id = VARCHAR(12)
    artist: str = Form("", max_length=255),      # Job.artist = VARCHAR(255)
    song_title: str = Form("", max_length=500),  # Job.song_title = VARCHAR(500)
    style: str = Form("oscuro", max_length=50),  # Job.style = VARCHAR(50)
    language: str = Form("", max_length=16),
    # segments_json es el payload del frontend con timing de cada lyric;
    # un video largo puede pesar varios cientos de KB. 5 MB es techo
    # generoso que rechaza payload absurdo sin restringir casos reales.
    segments_json: str = Form(..., max_length=5_000_000),
    delivery_profile: str = Form("youtube", max_length=20),  # Job.delivery_profile = VARCHAR(20)
    umg_frame_size: str = Form("", max_length=16),
    umg_fps: str = Form("", max_length=16),
    umg_prores_profile: str = Form("", max_length=4),
    background_id: int = Form(None),
    background_mode: str = Form("as_is", max_length=16),
    background_file: UploadFile = File(None),
    genre: str = Form("", max_length=64),
    font: str = Form("", max_length=64),
    concept: str = Form("", max_length=2000),
    movement_style: str = Form("", max_length=64),
    effect: str = Form("", max_length=32),
    animate_image: str = Form("", max_length=8),
    text_case: str = Form("upper", max_length=16),
    frame_format: str = Form("full", max_length=16),
    font_scale: str = Form("1.0", max_length=8),
    lyric_transition: str = Form("cut", max_length=16),
    text_motion: str = Form("none", max_length=16),
    lyrics_animation: str = Form("none", max_length=16),
    line_transition: str = Form("none", max_length=16),
    text_contrast: str = Form("medium", max_length=16),
    # Lyric text colors 2026-05-25. Hex `#RRGGBB` (7 chars), invalid input
    # normalized to "" by the call site so build_ass falls back to defaults.
    # INCIDENT 2026-05-26: missing Form params here made every POST
    # /generate reference the unbound local `lyric_color` → NameError →
    # 500. Prod users saw "Generando…" forever because the job was never
    # enqueued. Same root cause as /upload sibling above; same fix.
    lyric_color: str = Form("", max_length=8),
    lyric_sung_color: str = Form("", max_length=8),
    match_lyrics: bool = Form(True),
    background_hint: str = Form("", max_length=2000),
    bg_verbatim: bool = Form(False),
    custom_colors: str = Form("", max_length=200),
    # Add-on premium "Escenas" (multi-escena). Opt-in del operador en el
    # wizard. La ELEGIBILIDAD se chequea contra has_scenes_access ANTES de
    # forwardearlo al pipeline (un usuario sin acceso que mande el flag igual
    # cae al fondo único). Default False = comportamiento histórico.
    enable_scenes: bool = Form(False),
    # Capa C 2026-05-24: si el operador hizo pre-gen via /generate-preview
    # mientras editaba lyrics, este field contiene el hash que mapea al
    # background pre-cacheado en R2. La pipeline lo reusa antes de llamar
    # a Veo/Imagen — ahorra ~60-180s + $0.80-3.20 de cuota. Vacío = flow
    # tradicional sin cache.
    bg_cache_key: str = Form("", max_length=64),
    # Title-card customization (Full Rotor v1). Defaults = historical look.
    title_template: str = Form("auto", max_length=16),
    title_size: str = Form("1.0", max_length=8),
    title_artist_font: str = Form("", max_length=64),
    title_song_font: str = Form("", max_length=64),
    # UI v1.1 (2026-05-30): manual song split. "" = auto wrap (default).
    # When set, contains the 2 lines joined by "\n" — capped at 200 chars
    # to match the song title's effective range.
    title_song_break: str = Form("", max_length=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate video using user-edited segments (skips Whisper).

    Two flows:
      - **Reuse path** (`job_id` provided): the audio was already persisted
        by /transcribe and we just promote the job to `queued`. No body
        re-read, no re-upload to R2 — this is the path that fixes the
        OOM-on-large-WAV bug.
      - **Direct path** (no job_id, file required): legacy compat for
        callers that bypassed /transcribe. Streams the file in like before.
    """
    job_id = (job_id or "").strip()
    reuse = bool(job_id)

    if reuse:
        # Reuse path: verify the job belongs to caller and pull the audio
        # path / R2 key from the row. Two valid entry states:
        #   - transcribed_pending: editor flow (segments came from
        #     /transcribe-uploaded; segments_json carries the user-edited
        #     timings).
        #   - awaiting_upload: direct-generate flow (no editor;
        #     segments_json is "[]" so the worker runs Whisper itself
        #     against the audio that already landed in R2).
        from jobs import get_job_model
        job_row = get_job_model(db, job_id)
        if (not job_row
                or job_row.user_id != current_user["id"]
                or job_row.tenant_id != current_user["tenant_id"]):
            raise HTTPException(status_code=404, detail="Job not found.")
        # State whitelist for /generate. `transcribed_pending` is what the
        # transcription worker writes on success (post-2026-05-25 fix);
        # `transcribed` is accepted defensively for jobs that were written
        # by the older worker variant that drifted from the convention,
        # and `awaiting_upload` covers the direct-generate path (no editor).
        # See transcription_worker.py:137 for the writer side.
        if job_row.status not in ("transcribed_pending", "transcribed", "awaiting_upload"):
            raise HTTPException(
                status_code=409,
                detail=f"Job is in state {job_row.status!r}, cannot generate.",
            )
        if job_row.status == "awaiting_upload":
            # Direct-generate path. The R2 PUT must be finished (no
            # in-flight multipart) and the key must be recorded — without
            # those, the worker has nothing to fetch.
            if job_row.multipart_upload_id:
                raise HTTPException(
                    status_code=409,
                    detail="Multipart upload not completed yet.",
                )
            if not job_row.input_r2_key:
                raise HTTPException(
                    status_code=409,
                    detail="Job has no associated upload.",
                )
        existing_filename = job_row.filename
        existing_input_r2_key = job_row.input_r2_key
    else:
        if file is None or not file.filename:
            raise HTTPException(status_code=400, detail="Missing file or job_id.")
        if not file.filename.lower().endswith(_AUDIO_EXTENSIONS):
            raise HTTPException(
                status_code=400,
                detail="Only MP3 and WAV files are accepted.",
            )
        existing_filename = file.filename
        existing_input_r2_key = None

    artist = (artist or "").strip()
    song_title = (song_title or "").strip()
    if not artist or not song_title:
        parsed_artist, parsed_title = _parse_filename_artist_title(
            existing_filename or "", db=db, tenant_id=current_user.get("tenant_id", "")
        )
        if not artist:
            artist = parsed_artist
        if not song_title:
            song_title = parsed_title

    _enforce_plan_quota(db, current_user,
                        credits_needed=(scenes_credit_cost()
                                        if enable_scenes and has_scenes_access(current_user)
                                        else 1))
    _enforce_daily_volume_cap(db, current_user)
    _enforce_tenant_backlog(db, current_user)
    _enforce_disk_capacity()
    _enforce_memory_pressure()
    # Every submission is accepted as queued; RQ gives it to a worker the
    # moment one is free, and pipeline.run_pipeline flips status to
    # "processing" on its first line. No 429 for capacity reasons.
    initial_status = "queued"

    # Sanitize early — the AI-auth check below depends on background_mode
    # so we can't defer normalization to the resolve-library section.
    background_mode = background_mode if background_mode in ("as_is", "variation") else "as_is"

    # Check AI authorization (UMG Guideline 5). The skip applies only when
    # the operator picks a library asset AND uses it as-is — no AI invoked.
    # Variation mode still calls Veo image-to-video on a frame of the
    # source, which IS AI generation, so the auth gate must apply.
    _needs_ai_auth = (not background_id) or (background_mode == "variation")
    if _needs_ai_auth and current_user.get("role") != "admin":
        user_model = db.query(User).filter(User.id == current_user["id"]).first()
        if user_model and not user_model.ai_authorized:
            raise HTTPException(status_code=403, detail="AI tool usage not authorized. Contact admin for approval.")

    segments = json.loads(segments_json)
    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile, current_user=current_user)

    # Check plan limits
    usage_info = get_plan_usage(db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"),
                                billing_group=current_user.get("billing_group"))
    if usage_info["alert_100"] and current_user.get("plan") == "free":
        raise HTTPException(status_code=429, detail="Free plan limit reached. Upgrade to continue.")

    tenant_id = current_user["tenant_id"]

    if reuse:
        # Promote the existing transcribed_pending row in place — fill in
        # the fields the editor finalised + flip status to queued.
        job_row = get_job_model(db, job_id)
        job_row.artist = artist
        job_row.song_title = song_title or None
        job_row.style = style
        job_row.delivery_profile = delivery_profile
        job_row.umg_spec = umg_spec
        job_row.status = initial_status
        job_row.current_step = "queued"
        # Audit 2026-05-26 (#388 wizard-duplicate-jobs): reset progress +
        # error + last_progress_at on reuse. Without this, a double-fire
        # of /generate on the same job_id can land here while a prior
        # worker run had already written progress=N (e.g. 48) into the
        # row — the operator sees "status=queued progress=48%" in admin,
        # which is semantically impossible (queued = no worker ever
        # touched it). Reset matches what /retry already does for the
        # `error` → `processing` transition (main.py:7705-7711).
        job_row.progress = 0
        job_row.error = None
        job_row.last_progress_at = datetime.now(timezone.utc)
        db.commit()
    else:
        job_id = create_job(
            db,
            artist=artist, style=style, filename=existing_filename,
            user_id=current_user["id"], tenant_id=tenant_id,
            delivery_profile=delivery_profile, umg_spec=umg_spec,
            initial_status=initial_status,
            song_title=song_title,
        )

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, existing_filename)

    if reuse:
        input_r2_key = existing_input_r2_key
        # Local file MAY not be on this replica; the worker fetches from
        # R2 at run time when input_r2_key is set, so it's fine. If R2 is
        # disabled and the file isn't here either, the pipeline will
        # error out — same as any cross-replica edge case.
    else:
        # Stream the body to disk in 1 MiB chunks instead of buffering
        # the whole upload in RAM (the OOM path that caused the original
        # bug). Validate the audio header from disk after writing.
        await _stream_upload_to_disk(file, mp3_path)
        _validate_audio_file_on_disk(existing_filename, mp3_path)
        # Cross-container input transfer via R2 — see /upload for the full reason.
        input_r2_key = None
        if storage.is_enabled():
            input_r2_key = storage.upload_input(
                mp3_path, current_user["tenant_id"], job_id, existing_filename,
            )
            if input_r2_key:
                from jobs import get_job_model
                job_row = get_job_model(db, job_id)
                if job_row:
                    job_row.input_r2_key = input_r2_key
                    db.commit()

    # Resolve background: library asset > custom upload > AI generation
    bg_path = None
    bg_r2_key = None
    variation_source_path = None
    variation_source_r2_key = None
    variation_parent_id = None
    if background_id:
        bg_path, bg_r2_key, variation_source_path, variation_source_r2_key, variation_parent_id = (
            _resolve_library_background(
                background_id, background_mode, current_user, db, job_dir, job_id,
            )
        )
    elif background_file and background_file.filename:
        # Valida (magic bytes + tamaño), sube a R2 y persiste
        # bg_r2_key_cached para que edits y /retry preserven el archivo.
        bg_path, bg_r2_key = _save_custom_background(
            background_file, job_dir, job_id, current_user["tenant_id"],
        )

    _font_scale_gen = 1.0
    try:
        _font_scale_gen = max(0.6, min(1.5, float(font_scale or "1.0")))
    except (ValueError, TypeError):
        pass

    # Remove the orphan draft the wizard sometimes leaves behind: if a
    # sibling transcribed_pending/awaiting_upload row for the same audio was
    # just created (re-upload-on-generate bug), delete it so the operator
    # doesn't see a phantom "2nd job". Time-windowed so it never touches an
    # intentional re-upload of the same song later.
    #
    # Audit 2026-05-26 (#388 wizard-duplicate-jobs): sanitize the filename
    # before the dedupe filter. `/upload-url` persists `Job.filename` via
    # `_safe_basename` (main.py:2335 region) — strips directory components,
    # control chars, length caps — while the direct-generate path here
    # gets `existing_filename` from various sources that may or may not
    # have been sanitized. Filter `Job.filename == filename` is a literal
    # equality, so an unsanitized "Sin Gamulan/../foo.wav" vs sanitized
    # "Sin_Gamulan_-_..." in DB silently misses every sibling draft —
    # which is exactly how 4 jobs for the same audio ended up coexisting
    # in prod 2026-05-26.
    try:
        from jobs import supersede_sibling_drafts
        _dedup_filename = (
            _safe_basename(existing_filename) if existing_filename else ""
        )
        supersede_sibling_drafts(
            db, keep_job_id=job_id, user_id=current_user["id"],
            tenant_id=current_user["tenant_id"], filename=_dedup_filename,
        )
    except Exception as e:
        logger.warning("[DEDUP] supersede sibling drafts failed: %s", e)

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=current_user.get("plan", "100"),
        tenant_id=current_user.get("tenant_id", ""),
        segments_override=segments,
        # Audit fix 2026-05-25: language se recibía como Form param
        # (línea 5041) pero NUNCA se forwardaba al pipeline. Whisper/
        # Gemini caían a auto-detect (~50% misdetection en catálogo
        # hispanohablante con vocabulario mezclado). El endpoint legacy
        # /upload sí lo pasaba; /generate-with-segments (el del wizard
        # nuevo) lo había dropeado.
        language=language,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
        background_path=bg_path,
        input_r2_key=input_r2_key,
        bg_r2_key=bg_r2_key,
        variation_source_path=variation_source_path,
        variation_source_r2_key=variation_source_r2_key,
        variation_parent_asset_id=variation_parent_id,
        genre=genre,
        font=font,
        concept=concept,
        movement_style=movement_style,
        effect=effect,
        animate_image=str(animate_image).strip().lower() in ("true", "1", "yes", "on"),
        song_title=song_title,
        text_case=text_case if text_case in ("upper", "title", "lower", "original", "sentence") else "upper",
        frame_format=frame_format if frame_format in ("full", "cine") else "full",
        font_scale=_font_scale_gen,
        # Deprecados 2026-05-23 (ver primer endpoint /upload).
        lyric_transition="cut",
        text_motion="none",
        lyrics_animation=lyrics_animation if lyrics_animation in ("none", "karaoke", "word_reveal", "pop", "glow") else "none",
        line_transition=line_transition if line_transition in ("none", "slide_up", "slide_side", "wipe", "dissolve_blur") else "none",
        # Lyric text colors 2026-05-25. Hex #RRGGBB validado acá; cualquier
        # otro valor se normaliza a "" (= backend usa blanco default en
        # build_ass). Para karaoke: lyric_color = palabra no cantada,
        # lyric_sung_color = palabra cantada. Para otras animaciones:
        # lyric_color = único color del texto.
        lyric_color=(lyric_color.strip() if lyric_color and re.match(r"^#[0-9a-fA-F]{6}$", lyric_color.strip() or "") else ""),
        lyric_sung_color=(lyric_sung_color.strip() if lyric_sung_color and re.match(r"^#[0-9a-fA-F]{6}$", lyric_sung_color.strip() or "") else ""),
        text_contrast=text_contrast if text_contrast in ("subtle", "medium", "strong") else "medium",
        match_lyrics=match_lyrics,
        background_hint=(background_hint.strip() or None),
        bg_verbatim=bg_verbatim,
        custom_colors=(custom_colors.strip() or ""),
        # Capa C 2026-05-24: pasa el hash del bg pre-cacheado a la pipeline.
        # Si el cache hit, _ensure_background se skip y el job ahorra
        # ~60-180s + $0.80-3.20 de cuota Veo. Vacío = flow tradicional.
        bg_cache_key=(bg_cache_key.strip() or None),
        # Escenas (multi-escena): opt-in del operador AND elegibilidad real.
        # Si el flag llega pero el usuario no tiene acceso, se ignora (fondo
        # único) — el gate de feature vive en el backend, no en el form.
        enable_scenes=bool(enable_scenes) and has_scenes_access(current_user),
        title_template=title_template if title_template in ("auto", "centered", "lower_third", "badge") else "auto",
        title_size=_clamp_title_size(title_size),
        title_artist_font=(title_artist_font.strip() or ""),
        title_song_font=(title_song_font.strip() or ""),
        # UI v1.1: pass-through. Empty string preserves auto-wrap.
        title_song_break=(title_song_break or ""),
    )

    return {"job_id": job_id, "status": initial_status}


@app.get("/admin/queue")
def admin_queue(current_user: dict = Depends(get_current_user)):
    """Return queue depth per priority. Admin only."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return queue_depth()


@app.get("/delivery-profiles")
def get_delivery_profiles(current_user: dict = Depends(get_current_user)):
    """Return the catalog of accepted UMG specs for frontend dropdowns."""
    return {
        "profiles": ["youtube", "umg", "both"],
        "umg": umg_catalog(),
    }


@app.get("/status/{job_id}")
def status(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from pipeline import _MAX_EDITS
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    edit_count = job.get("edit_count") or 0
    # Admins are exempt from the per-job edit cap. The frontend reads
    # edit_limit_exempt to skip the limit-reached panel and show "sin
    # límite" instead of a remaining count.
    _is_admin = current_user.get("role") == "admin"
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "current_step": job["current_step"],
        "progress": job["progress"],
        "files": job["files"],
        "error": job.get("error"),
        "artist": job.get("artist"),
        # song_title + style: needed by the post-render edit-wizard
        # (App.jsx EditLyricsRoute) to pre-fill all wizard fields off
        # /status alone. Without these, the edit-wizard can't show the
        # operator the current title/palette to fix a typo or compare.
        # JobDetail also reads song_title — pre-fix, `job.song_title`
        # was always undefined so the field silently fell back to the
        # filename derivation.
        "song_title": job.get("song_title"),
        "style": job.get("style"),
        "filename": job.get("filename"),
        "created_at": job.get("created_at"),
        # Frontend uses delivery_profile to decide whether to show the
        # UMG master download tab in JobDetail.
        "delivery_profile": job.get("delivery_profile", "youtube"),
        # ProRes readiness — drives the badge in JobDetail. Must be
        # included here so the badge reflects server state when the
        # user opens a job that already had ProRes generated.
        "s3_keys": job.get("s3_keys"),
        "prores_ready": job.get("prores_ready", False),
        "completed_at": job.get("completed_at"),
        # Edit request state — drives the "Pedir cambios" panel in
        # LyricsEditor. edits_remaining = _MAX_EDITS - edit_count, clamped
        # at zero. render_params holds the typography settings the last
        # edit applied (or the initial render) so the UI can preload them.
        "edit_count": edit_count,
        "edits_remaining": max(0, _MAX_EDITS - edit_count),
        "edit_limit_exempt": _is_admin,
        "render_params": job.get("render_params"),
        # Multi-escena: JobDetail dibuja la tira de corrección por escena desde
        # `job.scene_plan`. Al abrir por URL/refresh el job viene de ACÁ (no de
        # la lista), así que sin esto el filmstrip NUNCA aparecía por link (bug
        # 2026-07-01; #780 solo cubrió el camino "desde la lista").
        "scene_plan": job.get("scene_plan"),
        # EditRequestPanel reads these to drive the lyrics-edit and
        # typography-edit UIs. segments_json hydrates the inline lyrics
        # editor; bg_r2_key_cached gates the typography mode. Without
        # them, the panel falsely tells the user "este job no tiene
        # letras guardadas" even when the DB row has segments — exactly
        # the bug PR #111 fixed for `/jobs/{id}` but missed in this
        # endpoint, which is the one the frontend actually polls
        # (JobDetail.jsx fetches `/status/${job_id}`, never `/jobs/{id}`).
        "segments_json": job.get("segments_json"),
        "bg_r2_key_cached": job.get("bg_r2_key_cached"),
        # Approval state. JobDetail uses these to render the "Aprobado"
        # badge and to gate the "Enviar a UMG" button (admin-only). Both
        # were missing from this response since the endpoint was first
        # written for the upload-render flow, before the human-review
        # workflow existed — the badge silently never rendered because
        # the frontend reads job.approved_by directly from /status.
        "approved_by": job.get("approved_by"),
        "approved_at": job.get("approved_at"),
        # Whether this job is currently published on the UMG deliverables
        # portal. Drives the "Enviar a UMG" button state in JobDetail.jsx
        # (hidden / available / "✓ Ya en UMG"). Single boolean is enough —
        # JobDetail doesn't need the delivery id, just the on/off state.
        "is_in_umg_portal": (
            db.query(Delivery.id)
            .filter(Delivery.job_id == job_id)
            .filter(Delivery.removed_at.is_(None))
            .first()
            is not None
        ),
        "youtube": job.get("youtube"),
        "youtube_short": job.get("youtube_short"),
    }


def _sse_tick(token, job_id, scope, initial_user_id, initial_tenant_id):
    """Blocking per-tick work for the /events SSE stream.

    Run via ``asyncio.to_thread`` from the async generator so the JWT decode +
    2 DB queries do NOT execute on the event loop on every 2s tick of every
    open dashboard (with N tabs that was N×(decode+2 SELECT) on the loop each
    2s). Returns ``(job_dict_or_None, unauthorized, reason)``.

    Both re-validations below are deliberate and must stay:
      - JWT re-decode every tick (audit P0 #73): a 90-min render can outlive
        token expiry; surface ``unauthorized`` so the client falls back to
        authed polling where the 401 triggers the logout flow.
      - Tenant re-check every tick (PR #95): closes the window where an admin
        transfers the user across tenants mid-stream. Security, not optional.
    ``scoped_db()`` releases the connection back to the pool in milliseconds.
    """
    from auth import decode_token as _decode
    try:
        _decode(token)
    except HTTPException:
        return None, True, "token_expired"
    except Exception:
        # decode_token only raises HTTPException; treat any other JWT-lib
        # error as an invalid token too.
        return None, True, "token_invalid"
    with scoped_db() as db_tick:
        fresh_user = db_tick.query(User).filter(User.id == initial_user_id).first()
        if not fresh_user or fresh_user.tenant_id != initial_tenant_id:
            return None, True, "tenant_changed"
        job = get_job(db_tick, job_id, **scope)
    return job, False, ""


@app.get("/events/{job_id}")
async def job_events(
    job_id: str,
    token: str = Query(..., description="Auth token (EventSource can't send Bearer headers)"),
):
    """Server-Sent Events stream for a single job. Emits one event whenever
    the job's status, step, or progress changes, then closes on any terminal
    state. The client passes the login JWT as ?token= because EventSource
    does not support custom request headers.

    Connection budget: this is the worst pool-hog in the codebase
    pre-fix because an SSE stream can live for the full render
    duration (60+ min). The previous code grabbed Depends(get_db)
    AND opened a second session inside the generator — two
    connections per open dashboard tab. The current shape only
    opens a session for each 2-second poll tick, releasing it
    immediately so a hundred dashboards = a hundred brief tickle
    queries, not a hundred permanently-held sockets."""
    import asyncio

    # Validate auth + job access up front with a short-lived session.
    # If anything below fails the client gets a normal HTTP error
    # without ever entering the SSE generator.
    with scoped_db() as db:
        try:
            current_user = get_current_user_from_token_param(token, db)
        except HTTPException:
            raise HTTPException(status_code=401, detail="Invalid or expired token.")
        job_check = get_job(db, job_id, **_job_scope(current_user))
        if job_check is None:
            raise HTTPException(status_code=404, detail="Job not found.")

    # SSE terminal set = canonical _TERMINAL_STATUSES (jobs.py) PLUS the
    # quasi-terminal states the frontend treats as "stop polling" — namely
    # pending_review (waiting on operator), transcription_failed (editor
    # surfaces a Retry CTA, no further events expected), and
    # bg_preview_done / bg_preview_failed (ghost jobs that never advance).
    # Audit 2026-05-26: previous set excluded rejected, transcription_failed,
    # bg_preview_*. SSE for those statuses polled forever → socket leak +
    # the frontend never received the close event → operator's UI made it
    # look like the job "disappeared".
    TERMINAL = {
        "done", "pending_review", "error", "rejected",
        "validation_failed", "transcription_failed",
        "bg_preview_done", "bg_preview_failed",
    }
    scope = _job_scope(current_user)
    # Capturamos identidad+tenant al abrir para re-validar en cada poll.
    # Sin esto, si un admin transfiere al user entre tenants mid-stream
    # el SSE seguiría emitiendo eventos del job viejo (que ya pertenece
    # a otro tenant). Improbable hoy pero el costo de revalidar es 1
    # SELECT por poll cada 2 s → trivial.
    _initial_user_id = current_user["id"]
    _initial_tenant_id = current_user.get("tenant_id")

    async def event_generator():
        last_sig = None
        # Merge de dos fixes (ahora ejecutados en threadpool vía _sse_tick para
        # no bloquear el event loop en cada tick — ver _sse_tick):
        #   - PR #97: scoped_db() per tick para evitar pool starvation.
        #   - PR #95: re-validar tenant del user en cada tick (cierra el
        #     window donde admin transfiere user entre tenants mid-stream).
        while True:
            # JWT decode + 2 SELECTs por tick son trabajo bloqueante; los
            # corremos off-loop con asyncio.to_thread para que N dashboards
            # abiertos no inunden el event loop cada 2 s.
            job, unauthorized, unauthorized_reason = await asyncio.to_thread(
                _sse_tick, token, job_id, scope, _initial_user_id, _initial_tenant_id,
            )
            if unauthorized:
                yield f"event: unauthorized\ndata: {json.dumps({'reason': unauthorized_reason})}\n\n"
                break
            if job is None:
                break
            sig = (job["status"], job["current_step"], job["progress"])
            if sig != last_sig:
                last_sig = sig
                # ETA / step-text enrichment (audit 2026-05-27 "638"
                # operator: bar froze at 22%, "~8 min restantes" never
                # changed). step_eta computes a dynamic ETA from the
                # current_step + progress; the frontend reads `eta_s`
                # and `step_text_es` directly so it stops cycling
                # through hardcoded step labels.
                _eta_s = None
                _step_text = None
                try:
                    from step_eta import compute_eta_s, STEP_USER_TEXT_ES
                    _eta_s = compute_eta_s(job["current_step"], job["progress"])
                    if job["current_step"]:
                        _step_text = STEP_USER_TEXT_ES.get(
                            job["current_step"].strip().lower()
                            if isinstance(job["current_step"], str) else None
                        )
                except Exception:  # pragma: no cover
                    # ETA is best-effort. If step_eta breaks, the
                    # frontend falls back to its old hardcoded text.
                    pass
                payload = {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "current_step": job["current_step"],
                    "progress": job["progress"],
                    "error": job.get("error"),
                    "created_at": job.get("created_at"),
                    "completed_at": job.get("completed_at"),
                    "eta_s": _eta_s,
                    "step_text_es": _step_text,
                }
                yield f"data: {json.dumps(payload)}\n\n"
            if job["status"] in TERMINAL:
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs")
def list_jobs(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_all_jobs(db, **_job_scope(current_user))


@app.delete("/jobs/{job_id}")
async def delete_job_endpoint(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hard-delete a stuck or failed job row. Operator uses this to clean
    up history rows in `processing` / `queued` / `error` / `validation_failed`
    state. Done / pending_review jobs are protected (audit trail + plan
    quota integrity)."""
    tenant_id = current_user["tenant_id"]
    ok, reason = delete_job(db, job_id, tenant_id)
    if not ok:
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Job not found.")
        if reason.startswith("protected_status:"):
            status_val = reason.split(":", 1)[1]
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete a job in status '{status_val}'. Only stuck or failed jobs can be deleted.",
            )
        raise HTTPException(status_code=400, detail=reason)
    return {"deleted": job_id}


@app.post("/jobs/bulk-delete")
async def bulk_delete_jobs_endpoint(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete many jobs in one round-trip. Body: {"job_ids": ["aaa", "bbb"]}.
    Returns {"deleted": [...ids...], "skipped": {"id": reason}} so the UI
    can surface which IDs were protected (e.g. status=done) or didn't exist.
    Same safety rules as the single delete: only stuck/failed jobs go through.
    """
    tenant_id = current_user["tenant_id"]
    ids = payload.get("job_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise HTTPException(status_code=400, detail="Body must be {job_ids: [string, ...]}.")
    # Cap to a reasonable per-request batch so a runaway client can't
    # nuke the whole table in one call.
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="Too many ids in one request (max 200).")
    return bulk_delete_jobs(db, ids, tenant_id)


FILE_MAP = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
}

MEDIA_TYPES = {
    "video": "video/mp4",
    "short": "video/mp4",
    "thumbnail": "image/jpeg",
    "umg_master": "video/quicktime",
    "umg_short": "video/quicktime",
}

# File types that can't be previewed in-browser (ProRes is not browser-playable).
NON_PREVIEWABLE = {"umg_master", "umg_short"}

# Bundled in the "download all" zip. We exclude umg_master deliberately —
# ProRes masters are 1+ GB and have their own dedicated button in the UI.
_BUNDLE_TYPES = ("video", "short", "thumbnail")


# ProRes transcode helpers live in prores.py so the optional pre-warm
# RQ worker can import them without pulling in the FastAPI app.
from prores import (
    ensure_prores_exists,
    check_prores_readiness,
    ProResReadiness,
    ProResMisconfigured,
    ProResSourceMissing,
)


@app.get("/download/{job_id}/all")
async def download_all_zip(
    job_id: str,
    request: Request,
    token: str = Query(...),
):
    """Bundle the small deliverables (video MP4 + short + thumbnail) into a
    single ZIP so the operator gets one download instead of three rapid
    a.click() calls (which the browser treats as popup spam and drops).

    UMG ProRes masters are excluded by design: they're huge (1+ GB) and
    UMG editorial expects them as a stand-alone .mov, not buried in a zip.

    No Depends(get_db) — zip-build holds a session through the R2
    fetch + zip assembly + StreamingResponse. Releasing it after the
    metadata reads is enough for downstream code (R2 + zip are
    DB-free)."""
    import io as _io
    import zipfile as _zip

    with scoped_db() as db:
        current_user = verify_media_token(token, job_id, "all", db)
        job = get_job(db, job_id, **_job_scope(current_user))
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job["status"] != "done":
            raise HTTPException(status_code=400, detail="Job is not done yet.")

    job_files = job.get("files") or {}
    s3_keys = job.get("s3_keys") or {}
    bundle = [t for t in _BUNDLE_TYPES if job_files.get(f"{t}_url")]
    if not bundle:
        # umg-only jobs land here — they should be downloading the ProRes
        # master directly via /download/{id}/umg_master, not /all.
        raise HTTPException(
            status_code=400,
            detail="No bundleable deliverables for this job (UMG-only? use the master button).",
        )

    # Stage R2-stored files into a tmpdir so zipfile can stream them.
    # Keep files on disk only for the lifetime of this request.
    import tempfile, shutil
    tmp_dir = tempfile.mkdtemp(prefix=f"genly_zip_{job_id}_")
    try:
        on_disk: list[tuple[str, str]] = []  # (path, name_in_zip)
        for ftype in bundle:
            filename = FILE_MAP[ftype]
            key = s3_keys.get(ftype)
            if key and storage.is_enabled():
                local = os.path.join(tmp_dir, filename)
                if not storage.download_object(key, local):
                    # Fall through to disk as a last resort.
                    local = os.path.join(OUTPUTS_DIR, job_id, filename)
            else:
                local = os.path.join(OUTPUTS_DIR, job_id, filename)
            if not os.path.exists(local):
                logger.warning("[ZIP] missing source for %s: %s", ftype, local)
                continue
            on_disk.append((local, filename))

        if not on_disk:
            raise HTTPException(status_code=404, detail="Deliverables not found on disk or R2.")

        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", compression=_zip.ZIP_STORED) as zf:
            # ZIP_STORED (no compression) — MP4/JPG are already compressed,
            # re-zipping wastes CPU for ~0% size win.
            for path, name in on_disk:
                zf.write(path, arcname=name)
        buf.seek(0)

        # Filename is best-effort — fall back to job_id if artist/title are
        # missing so we never produce a zip with weird empty-string names.
        artist = (job.get("artist") or "").strip() or job_id
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in artist
        ).strip("_") or job_id
        zip_name = f"genly-{safe_name}.zip"

        _audit_media_access(
            current_user, job_id, "all",
            action="job.download", source="zip_bundle", request=request,
        )
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


@app.get("/media-token/{job_id}/{file_type}")
async def issue_media_token(
    job_id: str,
    file_type: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a short-lived (~5 min) token scoped to a single (job_id, file_type).

    The frontend calls this from a normal Bearer-authenticated request
    (token never appears in a URL) and embeds the returned token in the
    ?token=... query string of /download and /preview. Even if that URL
    leaks via Referer / browser history / server logs, it expires in 5
    minutes and only works for that exact file.

    The pseudo-file_type "all" is permitted for the /download/{id}/all
    zip endpoint, which bundles the small deliverables in one stream.
    """
    if file_type not in FILE_MAP and file_type != "all":
        raise HTTPException(status_code=400, detail="Invalid file type.")
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    # El media-token es la puerta a VER el media: si un admin cruza de
    # tenant, queda en el audit trail (contrato de la apertura cross-tenant).
    _audit_cross_tenant_access(db, current_user, job, kind=f"media-token:{file_type}")
    user_model = db.query(User).filter(User.id == current_user["id"]).first()
    return {"token": create_media_token(user_model, job_id, file_type)}


def _audit_media_access(
    current_user: dict,
    job_id: str,
    file_type: str,
    *,
    action: str,
    source: str,
    request: Request | None = None,
) -> None:
    """Write an AuditLog row for a media delivery (download / source audio).

    UMG-launch hardening 2026-06-01: a record label's first compliance
    question is "who accessed which master, when". Approve/reject/delete
    were already audited; the actual byte-serving endpoints were not.

    Uses its own short-lived session (the calling endpoints intentionally
    release their pool slot before serving — see scoped_db() docstring),
    and swallows every error: an audit-trail hiccup must never block a
    delivery that the user is authorized to receive.
    """
    try:
        with scoped_db() as db:
            db.add(AuditLog(
                user_id=current_user.get("id"),
                action=action,
                detail={
                    "job_id": job_id,
                    "tenant_id": current_user.get("tenant_id"),
                    "file_type": file_type,
                    "source": source,
                },
                ip_address=(request.client.host if request and request.client else None),
            ))
            db.commit()
    except Exception as e:  # pragma: no cover
        logger.warning("[AUDIT] media access log failed for %s/%s: %s", job_id, file_type, e)


@app.get("/download/{job_id}/{file_type}")
async def download(
    job_id: str,
    file_type: str,
    request: Request,
    token: str = Query(...),
):
    # No Depends(get_db) — see scoped_db() docstring. /download serves
    # multi-GB ProRes masters; holding a pool slot for the full upload
    # is one of the cheapest ways to lock the API out under load.
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    with scoped_db() as db:
        current_user = verify_media_token(token, job_id, file_type, db)
        job = get_job(db, job_id, **_job_scope(current_user))
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job["status"] != "done":
            raise HTTPException(status_code=400, detail="Job is not done yet.")
    tenant_id = current_user["tenant_id"]

    # Prefer a pre-signed URL to R2 so the uvicorn worker isn't tied up
    # streaming multi-GB ProRes masters. Pass download_filename so R2
    # sends Content-Disposition: attachment and the browser downloads
    # instead of opening the file inline.
    s3_key = (job.get("s3_keys") or {}).get(file_type)
    if s3_key and storage.is_enabled():
        url = storage.generate_signed_url(
            s3_key, expiry_seconds=3600,
            download_filename=FILE_MAP.get(file_type),
        )
        if url:
            # Audit only on responses that actually deliver the file —
            # the 202 ProRes-queued branches below are polled and would
            # produce false "downloaded" entries.
            _audit_media_access(
                current_user, job_id, file_type,
                action="job.download", source="r2_redirect", request=request,
            )
            return RedirectResponse(url, status_code=302)

    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])

    # Lazy ProRes path: never run ffmpeg synchronously in the request
    # thread. check_prores_readiness short-waits up to 15 s if a
    # transcode is mid-flight; otherwise tells us to enqueue a prewarm
    # and respond 202 + Retry-After. UMG's "first download" is now
    # bounded to whatever this thread does — no 60-300 s blocking,
    # no uvicorn-worker exhaustion under concurrent load.
    if file_type in ("umg_master", "umg_short"):
        readiness = check_prores_readiness(job_id, file_type, job, tenant_id)
        if readiness.state == ProResReadiness.READY_LOCAL:
            pass  # fall through to FileResponse below
        elif readiness.state == ProResReadiness.READY_R2:
            # Re-fetch the s3_keys (a sibling caller may have just uploaded
            # while we were checking the lock). Short-lived DB session
            # only for this re-read.
            from jobs import get_job_model as _get_job_model
            with scoped_db() as _db:
                _model = _get_job_model(_db, job_id)
                _s3_keys = dict(_model.s3_keys or {}) if _model else {}
            s3_key = _s3_keys.get(file_type)
            if s3_key and storage.is_enabled():
                url = storage.generate_signed_url(
                    s3_key, expiry_seconds=3600,
                    download_filename=FILE_MAP.get(file_type),
                )
                if url:
                    _audit_media_access(
                        current_user, job_id, file_type,
                        action="job.download", source="r2_redirect", request=request,
                    )
                    return RedirectResponse(url, status_code=302)
            # R2 said yes but signed URL failed — fall through.
        elif readiness.state == ProResReadiness.MISCONFIGURED:
            raise HTTPException(status_code=400, detail=readiness.detail)
        elif readiness.state == ProResReadiness.SOURCE_MISSING:
            raise HTTPException(status_code=404, detail=readiness.detail)
        elif readiness.state == ProResReadiness.NOT_STARTED:
            # Kick off a prewarm in the background, then 202.
            try:
                from queue_jobs import enqueue_prores_prewarm
                enqueue_prores_prewarm(job_id, file_type)
            except Exception as e:  # pragma: no cover
                logger.warning("[PRORES] enqueue prewarm from /download failed: %s", e)
            return JSONResponse(
                status_code=202,
                content={
                    "status": "queued",
                    "detail": readiness.detail,
                    "retry_after": readiness.retry_after_seconds,
                },
                headers={"Retry-After": str(readiness.retry_after_seconds)},
            )
        elif readiness.state == ProResReadiness.IN_PROGRESS:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "in_progress",
                    "detail": readiness.detail,
                    "retry_after": readiness.retry_after_seconds,
                },
                headers={"Retry-After": str(readiness.retry_after_seconds)},
            )

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    _audit_media_access(
        current_user, job_id, file_type,
        action="job.download", source="local_file", request=request,
    )
    return FileResponse(file_path, filename=FILE_MAP[file_type], media_type="application/octet-stream")


@app.get("/preview/{job_id}/{file_type}")
async def preview(
    job_id: str,
    file_type: str,
    token: str = Query(...),
):
    # No Depends(get_db) — see scoped_db() docstring. The dashboard fires
    # 6+ /preview/.../thumbnail calls in parallel on every refresh; with
    # the dependency-injected session that's 6 connections held for the
    # full streaming duration. Under modest concurrent dashboard load
    # this exhausted the pool and broke /usage (the original incident).
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    if file_type in NON_PREVIEWABLE:
        raise HTTPException(
            status_code=415,
            detail=f"{file_type} is a delivery master and cannot be previewed in-browser. "
                   f"Use /download/{job_id}/{file_type} instead.",
        )
    with scoped_db() as db:
        current_user = verify_media_token(token, job_id, file_type, db)
        job = get_job(db, job_id, **_job_scope(current_user))
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job["status"] not in ("done", "pending_review"):
            raise HTTPException(status_code=400, detail="Job is not ready for preview.")
        s3_key = (job.get("s3_keys") or {}).get(file_type)
    # DB session closed — pool is free for /usage and friends.

    # Local copy is removed after R2 upload to keep disk usage bounded. Fall
    # back to a signed URL for the preview in that case.
    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type=MEDIA_TYPES[file_type])

    if s3_key and storage.is_enabled():
        url = storage.generate_signed_url(s3_key, expiry_seconds=3600)
        if url:
            return RedirectResponse(url, status_code=302)

    raise HTTPException(status_code=404, detail="File not found.")


# ---------------------------------------------------------------------------
# UMG Compliance Status & Data Policy
# ---------------------------------------------------------------------------

_VERTEX_ENTERPRISE_CONFIRMED = os.environ.get("VERTEX_ENTERPRISE_CONFIRMED", "false").lower() == "true"

# Data policy: documents exactly what data is sent to which AI APIs
_DATA_POLICY = {
    "platform": "GenLy AI",
    "ai_provider": "Google Cloud Vertex AI (Enterprise)",
    "project_id": os.environ.get("VERTEX_PROJECT", ""),
    "region": os.environ.get("VERTEX_LOCATION", "us-central1"),
    "training_policy": {
        "statement": (
            "Google Cloud Vertex AI Enterprise does not use customer data to train "
            "foundation models. Per Google Cloud Terms of Service and Data Processing "
            "Amendment, customer data is not used to improve Google products or services "
            "without explicit opt-in. GenLy AI does not opt in to any training programs."
        ),
        "fine_tuning": "GenLy AI does not perform fine-tuning on any models.",
        "data_retention": (
            "Prompts and generated outputs are processed in real-time and are not "
            "retained by Google beyond the API request lifecycle, per Vertex AI's "
            "data processing terms."
        ),
    },
    "data_sent_to_ai": [
        {
            "api": "Gemini 2.5 Flash (gemini-2.5-flash)",
            "purpose": "Lyrics analysis for background style selection",
            "data_sent": ["Artist name (configurable via SEND_ARTIST_TO_AI)", "First 600 characters of transcribed lyrics"],
            "data_not_sent": ["Full audio files", "User personal data", "Billing information"],
        },
        {
            "api": "Veo 3.1 Fast (veo-3.1-fast-generate-001)",
            "purpose": "Video background generation",
            "data_sent": ["AI-generated scene description prompt (no artist/lyrics data)"],
            "data_not_sent": ["Audio files", "Lyrics text", "Artist name"],
        },
        {
            "api": "Imagen 4 (imagen-4.0-generate-001)",
            "purpose": "Image background generation (fallback)",
            "data_sent": ["AI-generated scene description prompt (no artist/lyrics data)"],
            "data_not_sent": ["Audio files", "Lyrics text", "Artist name"],
        },
        {
            "api": "Gemini 2.5 Flash (gemini-2.5-flash)",
            "purpose": "YouTube metadata generation (SEO)",
            "data_sent": ["Artist name", "Song name", "First 300 characters of lyrics"],
            "data_not_sent": ["Full audio files", "Full lyrics", "User personal data"],
        },
        {
            "api": "Gemini 2.5 Flash Vision",
            "purpose": "Output content validation (Guideline 15 compliance)",
            "data_sent": ["Extracted video frames (images only, no audio)"],
            "data_not_sent": ["Audio files", "Lyrics text", "Artist name"],
        },
    ],
    "safeguards": [
        "All AI prompts explicitly exclude generation of people, faces, hands, and text",
        "Output validation scans generated frames for prohibited content before approval",
        "Provenance records track every AI invocation with full prompt and response data",
        "Artist name can be anonymized via SEND_ARTIST_TO_AI=false configuration",
        "Human approval required before any generated content is downloadable (REQUIRE_REVIEW=true)",
    ],
}


@app.get("/compliance/status")
async def compliance_status(
    current_user: dict = Depends(get_current_user),
):
    """Return UMG compliance status for the platform."""
    return {
        "guidelines_version": "UMG AI Image and Video Tools Guidelines — October 22, 2025",
        "checks": {
            "guideline_1_tools": {
                "status": "confirmed" if _VERTEX_ENTERPRISE_CONFIRMED else "pending",
                "detail": (
                    "Google Veo 3.1 Fast via Vertex AI Enterprise API is in use. "
                    + ("Enterprise agreement has been confirmed." if _VERTEX_ENTERPRISE_CONFIRMED
                       else "ACTION REQUIRED: Confirm with UMG that your Vertex AI enterprise contract qualifies as the required enterprise-level agreement for Google Veo.")
                ),
                "tool": "veo-3.1-fast-generate-001",
                "provider": "Google Cloud Vertex AI",
                "project": os.environ.get("VERTEX_PROJECT", ""),
            },
            "guideline_3_prohibited_tools": {
                "status": "ok",
                "detail": "No prohibited tools in use. Verified: no Midjourney, Sora, Dall-E, Runway, Hailuo/Minimax.",
            },
            "guideline_5_authorization": {
                "status": "ok",
                "detail": "User AI authorization system active. Users must be authorized by admin before using AI tools.",
            },
            "guideline_6_limited_use": {
                "status": "ok",
                "detail": "AI is used only for background generation. Lyrics overlay, fonts, and compositing are human-created via traditional tools (moviepy, ffmpeg, ImageMagick).",
            },
            "guideline_14_no_training": {
                "status": "ok",
                "detail": "Vertex AI Enterprise does not train on customer data. No fine-tuning performed. Artist data minimization configurable.",
                "send_artist_to_ai": os.environ.get("SEND_ARTIST_TO_AI", "true"),
            },
            "guideline_15_content_safety": {
                "status": "ok",
                "detail": "Content validation active. AI prompts exclude people/faces. Output frames scanned by Gemini Vision before approval.",
            },
            "guideline_16_clearance": {
                "status": "ok",
                "detail": "Human review workflow active. Jobs require approval before content is downloadable or publishable.",
                "require_review": os.environ.get("REQUIRE_REVIEW", "true"),
            },
            "guideline_17_provenance": {
                "status": "ok",
                "detail": "Full AI provenance tracking active. Every AI call recorded with tool, prompt, data types, and output artifact.",
            },
        },
    }


@app.get("/compliance/data-policy")
async def compliance_data_policy(
    current_user: dict = Depends(get_current_user),
):
    """Return detailed data policy — what data is sent to which AI APIs."""
    return _DATA_POLICY


# ---------------------------------------------------------------------------
# AI Provenance (UMG Compliance)
# ---------------------------------------------------------------------------

@app.get("/provenance/{job_id}")
async def get_provenance(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return AI provenance records for a job."""
    from database import AIProvenance
    job = get_job(db, job_id, **_job_scope(current_user))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    records = (
        db.query(AIProvenance)
        .filter(AIProvenance.job_id == job_id)
        .order_by(AIProvenance.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "step": r.step,
            "tool_name": r.tool_name,
            "tool_provider": r.tool_provider,
            "tool_version": r.tool_version,
            "prompt_sent": r.prompt_sent,
            "prompt_hash": r.prompt_hash,
            "response_summary": r.response_summary,
            "input_data_types": r.input_data_types,
            "output_artifact": r.output_artifact,
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


@app.get("/provenance/{job_id}/export")
async def export_provenance(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Export provenance data for copyright registration filing."""
    from database import AIProvenance
    job = get_job(db, job_id, **_job_scope(current_user))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    records = (
        db.query(AIProvenance)
        .filter(AIProvenance.job_id == job_id)
        .order_by(AIProvenance.created_at)
        .all()
    )

    ai_elements = []
    human_elements = [
        {
            "element": "lyrics_transcription_review",
            "description": "Song lyrics transcribed by Whisper AI, then reviewed and edited by a human operator before video generation",
            "copyright_status": "Human-reviewed and edited — copyrightable as human creative contribution",
        },
        {
            "element": "style_selection",
            "description": "Visual style chosen by human operator (e.g. oscuro, neon, minimal, calido)",
            "copyright_status": "Human creative selection — copyrightable",
        },
        {
            "element": "font_rendering",
            "description": "Typography rendered using Google Fonts (SIL OFL license) via moviepy/ImageMagick — traditional software tools, not AI",
            "copyright_status": "Human-directed traditional rendering — copyrightable",
        },
        {
            "element": "text_overlay_composition",
            "description": "Lyrics positioned, timed, and styled over video background using moviepy — traditional compositing, not AI",
            "copyright_status": "Human-directed composition — copyrightable",
        },
        {
            "element": "video_compositing",
            "description": "Final video assembled from background + text layers + audio using moviepy and ffmpeg — traditional video editing tools",
            "copyright_status": "Human-directed assembly — copyrightable",
        },
        {
            "element": "audio_track",
            "description": "Original MP3 audio file provided by the user — not AI-generated",
            "copyright_status": "Pre-existing human-created work — fully copyrightable",
        },
    ]

    for r in records:
        is_human_bg = r.step == "background_human"
        ai_elements.append({
            "element": r.step,
            "tool": f"{r.tool_name} ({r.tool_provider})",
            "prompt": r.prompt_sent,
            "input_data_types": r.input_data_types,
            "timestamp": r.created_at.isoformat() if r.created_at else None,
            "copyright_status": (
                "Human-provided asset — copyrightable" if is_human_bg
                else "AI-generated from prompt — must be disclaimed for US copyright registration per USCO guidance"
            ),
        })

    return {
        "export_version": "1.0",
        "guidelines_reference": "UMG AI Image and Video Tools Guidelines — October 22, 2025",
        "job_id": job_id,
        "artist": job.get("artist"),
        "filename": job.get("filename"),
        "created_at": job.get("created_at"),
        "ai_generated_elements": ai_elements,
        "human_created_elements": human_elements,
        "copyright_disclaimer": {
            "summary": (
                "This video contains both AI-generated and human-created elements. "
                "Per the US Copyright Office (Copyright Registration Guidance, Feb 2023 & subsequent rulings), "
                "content generated solely by AI from prompts is not eligible for copyright protection. "
                "AI-generated elements in this video (primarily the background visuals) must be disclaimed "
                "in any US copyright registration filing. Human-created elements (lyrics overlay, composition, "
                "typography, audio synchronization, style selection) are eligible for copyright protection."
            ),
            "ai_elements_to_disclaim": [
                r.step for r in records if r.step != "background_human"
            ],
            "copyrightable_human_elements": [
                "Lyrics text overlay and timing",
                "Typography and font selection",
                "Visual composition and layout",
                "Audio-visual synchronization",
                "Style and aesthetic choices",
                "Original audio recording",
            ],
        },
        "data_protection": {
            "ai_provider": "Google Cloud Vertex AI (Enterprise)",
            "training_policy": (
                "Vertex AI Enterprise does not use customer data to train foundation models. "
                "No fine-tuning is performed. Customer data is processed in real-time and not "
                "retained beyond the API request lifecycle."
            ),
            "data_minimization": {
                "artist_name_sent": os.environ.get("SEND_ARTIST_TO_AI", "true") == "true",
                "max_lyrics_chars_sent": 600,
                "audio_sent_to_ai": False,
                "user_pii_sent": False,
            },
        },
        "approval": {
            "approved_by": job.get("approved_by"),
            "approved_at": job.get("approved_at"),
            "review_notes": job.get("review_notes"),
        },
    }


# ---------------------------------------------------------------------------
# Job Approval (UMG Compliance)
# ---------------------------------------------------------------------------

class ApproveJobRequest(BaseModel):
    notes: str = Field(default="", max_length=2048)


class EditJobRequest(BaseModel):
    # edit_type values:
    #  - typography: re-render with new font/size/case/motion settings
    #  - background: regenerate Veo, keep persisted segments
    #  - lyrics:     re-render with caller-supplied segments. Background
    #                reuses bg_r2_key_cached (no Veo cost). Use case:
    #                fix a typo/timing/word in a pending_review or done
    #                video without re-uploading the MP3.
    # Los campos posteriores NO se persisten a columnas VARCHAR — viajan
    # dentro de Job.render_params (JSON column). Los max_length acá son
    # límites del payload del cliente para evitar JSON gigante.
    edit_type: str = Field(..., max_length=32)
    font: str | None = Field(default=None, max_length=64)
    font_scale: float | None = None
    text_case: str | None = Field(default=None, max_length=16)
    frame_format: str | None = Field(default=None, max_length=16)
    # lyric_transition + text_motion deprecados 2026-05-23 — campos
    # eliminados del modelo. Clientes viejos que sigan mandándolos en el
    # body son ignorados por Pydantic (default: extra fields permitidos).
    text_contrast: str | None = Field(default=None, max_length=16)
    # Required when edit_type=="lyrics". For edit_type=="background" or
    # "typography", segments is OPTIONAL — if the operator made text
    # corrections inside the modal's LyricsEditor that autosave hasn't
    # flushed yet, send them here and the API will persist them to
    # segments_json before enqueueing. Each segment must have start (s),
    # end (s), text (str); anything else is ignored.
    segments: list[dict] | None = Field(default=None)
    # Optional free-form hint for edit_type=="background". The operator
    # types what they want the new background to convey ("paisaje cálido
    # al atardecer", "abstracto con ondas de luz suave", etc.) and the
    # pipeline forwards it to Gemini's system prompt as an explicit
    # operator override. Bump 300→2000 (2026-05-18): los modelos de
    # imagen/video rinden mejor con prompts detallados que permitan
    # negaciones redundantes ("no cars, no traffic, no people…") y
    # spec granular de cámara. 300 obligaba a sacrificar negaciones que
    # son críticas para evitar bias del modelo. Costo Gemini marginal.
    background_hint: str | None = Field(default=None, max_length=2000)
    # Operator-controlled bypass of content_validator (UMG Guideline 15
    # check). Default False = follow tenant default. True = skip validator
    # entirely (only has effect on UMG tenants — non-UMG already skip by
    # default).
    #
    # Use case: UMG operator wants to ship a video where the flagged
    # content IS the song's visual identity (rock guitarist hands).
    # They accept the downstream UMG-review rejection risk knowingly.
    bypass_content_validation: bool = Field(default=False)
    # Inverse of bypass for non-UMG tenants. Default False = follow tenant
    # default (skip validator for non-UMG). True = force validator to run
    # even though the tenant doesn't require it. For operators of non-UMG
    # tenants who *want* the conservative behavior anyway.
    force_content_validation: bool = Field(default=False)
    # Background generation mode. Only meaningful when edit_type=="background".
    #
    #   "veo"    → Google Veo 3.1 text-to-video. Cinematic, ~$0.50/gen,
    #              60-180s wall clock, but prone to inserting human faces
    #              that fail UMG content validation (incident 2026-05-15
    #              "Lunes Por La Madrugada").
    #   "imagen" → Imagen-4 text-to-image + local Ken Burns animation.
    #              ~$0.03/gen, 5-15s wall clock, controllable composition,
    #              no face-validation failures. Lower visual ambition
    #              than Veo (zoom/pan vs real camera moves) but reliable.
    #
    # Default unset (None) → backend treats as "veo" for backward compat.
    # Frontend EditRequestPanel exposes a segmented toggle near the
    # background_hint field.
    background_mode: str | None = Field(
        default=None,
        pattern="^(veo|imagen)$",
    )
    # Camera/motion register for edit_type=="background". Lets the operator
    # change how the new background moves (incl. "estatico" = locked camera)
    # without typing prose — closes the gap where movement was only
    # selectable in the wizard, never at edit time. None → inherit the
    # job's persisted movement_style. Validated as free-text; normalized
    # downstream by _normalize_movement_style.
    movement_style: str | None = Field(default=None, max_length=64)
    # "Usar mi prompt tal cual": when True (and background_hint is set),
    # the hint goes straight to Veo without Gemini's rewrite. Only
    # meaningful for edit_type=="background".
    bg_verbatim: bool = Field(default=False)
    # FX layer + lyric animations chosen in the wizard. Added 2026-05-22:
    # antes el operador no podía cambiarlos post-upload y, peor, los
    # whitelists de /retry y /variant los descartaban silenciosamente.
    # None → no cambia (heredan de render_params del job). Cadena vacía
    # válida ("" = sin efecto). max_length espeja /generate (3646/3652/3653).
    effect: str | None = Field(default=None, max_length=32)
    lyrics_animation: str | None = Field(default=None, max_length=16)
    line_transition: str | None = Field(default=None, max_length=16)
    # Explicit ack that the caller understands re-syncing lyrics on a job
    # already published to YouTube will update R2 but NOT replace the
    # YouTube video file (the YouTube API doesn't allow file replacement,
    # only metadata). Defaults to False so the API fails closed with a
    # 409 — the frontend prompts the operator, who has to opt in.
    allow_youtube_drift: bool = False
    # PR C 2026-05-26 (feat/edit-metadata): operator can fix a typo in
    # the title card (artist / song_title) without re-uploading. Only
    # used when edit_type=="metadata". max_length matches the DB columns
    # exactly: Job.artist VARCHAR(255), Job.song_title VARCHAR(500).
    # Both nullable — caller may send one, the other, or both.
    artist: str | None = Field(default=None, max_length=255)
    song_title: str | None = Field(default=None, max_length=500)
    # Title-card customization (Full Rotor v1). Operator controls the intro
    # title layout/size/fonts post-upload. None → inherit render_params.
    # Persisted in render_params (not DB columns); ride in the typography
    # edit bucket (fast re-render, no bg/segments touched).
    title_template: str | None = Field(default=None, max_length=16)
    title_size: float | None = None
    title_artist_font: str | None = Field(default=None, max_length=64)
    title_song_font: str | None = Field(default=None, max_length=64)
    # UI v1.1 (2026-05-30): manual song-title line break. "" or None =>
    # keep auto wrap (no change vs current). When the operator chose to
    # split the title in 2 lines, the wizard sends the lines joined by
    # "\n". Persisted in render_params alongside the other title_* keys
    # so retry/variant inherit the choice.
    title_song_break: str | None = Field(default=None, max_length=200)


class EnableProResRequest(BaseModel):
    """Body para POST /enable-prores/{job_id}. Mismos campos que el upload
    UMG. Strings sin parsear — _parse_umg_params se encarga de validar y
    convertir a tipos correctos."""
    umg_frame_size: str = Field(..., max_length=16)      # "HD" | "UHD-4K" | "DCI-4K" | "DCI-2K"
    umg_fps: str = Field(..., max_length=16)             # "23.976"...".60"
    umg_prores_profile: str = Field(..., max_length=4)   # "3" (422 HQ) | "4" (4444) | "5" (4444 XQ)


class DeliverToDriveRequest(BaseModel):
    """Body para POST /jobs/{job_id}/deliver-to-drive."""
    file_type: str = Field(..., max_length=20)  # "umg_master" | "umg_short" | "video" | "short"


@app.post("/approve/{job_id}")
async def approve_job(
    job_id: str,
    body: ApproveJobRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a job after human review, changing status from pending_review to done."""
    from database import Job as JobModel, AuditLog
    from datetime import datetime, timezone

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "pending_review":
        raise HTTPException(status_code=400, detail="Job is not pending review")

    job.status = "done"
    job.approved_by = current_user["id"]
    job.approved_at = datetime.now(timezone.utc)
    job.review_notes = body.notes or None

    # Archivado Fase 1 (2026-06-10): el éxito aprobado archiva los
    # intentos fallidos previos del mismo audio (mismo user+filename) —
    # dejan de ensuciar la historia sin borrarse (audit trail). Misma tx
    # que la aprobación; best-effort: si falla, la aprobación NO se cae.
    _archived_n = 0
    try:
        from jobs import archive_failed_attempts
        _archived_n = archive_failed_attempts(db, keep_job=job)
    except Exception as _arch_exc:
        logger.warning("[ARCHIVE] archive_failed_attempts falló para %s: %s",
                       job_id, _arch_exc)

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.approve",
        detail={"job_id": job_id, "notes": body.notes,
                "archived_failed_attempts": _archived_n},
    ))
    db.commit()

    # 2026-05-30 perf: drop the cached /usage entry for this operator so
    # the sidebar badge reflects the +1 immediately, not after the 30 s
    # TTL. Failure is silent — if Redis is down the next /usage just
    # bypasses the cache and reads the live counter.
    try:
        from cache import invalidate, usage_key
        invalidate(usage_key(current_user["tenant_id"], current_user["id"]))
    except Exception:
        pass

    return {"ok": True, "status": "done", "job_id": job_id}


@app.post("/reject/{job_id}")
async def reject_job(
    job_id: str,
    body: ApproveJobRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reject a job, marking it as rejected."""
    from database import Job as JobModel, AuditLog
    from datetime import datetime, timezone

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "pending_review":
        raise HTTPException(status_code=400, detail="Job is not pending review")

    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.approved_at = datetime.now(timezone.utc)
    job.review_notes = body.notes or None

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.reject",
        detail={"job_id": job_id, "notes": body.notes},
    ))
    db.commit()

    return {"ok": True, "status": "rejected", "job_id": job_id}


@app.get("/jobs/{job_id}/source-audio-url")
async def get_source_audio_url(
    job_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pre-signed URL to the audio for the post-approval lyrics editor.

    Powers the LyricsEditor's <audio> element: when an operator opens
    an existing job to fix sync, the audio plays so the operator can
    align segments to it. Returning a signed URL (instead of proxying
    bytes through uvicorn) keeps the API container free during long
    editor sessions.

    Resolution order:
      1. `input_r2_key` (original uploaded MP3/WAV) — best quality.
      2. `s3_keys["video"]` (rendered HD MP4) — fallback when the
         original is gone (lifecycle GC, very old job, or one of the
         duplicate-job-bug casualties). Browsers play the audio track
         out of an <audio src="...mp4"> just fine, no client change
         needed. The response sets `source="video"` + `fallback=true`
         so the UI can show a "playing audio from rendered video" badge.
      3. `s3_keys["short"]` (vertical/short MP4) — second fallback.
      4. Nothing exists → 404 with a re-upload message.

    HOTFIX FASE 2 — 2026-05-27: previously this only checked
    input_r2_key, so jobs whose original was lost (the duplicate-job
    cascade and the lifecycle-GC purge) became un-editable forever.
    Now agus.cafisi (and any operator with stale jobs) can keep
    correcting lyrics using the rendered video's audio track.

    Owner / same-tenant only — same auth model as /download/<job>/<file>.
    """
    from database import Job as JobModel
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # 1. Try the original audio first (best quality, no render artifacts).
    #
    # HOTFIX 2026-05-27: presign is unconditional (it just signs a URL
    # without HEAD-ing the object), so a row whose input_r2_key points
    # at a deleted R2 file was previously returning a 404'd URL — the
    # editor opened mudo and the MP4 fallback never triggered. We now
    # HEAD-probe before serving so a dead key falls through to the MP4
    # branch correctly. ~50-200 ms cost per editor load is acceptable
    # (one-shot per session) and is exactly the diagnostic agus.cafisi
    # needed for his 26 jobs with set-but-DEAD input_r2_key (lifecycle
    # GC after 30 d retention purged the originals).
    if job.input_r2_key and storage.object_exists(job.input_r2_key):
        url = storage.generate_signed_url(job.input_r2_key, expiry_seconds=3600)
        if url:
            # Audit: this serves a signed URL to the ORIGINAL master the
            # label uploaded — exactly the access a compliance review asks
            # about. Once per editor session (endpoint is not polled).
            _audit_media_access(
                current_user, job_id, "source_audio",
                action="job.source_audio_access", source="input", request=request,
            )
            return {
                "url": url,
                "expires_in": 3600,
                "source": "input",
                "fallback": False,
            }
        # Storage signed-URL helper returned None (storage disabled mid-
        # request, etc.). Fall through to render fallback rather than
        # 503 — if a rendered MP4 exists we can still serve the editor.

    # 2-3. Fallback to rendered MP4(s). Browsers play <audio src=".mp4">.
    #
    # HOTFIX F8 2026-05-27 (audit): probe each candidate with HEAD before
    # presigning. presign is a local operation that doesn't verify the
    # object exists — without this guard, a job whose video MP4 was
    # also purged (e.g., bulk-deleted output, very old job) would receive
    # a signed URL that 404s on fetch. The MP4 fallback's whole point is
    # to provide a working audio source; serving a dead URL defeats it
    # and the 4. final "re-upload" branch never fires.
    s3_keys = job.s3_keys or {}
    if isinstance(s3_keys, dict):
        for source_type in ("video", "short"):
            r2_key = s3_keys.get(source_type)
            if not r2_key:
                continue
            if not storage.object_exists(r2_key):
                continue
            url = storage.generate_signed_url(r2_key, expiry_seconds=3600)
            if url:
                _audit_media_access(
                    current_user, job_id, "source_audio",
                    action="job.source_audio_access", source=source_type, request=request,
                )
                return {
                    "url": url,
                    "expires_in": 3600,
                    "source": source_type,
                    "fallback": True,
                }

    # 4. Neither original nor any rendered MP4 — operator must re-upload.
    # Diagnóstico (2026-07-10, "No Hay Santos"): cuando el editor muestra
    # "Audio no disponible" necesitamos saber DESDE PROD qué candidato
    # falló y por qué (key ausente vs sonda a R2 caída) sin reproducir.
    logger.warning(
        "[SOURCE-AUDIO] 404 for job %s: input_r2_key=%r s3_keys(video)=%r "
        "s3_keys(short)=%r — every candidate missing or unprobeable",
        job_id, job.input_r2_key,
        (job.s3_keys or {}).get("video") if isinstance(job.s3_keys, dict) else None,
        (job.s3_keys or {}).get("short") if isinstance(job.s3_keys, dict) else None,
    )
    raise HTTPException(
        status_code=404,
        detail=(
            "Source audio is not available for this job. "
            "Re-upload the audio file to keep editing the lyrics."
        ),
    )


@app.post("/jobs/{job_id}/restore-audio")
@limiter.limit("20/minute")
async def restore_audio(
    request: Request,
    job_id: str,
    audio: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-upload the original WAV/MP3 for a job whose `input_r2_key`
    points to an object that's no longer in R2 (lifecycle GC, manual
    cleanup, or some other deletion path).

    Use case: the 2026-05-27 cleanup audit found 26 of agus.cafisi's jobs
    with `input_r2_key` set in DB but the R2 object gone. The MP4
    fallback (#418) lets the editor function for lyric correction, but
    re-rendering needs the original WAV. This endpoint accepts the file
    from the owner's local copy and writes it back to the canonical R2
    path (matching the existing `input_r2_key` so no DB key migration
    is needed).

    Validations:
      - Owner or same-tenant only (existing auth model).
      - Filename + magic-bytes check via `_validate_audio_filename_only`
        + `_validate_audio_file_on_disk`.
      - Plan disk quota (`_enforce_disk_capacity`).
      - Max file size (`MAX_UPLOAD_MB`).

    Returns the canonical key + size on success.
    """
    from database import Job as JobModel
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    _validate_audio_filename_only(audio.filename)

    # Determine the target R2 key. If input_r2_key is set, reuse it
    # (this is the common case — the DB row already points where we
    # want to upload, we just need R2 to actually have the object).
    # If NULL (rare), derive the canonical path so future probes find it.
    safe_basename = _safe_basename(audio.filename)
    if job.input_r2_key:
        target_key = job.input_r2_key
    else:
        target_key = storage._input_object_key(
            job.tenant_id, job_id, job.filename or safe_basename,
        )

    _enforce_disk_capacity()

    # Stream to a temp file with size + magic-bytes validation.
    import tempfile
    fd, temp_path = tempfile.mkstemp(prefix=f"restore_{job_id}_", suffix=".bin")
    os.close(fd)
    try:
        size_bytes = await _stream_upload_to_disk(audio, temp_path)
        _validate_audio_file_on_disk(audio.filename, temp_path)

        # Upload to R2 at the target key. We use upload_file (arbitrary
        # key) instead of upload_input (which would re-derive the path)
        # so we keep the EXISTING DB key happy and don't churn it.
        uploaded = storage.upload_file(temp_path, target_key)
        if not uploaded:
            raise HTTPException(status_code=503, detail="R2 unavailable.")
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    # If input_r2_key was NULL before, persist the canonical path now so
    # future reads find the file via the standard `/source-audio-url`
    # path without going through the MP4 fallback.
    if not job.input_r2_key:
        job.input_r2_key = target_key
        db.commit()

    logger.info(
        "[RESTORE-AUDIO] job_id=%s tenant=%s key=%s size_mb=%.1f restored_by_user=%s",
        job_id, job.tenant_id, target_key, size_bytes / 1024 / 1024,
        current_user.get("id"),
    )

    # HOTFIX F4 2026-05-27 (audit): write an AuditLog row for forensics.
    # Restore writes both the DB (input_r2_key when previously NULL) AND
    # the R2 object — higher impact than a title typo. UMG compliance
    # requires a trail. Same pattern as admin.cleanup_inputs / job.edit_request.
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.restore_audio",
        detail={
            "job_id": job_id,
            "key": target_key,
            "size_mb": round(size_bytes / 1024 / 1024, 2),
            "filename": audio.filename,
        },
    ))
    db.commit()

    return {
        "job_id": job_id,
        "key": target_key,
        "size_mb": round(size_bytes / 1024 / 1024, 2),
        "restored": True,
    }


@app.get("/jobs/{job_id}/background-url")
async def get_background_url(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Signed URL to the cached background video (bg_r2_key_cached) so the
    editor's live preview can show lyrics over the REAL background. 404
    when there's no cached background yet (e.g. a job that never rendered),
    in which case the preview falls back to a style-tinted gradient.
    Owner / same-tenant only, same model as /source-audio-url."""
    from database import Job as JobModel
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.bg_r2_key_cached:
        raise HTTPException(status_code=404, detail="No cached background for this job.")
    url = storage.generate_signed_url(job.bg_r2_key_cached, expiry_seconds=3600)
    if not url:
        raise HTTPException(status_code=503, detail="Object storage is unavailable.")
    return {"url": url, "expires_in": 3600}


# NOTE: sync `def` on purpose — librosa.load is CPU/IO-blocking, so FastAPI
# runs this in its threadpool instead of blocking the event loop. An async
# def here would freeze every other request during the (multi-second) first
# compute, which is exactly the saturation failure mode we want to avoid.
@app.get("/jobs/{job_id}/waveform")
def get_waveform(
    job_id: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Downsampled peak envelope of the source audio for the timeline
    editor's waveform. Returns {"peaks": [0..1]*N, "duration": seconds}.

    First call per job downloads the MP3 from R2 and computes the envelope
    with librosa (a few seconds); the result is cached to R2 as
    waveform/{job_id}.json so every later open is a fast object fetch.
    PR feat/waveform-precompute 2026-05-27: the render pipeline now
    pre-computes the envelope when the job flips to done/pending_review,
    so the first operator-facing call to this endpoint is almost always
    a cache hit (~200ms instead of 5-30s cold-cache cost).
    Owner / same-tenant only, same auth model as /source-audio-url.
    """
    from database import Job as JobModel
    from waveform_compute import compute_and_cache_waveform

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.input_r2_key:
        raise HTTPException(
            status_code=404, detail="Source audio is not available for this job."
        )
    if not storage.is_enabled():
        raise HTTPException(status_code=503, detail="Object storage is unavailable.")

    # Delegate cache + compute + cache-write to the shared helper. Pipeline
    # uses the same function post-render so the cache key + payload shape
    # stay in sync across both call paths.
    payload = compute_and_cache_waveform(job.job_id, job.input_r2_key)
    if payload is None:
        # Distinguish the two failure modes the helper bundles together so
        # the frontend can show a useful message. We re-check the source
        # presence (the helper returned None if the audio could not be
        # downloaded) and surface the same 422 the operator was used to.
        raise HTTPException(
            status_code=422,
            detail="El audio original ya no está en storage. Subí el MP3 de nuevo.",
        )

    response.headers["Cache-Control"] = "private, max-age=86400"
    return payload


class SaveSegmentsRequest(BaseModel):
    # Persisted to Job.segments_json (JSONB). Same shape /generate and
    # /edit accept. 5 MB upper bound mirrors /generate's segments_json
    # form-field cap — a long video can legitimately ship a few hundred
    # KB of segments.
    segments: list[dict] = Field(..., max_length=10000)


@app.post("/jobs/{job_id}/save-segments")
@limiter.limit("60/minute")
async def save_segments(
    request: Request,
    job_id: str,
    body: SaveSegmentsRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist user-edited segments while the LyricsEditor is mounted.

    Three reasons this exists:

    1. The reaper's staleness anchor (last_user_activity_at) gets bumped
       every save, so a 90-min batch-edit session doesn't get reaped at
       the 30-min mark. Before this endpoint, segments only touched the
       backend at POST /generate — so the reaper had no signal that the
       user was actively working.
    2. Cross-tab / refresh recovery: if the wizard tab dies mid-batch,
       we can rehydrate the editor from segments_json on the server
       instead of relying on browser sessionStorage (which is per-tab).
    3. Post-approval lyrics fixes: the same LyricsEditor is reused inside
       the /edit modal on pending_review jobs. The operator typing a
       correction (e.g. "de la amor" → "del amor") needs that change
       persisted before any subsequent re-render reads `segments_json`.

    Allowed statuses are the ones where the LyricsEditor is operationally
    mounted. Other statuses (done, error, processing, queued, awaiting_upload)
    have their own write paths.

    Validates ownership the same way as /generate's reuse path.
    """
    from jobs import get_job_model, touch_user_activity

    job = get_job_model(db, job_id)
    if (not job
            or job.user_id != current_user["id"]
            or job.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")

    # Wizard (transcribed_pending) is the original use case; pending_review
    # / rejected enable the post-approval /edit modal's autosave so text
    # corrections persist even if the operator never clicks "Apply lyrics"
    # explicitly. `editing` covers the transient state during a /edit run.
    # Incident 2026-05-15: Bersuit's "enfermera del amor" lyric reverted
    # to "de la amor" on 3 of 4 occurrences after a background re-render
    # because autosave 409'd against pending_review here.
    #
    # `done` added 2026-05-19: JobDetail presents a LyricsEditor on done
    # jobs (see EditRequestPanel allowedModes for done/rejected), so the
    # operator can fix typos on already-shipped videos. Autosave needs to
    # write through to segments_json before the operator clicks
    # "Re-renderizar" — without this, every keystroke 409'd and the
    # corrections were lost. The endpoint only writes segments_json + bumps
    # last_user_activity_at; it never mutates status, so allowing it on
    # done has no pipeline-state side effects. The actual re-render still
    # goes through POST /edit with edit_type="lyrics" which transitions
    # the job to editing.
    # `transcribed` lives in the whitelist for the same reason as
    # /generate (see 2026-05-25 worker-state-drift fix): older async
    # jobs were persisted with status='transcribed' literal. Newer
    # jobs use 'transcribed_pending'. Editor must work on both.
    _SAVE_SEGMENTS_ALLOWED = (
        "transcribed_pending", "transcribed", "pending_review", "rejected", "editing", "done",
    )
    if job.status not in _SAVE_SEGMENTS_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"save-segments requires status in {_SAVE_SEGMENTS_ALLOWED} "
                f"(current: {job.status})"
            ),
        )

    segs = body.segments or []
    # Light shape check — full validation lives in /generate's pipeline.
    # We just want to reject obviously broken payloads early so the
    # autosave doesn't silently store garbage.
    #
    # SECURITY (incident 2026-05-24 audit): also reject non-finite floats
    # (NaN/Infinity). Python `json` accepts them, but Postgres JSONB
    # rejects on insert → opaque 500. Sorting NaN is undefined in Timsort
    # so a NaN start corrupts the entire timeline order. And cap text
    # size at 2 KB per line: anything bigger is a paste error or DoS, and
    # libass wraps badly past that anyway.
    import math
    _MAX_TEXT_CHARS = 2000   # per-segment text cap
    _MAX_END_S = 24 * 3600   # 24h ceiling — songs are seconds, not days
    for i, seg in enumerate(segs):
        if not isinstance(seg, dict):
            raise HTTPException(status_code=400, detail=f"segments[{i}] must be an object")
        for k in ("start", "end", "text"):
            if k not in seg:
                raise HTTPException(
                    status_code=400,
                    detail=f"segments[{i}] missing required key {k!r}",
                )
        try:
            start_f = float(seg["start"])
            end_f = float(seg["end"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"segments[{i}] start/end must be numeric",
            )
        if not (math.isfinite(start_f) and math.isfinite(end_f)):
            raise HTTPException(
                status_code=400,
                detail=f"segments[{i}] start/end must be finite (NaN/Infinity rejected)",
            )
        if start_f < 0 or end_f < 0 or end_f > _MAX_END_S or start_f > end_f:
            raise HTTPException(
                status_code=400,
                detail=f"segments[{i}] start/end out of range (need 0 ≤ start ≤ end ≤ {_MAX_END_S}s)",
            )
        text = seg.get("text", "")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail=f"segments[{i}].text must be a string")
        if len(text) > _MAX_TEXT_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"segments[{i}].text too long ({len(text)} chars > {_MAX_TEXT_CHARS})",
            )

    # Sort by start ascending so downstream consumers (renderer, sync-mode
    # neighbor clamp, lookup-by-cronological-position) can assume a
    # monotonic timeline. The frontend editor can submit out-of-order
    # arrays — e.g. when the operator clicks "Agregar línea" mid-song,
    # the new line gets appended to the end of the React state array
    # even though its `start` belongs in the middle. Without this sort,
    # the next reload of the editor showed a jumbled list and the sync
    # cursor / clamp logic referenced the wrong neighbors. Origin: Una
    # Vez Más — Viejas Locas (agus.cafisi, 2026-05-18).
    segs = sorted(segs, key=lambda s: float(s.get("start", 0) or 0))

    # Audit log of what changed between prev and new — only when non-empty.
    # Motivation: operator (Tomas, 2026-05-19) reported "lines change places"
    # in autosync, and we had ZERO way to reconstruct what happened (only
    # the final sorted segments_json was persisted). This block writes a
    # compact diff per save so future complaints are diagnosable.
    # Capped to keep payload small (20 changed entries max with `truncated`
    # flag if exceeded).
    try:
        from database import AuditLog
        prev_segs = job.segments_json if isinstance(job.segments_json, list) else []
        # Build id-keyed maps so we can diff by stable _id (frontend assigns
        # one) — fall back to positional index for legacy rows missing _id.
        def _key(s, idx):
            return s.get("_id") if isinstance(s, dict) and "_id" in s else f"idx_{idx}"
        prev_by_key = { _key(s, i): (i, s) for i, s in enumerate(prev_segs) }
        new_by_key  = { _key(s, i): (i, s) for i, s in enumerate(segs) }
        changed = []
        reorder = []
        for k, (new_idx, ns) in new_by_key.items():
            prev = prev_by_key.get(k)
            if prev is None:
                continue
            prev_idx, ps = prev
            # Field-level diff on the three meaningful values.
            ps_start = float(ps.get("start") or 0)
            ns_start = float(ns.get("start") or 0)
            ps_end = float(ps.get("end") or 0)
            ns_end = float(ns.get("end") or 0)
            ps_text = (ps.get("text") or "").strip()
            ns_text = (ns.get("text") or "").strip()
            if (abs(ps_start - ns_start) > 0.05 or abs(ps_end - ns_end) > 0.05
                    or ps_text != ns_text):
                changed.append({
                    "id": k,
                    "prev_start": round(ps_start, 3),
                    "new_start": round(ns_start, 3),
                    "prev_end": round(ps_end, 3),
                    "new_end": round(ns_end, 3),
                    "prev_text": ps_text[:120],
                    "new_text": ns_text[:120],
                })
            if prev_idx != new_idx:
                reorder.append({"id": k, "from_idx": prev_idx, "to_idx": new_idx})

        if changed or reorder:
            truncated = False
            if len(changed) > 20:
                changed = changed[:20]
                truncated = True
            if len(reorder) > 30:
                reorder = reorder[:30]
                truncated = True
            db.add(AuditLog(
                user_id=current_user["id"],
                action="lyrics.segments_diff",
                detail={
                    "job_id": job_id,
                    "n_lines": len(segs),
                    "changed": changed,
                    "reorder": reorder,
                    "truncated": truncated,
                },
            ))
    except Exception as e:
        # Audit logging is best-effort — never break the save flow.
        logger.warning("[save-segments] audit log failed: %s", e)

    job.segments_json = segs
    touch_user_activity(db, job)
    db.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "saved_at": job.last_user_activity_at.isoformat() if job.last_user_activity_at else None,
        "count": len(segs),
    }


@app.post("/edit/{job_id}")
async def request_edit(
    job_id: str,
    body: EditJobRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Request a partial re-render of a job that is pending_review.

    edit_type="typography": re-render with new font/size/case settings.
        Reuses the cached background from R2 — no AI cost.
    edit_type="background": regenerate Veo background only, keep segments.
        Costs ~$0.90 (Veo + validation).

    Limited to 3 edits per job. After the 3rd edit the reviewer must
    approve or reject — no further edits are allowed.
    """
    from database import Job as JobModel, AuditLog
    from pipeline import _MAX_EDITS

    # with_for_update() toma row-level lock en Postgres para serializar
    # el read-validate-write de edit_count. Sin esto, dos POST /edit del
    # mismo job en rápida sucesión leen el mismo edit_count, ambos pasan
    # el check < _MAX_EDITS, y ambos incrementan → user excede el límite
    # de 3 edits y la app cobra Veo extra (~$0.90 por background regen).
    # No-op en SQLite (igual que _lock_user_for_quota); lock real en
    # Postgres. Se libera con db.commit() al final del flow.
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .with_for_update()
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    valid_edit_types = ("typography", "background", "lyrics", "metadata")
    if body.edit_type not in valid_edit_types:
        raise HTTPException(
            status_code=400,
            detail=f"edit_type must be one of {valid_edit_types}",
        )
    # Escenas (incidente 2026-07-01, job 53b9513225b1): el edit "background"
    # es del mundo fondo-único — para un job multi-escena generaba UN clip
    # Veo de 8 s, PISABA bg_r2_key_cached (que era el timeline completo) y
    # re-renderizaba video+short loopeando esa única escena. El camino
    # correcto para estos jobs es la regeneración por escena del filmstrip
    # (edit_type="scene" vía /edit-scene, no consume cupo de edición).
    if body.edit_type == "background":
        _sp = job.scene_plan if isinstance(job.scene_plan, dict) else None
        if _sp and _sp.get("scenes"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Este video usa Escenas: el fondo es un timeline "
                    "multi-escena. Regenerá la escena que quieras cambiar "
                    "desde el filmstrip del video (no consume cupo de "
                    "edición)."
                ),
            )

    # Status gate. Lyrics and metadata edits accept a wider set of
    # terminal-ish states so users can fix typos/timing on videos that
    # already finished rendering (done, in approval queue, or even
    # rejected) without having to re-upload the MP3. typography/background
    # stay strict — they're billed as "edits in the review loop" and only
    # make sense while the reviewer is still deciding.
    if body.edit_type in ("lyrics", "metadata"):
        allowed = ("done", "pending_review", "rejected")
        if job.status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{body.edit_type.capitalize()} edit requires the job to be done, "
                    f"pending_review, or rejected (current: {job.status})"
                ),
            )
        # PR C 2026-05-26: metadata-specific validation. The handler must
        # have at least one of artist/song_title set AND non-empty after
        # trim; otherwise the re-render does nothing visible to the user.
        if body.edit_type == "metadata":
            new_artist = body.artist.strip() if body.artist is not None else None
            new_title = body.song_title.strip() if body.song_title is not None else None
            if new_artist is None and new_title is None:
                raise HTTPException(
                    status_code=400,
                    detail="metadata edit requires at least one of 'artist' or 'song_title'",
                )
            if (new_artist is not None and not new_artist) or (
                new_title is not None and not new_title
            ):
                raise HTTPException(
                    status_code=400,
                    detail="metadata fields must be non-empty after trimming whitespace",
                )
        # YouTube API does not allow replacing an uploaded video's file
        # (only metadata). Re-syncing lyrics OR re-rendering the title
        # card on a published job would update R2 silently while YouTube
        # continued serving the old cut. Fail closed; the frontend
        # prompts the operator and retries with allow_youtube_drift=true.
        if job.youtube_data and not body.allow_youtube_drift:
            yt_url = (
                job.youtube_data.get("url")
                if isinstance(job.youtube_data, dict) else None
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "youtube_already_published",
                    "message": (
                        f"Job already published to YouTube. {body.edit_type.capitalize()} "
                        f"edit will update the file in our platform but NOT "
                        f"replace the YouTube video. Pass "
                        f"allow_youtube_drift=true to proceed anyway."
                    ),
                    "youtube_url": yt_url,
                },
            )
    elif job.status != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=f"Job must be in pending_review to request edits (current: {job.status})",
        )

    current_edit_count = job.edit_count or 0
    # Admins are exempt from the per-job edit cap (operators QA'ing a
    # render may need more than _MAX_EDITS passes). Regular users still
    # hit the limit and must approve/reject.
    #
    # PR C 2026-05-26: metadata edits do NOT consume an edit slot. A typo
    # in the operator's own metadata is not the same as an aesthetic
    # iteration over the video — penalizing one of the 3 limited edits
    # for "fix the tilde" would frustrate operators who already spent
    # their slots on typography/background/lyrics. AuditLog still records
    # the metadata edit for traceability (`metadata_only=True`).
    _is_admin = current_user.get("role") == "admin"
    _metadata_only = body.edit_type == "metadata"
    if not _is_admin and not _metadata_only and current_edit_count >= _MAX_EDITS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum edit limit ({_MAX_EDITS}) reached. Please approve or reject.",
        )

    # Typography, lyrics, and metadata reuse the cached background. Without
    # bg_r2_key_cached set, the worker can't avoid re-running Veo —
    # which defeats the point of these fast-path edits.
    if body.edit_type in ("typography", "lyrics", "metadata") and not job.bg_r2_key_cached:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No cached background available for {body.edit_type} edit. "
                "Use edit_type='background' to regenerate it."
            ),
        )

    # Segments handling. Two cases:
    #
    #   - edit_type=lyrics: caller MUST send segments (it's the whole point
    #     of the edit), and we forward them via edit_params so the worker
    #     can use them as the explicit override without a DB roundtrip.
    #
    #   - edit_type=background / typography: caller MAY send segments. If
    #     they do (operator was editing text inside the modal alongside
    #     other changes), we persist them to segments_json BEFORE the
    #     enqueue so `run_edit_pipeline`'s `else: segments = job.segments_json`
    #     branch (pipeline.py:6309) reads the corrected text. Incident
    #     2026-05-15: Bersuit lyric "de la amor" → "del amor" was silently
    #     dropped on every background re-render because /edit only persisted
    #     segments for the lyrics path. Operator's edits lived in the
    #     LyricsEditor's local state and never reached the worker.
    #
    # Either way, shape validation runs once if segments are present.
    if body.edit_type == "lyrics" and (not body.segments or len(body.segments) == 0):
        raise HTTPException(
            status_code=400,
            detail="Lyrics edit requires 'segments' in the request body (non-empty list).",
        )
    if body.segments and len(body.segments) > 0:
        for i, seg in enumerate(body.segments):
            if not isinstance(seg, dict):
                raise HTTPException(status_code=400, detail=f"segments[{i}] must be an object")
            for k in ("start", "end", "text"):
                if k not in seg:
                    raise HTTPException(status_code=400, detail=f"segments[{i}] missing '{k}'")
            try:
                if float(seg["start"]) < 0 or float(seg["end"]) <= float(seg["start"]):
                    raise HTTPException(
                        status_code=400,
                        detail=f"segments[{i}] has invalid timing (start={seg['start']}, end={seg['end']})",
                    )
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"segments[{i}] start/end must be numbers",
                )
    elif body.edit_type != "lyrics" and not job.segments_json:
        raise HTTPException(
            status_code=400,
            detail="Job has no persisted transcription. Cannot re-render.",
        )

    # Normalize segments once (used both for persistence and the lyrics
    # edit_params payload).
    normalized_segments = None
    if body.segments and len(body.segments) > 0:
        normalized_segments = [
            {
                "start": float(s["start"]),
                "end": float(s["end"]),
                "text": str(s["text"]),
                # Preserve the manual-timing lock set in the visual Timings
                # editor. Without this, a lyrics re-render strips `locked`
                # and pipeline._apply_display_timing re-applies hold-until-next,
                # clobbering the operator's hand-set end. Only carry it when
                # truthy so untouched lines stay clean.
                **({"locked": True} if s.get("locked") else {}),
                # Preserve per-line layout overrides set in the live preview
                # (position / size / rotation). Same reason as `locked`: a
                # re-render must not strip the operator's layout. Only carried
                # when set to a non-default value so untouched lines stay clean.
                **({"pos": {"x": float(s["pos"]["x"]), "y": float(s["pos"]["y"])}}
                   if isinstance(s.get("pos"), dict)
                   and "x" in s["pos"] and "y" in s["pos"] else {}),
                **({"scale": float(s["scale"])}
                   if isinstance(s.get("scale"), (int, float))
                   and float(s["scale"]) != 1.0 else {}),
                **({"rot": float(s["rot"])}
                   if isinstance(s.get("rot"), (int, float))
                   and float(s["rot"]) != 0.0 else {}),
                # Preserve per-word timestamps (forced-align / whisperX) so a
                # re-render or future word-level/karaoke editor doesn't lose
                # them. The line-level editor ignores `words`; carried only
                # when present so untouched line-level edits stay lean.
                **({"words": s["words"]} if isinstance(s.get("words"), list) else {}),
            }
            for s in body.segments
        ]
        # Persist immediately so any subsequent reader (worker, /status
        # poll, the operator opening another tab) sees the corrected text.
        # The /edit handler is the right place for this — it's already
        # mutating the row a few lines below.
        # Audit 2026-05-26: capture the pre-edit segments so the enqueue
        # rollback below can restore them. Without this, if the enqueue
        # fails (Redis down) we revert status/edit_count but leave the new
        # segments persisted — the operator's "Reintentar edit" later sees
        # the new lyrics applied even though the edit was never processed.
        _pre_edit_segments = job.segments_json
        job.segments_json = normalized_segments

    edit_params: dict = {}
    if body.font is not None:
        edit_params["font"] = body.font
    if body.font_scale is not None:
        edit_params["font_scale"] = body.font_scale
    if body.text_case is not None:
        edit_params["text_case"] = body.text_case
    if body.frame_format is not None:
        edit_params["frame_format"] = body.frame_format
    if body.text_contrast is not None:
        edit_params["text_contrast"] = body.text_contrast
    # Title-card customization (Full Rotor v1) — durable visual choices, same
    # pattern as effect/lyrics_animation: forward to edit_params for THIS
    # render AND persist to render_params so retries/variants inherit them.
    if body.title_template is not None:
        _tt = (body.title_template or "").strip() or "auto"
        if _tt not in ("auto", "centered", "lower_third", "badge"):
            _tt = "auto"
        edit_params["title_template"] = _tt
        _rp = dict(job.render_params or {})
        _rp["title_template"] = _tt
        job.render_params = _rp
    if body.title_size is not None:
        _ts = max(0.5, min(2.0, float(body.title_size)))
        edit_params["title_size"] = _ts
        _rp = dict(job.render_params or {})
        _rp["title_size"] = _ts
        job.render_params = _rp
    if body.title_artist_font is not None:
        _taf = (body.title_artist_font or "").strip()
        edit_params["title_artist_font"] = _taf
        _rp = dict(job.render_params or {})
        _rp["title_artist_font"] = _taf
        job.render_params = _rp
    if body.title_song_font is not None:
        _tsf = (body.title_song_font or "").strip()
        edit_params["title_song_font"] = _tsf
        _rp = dict(job.render_params or {})
        _rp["title_song_font"] = _tsf
        job.render_params = _rp
    # UI v1.1 (2026-05-30): manual song-title line break. "" = auto wrap
    # (legacy). When set, the operator picked the 2 lines explicitly in the
    # wizard. Same pattern as the other title_* fields.
    if body.title_song_break is not None:
        _tsb = (body.title_song_break or "")
        edit_params["title_song_break"] = _tsb
        _rp = dict(job.render_params or {})
        _rp["title_song_break"] = _tsb
        job.render_params = _rp
    # body.lyric_transition + body.text_motion: campos eliminados 2026-05-23.
    # FX layer + lyric animations: durable visual choices, no edit_type gate
    # (the operator can change them inside any edit modal). Same pattern as
    # movement_style below: forward to edit_params for THIS render AND persist
    # to render_params so retries / variantes los heredan correctamente.
    # 2026-05-22: cerraba el bug donde lyrics_animation/line_transition no
    # estaban en los whitelists de /retry y /variant.
    if body.effect is not None:
        _fx = (body.effect or "").strip()
        edit_params["effect"] = _fx
        _rp = dict(job.render_params or {})
        _rp["effect"] = _fx
        job.render_params = _rp
    if body.lyrics_animation is not None:
        _la = (body.lyrics_animation or "").strip() or "none"
        edit_params["lyrics_animation"] = _la
        _rp = dict(job.render_params or {})
        _rp["lyrics_animation"] = _la
        job.render_params = _rp
    if body.line_transition is not None:
        _lt = (body.line_transition or "").strip() or "none"
        edit_params["line_transition"] = _lt
        _rp = dict(job.render_params or {})
        _rp["line_transition"] = _lt
        job.render_params = _rp
    if body.edit_type == "lyrics":
        # Lyrics path keeps explicit edit_params hand-off so the worker
        # doesn't re-query the DB for segments it already received in
        # the API call.
        edit_params["segments"] = normalized_segments
    if body.edit_type == "background" and body.background_hint and body.background_hint.strip():
        # Operator's free-form description of what they want the new
        # background to convey. Forwarded to Gemini's user_content as a
        # high-priority override block so it pisa los defaults that
        # produced the rejected background.
        _hint = body.background_hint.strip()
        edit_params["background_hint"] = _hint
        # ALSO persist the hint into the DURABLE render_params, not only
        # the transient edit_params that the worker consumes for this one
        # render. Without this, the operator's prompt lives for exactly one
        # render: if the job is later reaped (Railway restart / OOM) and a
        # /retry recovers it, /retry forwards job.render_params (PR #229)
        # but the hint was never stored there — so Gemini re-chooses freely
        # and the operator's direction silently vanishes. Real loss: Amanda
        # Pujó "Ser Anti" 2026-05-20 reverted to the alley cliché after a
        # reaped background edit retried without the hint. /variant already
        # persists its hint to render_params; this brings /edit in line.
        _rp = dict(job.render_params or {})
        _rp["background_hint"] = _hint
        job.render_params = _rp
    if body.edit_type == "background" and body.background_mode in ("veo", "imagen"):
        # Operator picked the generation mode (Veo cinematic video vs
        # Imagen-4 still + Ken Burns animation). Pydantic already
        # validated the enum via pattern; we just forward through
        # edit_params to run_edit_pipeline → _ensure_background.
        edit_params["background_mode"] = body.background_mode
    if body.edit_type == "background" and body.movement_style is not None:
        # Camera/motion register chosen in the editor (incl. "estatico").
        # Forward for this render AND persist to durable render_params — same
        # reaped-retry durability rationale as background_hint above, and so
        # a subsequent "Regenerar fondo" pre-fills the operator's last choice.
        _mv = (body.movement_style or "").strip()
        edit_params["movement_style"] = _mv
        _rp_mv = dict(job.render_params or {})
        _rp_mv["movement_style"] = _mv
        job.render_params = _rp_mv
    if body.edit_type == "background":
        # "Usar mi prompt tal cual" — send background_hint straight to Veo.
        # ALWAYS write the boolean (not only when True) so unchecking the
        # toggle on a later background edit clears a previously-persisted
        # True. Symmetric with movement_style above. The frontend always
        # sends bg_verbatim for background edits. Persisted durably so a
        # reaped /retry (whitelist includes bg_verbatim) honours it too.
        _bv = bool(body.bg_verbatim)
        edit_params["bg_verbatim"] = _bv
        _rp_v = dict(job.render_params or {})
        _rp_v["bg_verbatim"] = _bv
        job.render_params = _rp_v
    if body.edit_type == "background":
        # Persist the CURRENT mutually-exclusive choice. The worker resolves
        # policy from durable render_params; leaving a stale bypass there made
        # a later safe edit silently behave as unrestricted.
        _rp_policy = dict(job.render_params or {})
        _rp_policy.pop("bypass_content_validation", None)
        _rp_policy.pop("force_content_validation", None)
        if body.force_content_validation:
            edit_params["force_content_validation"] = True
            _rp_policy["force_content_validation"] = True
        elif body.bypass_content_validation:
            edit_params["bypass_content_validation"] = True
            _rp_policy["bypass_content_validation"] = True
        else:
            # Safe default, including legacy clients that send neither flag.
            edit_params["force_content_validation"] = True
            _rp_policy["force_content_validation"] = True
        job.render_params = _rp_policy
    # QA fix 2026-05-28 (edit-wizard consolidation): artist/song_title
    # mutations ungated across edit_types. Before this, the fields only
    # applied on edit_type=metadata; if the frontend sent a consolidated
    # POST (e.g. edit_type=lyrics carrying a corrected title), the title
    # silently dropped. The frontend's edit-wizard now bundles ALL diffs
    # into ONE POST with the highest-priority edit_type to dodge the
    # status gate (which 400'd when the previous loop fired a second
    # POST while job.status was still "editing"). Backend needs to apply
    # both axes.
    #
    # Validation: for explicit edit_type=metadata, at least one of artist
    # /song_title must be non-empty after trim (line ~7404 — unchanged).
    # For other edit_types, artist/song_title are OPTIONAL — apply when
    # set + non-empty, ignore otherwise.
    _new_artist = body.artist.strip() if body.artist is not None else None
    _new_title = body.song_title.strip() if body.song_title is not None else None
    _pre_edit_artist = job.artist
    _pre_edit_song_title = job.song_title
    if _new_artist:
        job.artist = _new_artist
        edit_params["artist"] = _new_artist
    if _new_title:
        job.song_title = _new_title
        edit_params["song_title"] = _new_title

    # PR C 2026-05-26: metadata edits do NOT bump edit_count (see
    # rationale at the edit-cap gate above). All other types still
    # consume a slot.
    new_edit_count = current_edit_count if _metadata_only else current_edit_count + 1

    # Pre-flight check that the edit will be able to source its audio.
    # The worker (run_edit_pipeline) resolves audio in two tiers: the
    # original input in R2, and — when that was purged (cleanup_old_inputs,
    # sibling delete) — extracting the track from a rendered deliverable
    # (video/short). This gate must mirror BOTH tiers: blocking on the
    # input alone rejected perfectly recoverable edits with "Subí el MP3
    # de nuevo" (2026-07-10, job 53b9513225b1 "No Hay Santos" — the lyrics
    # edit that re-stitches a damaged scene timeline was blocked even
    # though its rendered MP4 was alive and the worker would have
    # recovered the audio from it). Only 422 when NEITHER tier can work,
    # which is the case the 2026-05-19 agus.cafisi incident was about.
    try:
        import storage as _storage
        if _storage.is_enabled():
            _has_input = bool(
                job.input_r2_key and _storage.object_exists(job.input_r2_key)
            )
            _s3 = job.s3_keys if isinstance(job.s3_keys, dict) else {}
            _has_deliverable = any(
                _s3.get(_k) and _storage.object_exists(_s3[_k])
                for _k in ("video", "short")
            )
            if not _has_input and not _has_deliverable:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "El audio original ya no está en storage y no hay un "
                        "video renderizado del cual recuperarlo. "
                        "Subí el MP3 de nuevo para regenerar el video."
                    ),
                )
            if not _has_input:
                logger.info(
                    "[EDIT] job %s: input %r ausente en R2 — el worker recuperará "
                    "el audio del deliverable (tier-2)", job_id, job.input_r2_key,
                )
    except HTTPException:
        raise
    except Exception as _exc:
        logger.warning(
            "[EDIT] R2 pre-check failed for %s key=%r — proceeding anyway: %s",
            job_id, job.input_r2_key, _exc,
        )

    # Flip to editing immediately so the UI can show progress.
    job.status = "editing"
    job.edit_count = new_edit_count
    # Both typography and lyrics edits jump straight into the video
    # compositing step (cached bg reused). Only background edit goes
    # back through Veo, which is the `background` step.
    job.current_step = "background" if body.edit_type == "background" else "video"
    job.progress = 0
    # Stamp the moment editing began so the reaper can spot edits that
    # died mid-render (worker killed by Railway deploy / OOM / crash).
    # Without this the reaper has to guess from created_at, which is wrong
    # for lyrics edits on already-old done/rejected jobs.
    job.editing_started_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.edit_request",
        detail={
            "job_id": job_id,
            "edit_type": body.edit_type,
            "edit_params": edit_params,
            "edit_count": new_edit_count,
            # PR C 2026-05-26: flag that distinguishes metadata-only
            # edits from regular ones in the audit trail. UMG compliance
            # cares about every artist/title mutation; this lets ops
            # filter the log without parsing edit_params.
            "metadata_only": _metadata_only,
        },
    ))
    # HOTFIX F1 2026-05-27 (audit): the pre-edit capture moved UP to
    # before the in-memory mutation (search "_pre_edit_artist =" above).
    # The old capture here was a no-op because it read AFTER the
    # job.artist assignment.
    db.commit()

    try:
        enqueue_edit(
            job_id=job_id,
            edit_type=body.edit_type,
            edit_params=edit_params,
            plan=current_user.get("plan", "100"),
            tenant_id=current_user.get("tenant_id", ""),
        )
    except Exception as exc:
        # Enqueue failed (Redis down, unexpected RQ error). Roll back the DB
        # to pending_review so the user can retry without waiting for the reaper.
        logger.error("enqueue_edit failed for %s: %s", job_id, exc)
        job.status = "pending_review"
        job.edit_count = current_edit_count
        job.editing_started_at = None
        job.progress = 100
        job.current_step = "thumbnail"
        # Audit 2026-05-26: restore the pre-edit segments_json. The edit
        # handler optimistically persists the new segments BEFORE
        # enqueueing (so the worker reads the latest lyrics), but if the
        # enqueue fails the worker never runs — leaving the modified
        # lyrics persisted is a lie. The operator would re-open the editor
        # and see edits that were never actually applied to a video.
        try:
            job.segments_json = _pre_edit_segments
        except NameError:
            # Defensive: _pre_edit_segments is only assigned when
            # body.segments was non-empty. Non-lyrics edits (typography,
            # background) skip that branch and don't need the rollback.
            pass
        # PR C 2026-05-26: same rollback for metadata. If the enqueue
        # never landed, the visible artist/song_title in JobDetail would
        # lie — operator clicks "Guardar título", error, but UI still
        # shows the new title. Restore the original.
        try:
            job.artist = _pre_edit_artist
            job.song_title = _pre_edit_song_title
        except NameError:
            pass
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Cola de trabajos no disponible. Intentá de nuevo en unos segundos.",
        )

    return {
        "ok": True,
        "job_id": job_id,
        "edit_type": body.edit_type,
        "edit_count": new_edit_count,
        "edits_remaining": max(0, _MAX_EDITS - new_edit_count),
        "edit_limit_exempt": _is_admin,
    }


class RegenerateSceneRequest(BaseModel):
    """Body de POST /jobs/{job_id}/scenes/{recurrence_key}/regenerate.

    Sin campos = "otra toma" (mismo prompt, semilla nueva → otra versión).
    `prompt` reemplaza el prompt de la escena; `hint` lo re-deriva heredando la
    biblia. `movement_style` ∈ estatico|sutil|dinamico."""
    prompt: str | None = Field(default=None, max_length=2000)
    hint: str | None = Field(default=None, max_length=2000)
    movement_style: str | None = Field(default=None, max_length=16)
    allow_youtube_drift: bool = False


def _scene_reroll_max() -> int:
    """Cuántos re-rolls gratis por escena antes de frenar (anti-abuso).

    Presupuesto PROPIO del re-roll de escena, separado del _MAX_EDITS de las
    ediciones caras (letra/fondo). Un re-roll cuesta ~1 clip Veo (~US$0.80),
    así que es generoso. Env SCENE_REROLL_MAX (default 5). Admin exento."""
    try:
        return max(1, int(os.environ.get("SCENE_REROLL_MAX", "5")))
    except (TypeError, ValueError):
        return 5


@app.post("/jobs/{job_id}/scenes/{recurrence_key}/regenerate")
async def regenerate_scene(
    job_id: str,
    recurrence_key: str,
    body: RegenerateSceneRequest | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Regenera UNA escena del fondo multi-escena y re-renderiza el video.

    Cuesta ~1 clip Veo (~US$0.80) y NO consume el cupo de ediciones caras
    (_MAX_EDITS): tiene su propio presupuesto por escena (SCENE_REROLL_MAX).
    Sólo la escena pedida toca Veo; las demás re-bajan de la caché R2. Si la
    escena es recurrente (un coro), regenerarla cambia TODAS sus apariciones.
    """
    from pipeline import _MAX_EDITS  # sólo para reportar edits_remaining (cupo de ediciones caras); el re-roll NO lo consume
    from database import Job as JobModel, AuditLog

    body = body or RegenerateSceneRequest()
    if not has_scenes_access(current_user):
        raise HTTPException(status_code=403, detail="Escenas no habilitado para esta cuenta.")

    # Row-lock para serializar el read-validate-write de edit_count (igual que
    # /edit: dos regen del mismo job no deben saltar el cap ni cobrar Veo extra).
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .with_for_update()
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    plan = job.scene_plan if isinstance(job.scene_plan, dict) else None
    if not plan or not plan.get("scenes"):
        raise HTTPException(status_code=400, detail="Este job no es multi-escena.")
    if not any(s.get("recurrence_key") == recurrence_key for s in plan["scenes"]):
        raise HTTPException(status_code=404, detail=f"Escena '{recurrence_key}' no existe en este job.")

    # Estado: la corrección de escena es un arreglo post-hoc, así que se admite
    # en videos terminados/en revisión (igual criterio que lyrics/metadata).
    allowed = ("done", "pending_review", "rejected")
    if job.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"La escena se puede regenerar con el job done, pending_review o rejected (actual: {job.status}).",
        )

    # YouTube ya publicado: re-renderizar cambia el archivo en la plataforma
    # pero NO reemplaza el video de YouTube. Fail-closed salvo override.
    if job.youtube_data and not body.allow_youtube_drift:
        yt_url = job.youtube_data.get("url") if isinstance(job.youtube_data, dict) else None
        raise HTTPException(status_code=409, detail={
            "code": "youtube_already_published",
            "message": ("El job ya está publicado en YouTube. Regenerar la escena actualiza "
                        "el archivo acá pero NO reemplaza el video de YouTube. Pasá "
                        "allow_youtube_drift=true para continuar."),
            "youtube_url": yt_url,
        })

    _is_admin = current_user.get("role") == "admin"
    # Re-roll de escena: presupuesto PROPIO, desacoplado del _MAX_EDITS de las
    # ediciones caras (letra/fondo). Corregir una escena fea es barato (~1 clip
    # Veo) y debe ser generoso. Contamos los re-rolls previos de ESTA escena en
    # el audit log (append-only) — sin columna nueva ni clobber del scene_plan
    # que el worker reescribe. Cap por escena, env-tunable, admin exento.
    #
    # `detail` es JSONB vía TypeDecorator y NO soporta `.astext` en la expresión
    # SQL (rompía con AttributeError → 500 en CADA re-roll). Filtramos `action`
    # en SQL (indexado) y matcheamos job_id/recurrence_key en Python: la acción
    # es manual y poco frecuente, así que el volumen es chico.
    _prior_rerolls = sum(
        1
        for (_d,) in db.query(AuditLog.detail)
        .filter(AuditLog.action == "job.scene_regenerate")
        .all()
        if isinstance(_d, dict)
        and _d.get("job_id") == job_id
        and _d.get("recurrence_key") == recurrence_key
    )
    _reroll_cap = _scene_reroll_max()
    # Si la escena AÚN está fallada (ningún clip bueno todavía, se está sirviendo
    # una escena sustituta), NO aplicar el cap: el operador tiene que poder
    # seguir intentando arreglarla. El cap solo frena los re-rolls "por gusto" de
    # una escena que YA salió bien. Antes: N intentos fallidos por un Veo caído
    # transitorio lockeaban la escena para siempre (audit escrito por intento).
    _target = next((s for s in plan["scenes"] if s.get("recurrence_key") == recurrence_key), None)
    _scene_succeeded = (_target or {}).get("status") != "failed"
    if not _is_admin and _scene_succeeded and _prior_rerolls >= _reroll_cap:
        raise HTTPException(
            status_code=400,
            detail=f"Llegaste al máximo de regeneraciones de esta escena ({_reroll_cap}). Editá el prompt o aprobá el job.",
        )
    if not job.input_r2_key:
        raise HTTPException(status_code=422, detail="Audio original no disponible — no se puede re-renderizar.")

    _mv = (body.movement_style or "").strip()
    edit_params = {
        "scene_key": recurrence_key,
        "scene_prompt": (body.prompt or "").strip(),
        "scene_hint": (body.hint or "").strip(),
        "scene_movement": _mv if _mv in ("estatico", "sutil", "dinamico") else "",
    }

    # Nota: el re-roll de escena NO incrementa job.edit_count — tiene su propio
    # cupo (SCENE_REROLL_MAX, contado vía audit log arriba).
    job.status = "editing"
    job.current_step = "scenes"
    job.progress = 0
    job.editing_started_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.scene_regenerate",
        detail={"job_id": job_id, "recurrence_key": recurrence_key,
                "edit_params": edit_params, "reroll_index": _prior_rerolls + 1},
    ))
    db.commit()

    try:
        enqueue_edit(
            job_id=job_id,
            edit_type="scene",
            edit_params=edit_params,
            plan=current_user.get("plan", "100"),
            tenant_id=current_user.get("tenant_id", ""),
        )
    except Exception as exc:
        logger.error("enqueue_edit (scene) failed for %s: %s", job_id, exc)
        job.status = "pending_review"
        # el re-roll de escena no tocó edit_count → nada que revertir acá
        job.editing_started_at = None
        job.progress = 100
        job.current_step = "thumbnail"
        db.commit()
        raise HTTPException(status_code=503, detail="No se pudo encolar la regeneración. Reintentá.")

    return {
        "ok": True,
        "job_id": job_id,
        "recurrence_key": recurrence_key,
        # El re-roll de escena NO consume el cupo de ediciones; edit_count va sin
        # cambios. Reportamos el cupo general (para el editor) + el índice de
        # re-roll de esta escena.
        "edit_count": job.edit_count or 0,
        "edits_remaining": max(0, _MAX_EDITS - (job.edit_count or 0)),
        "edit_limit_exempt": _is_admin,
        "reroll_count": _prior_rerolls + 1,
    }


@app.get("/jobs/{job_id}/scenes/thumbs")
async def get_scene_thumbnails(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """URLs firmadas (R2, 1h) de los pósters de todas las escenas, en una sola
    llamada autenticada. El frontend las pone directo en <img src> (las URLs
    firmadas no necesitan header). Devuelve {recurrence_key: url} sólo para las
    escenas que tienen thumb_key."""
    from database import Job as JobModel

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    plan = job.scene_plan if isinstance(job.scene_plan, dict) else None
    out: dict[str, str] = {}
    if plan and storage.is_enabled():
        for s in plan.get("scenes", []):
            tk = s.get("thumb_key")
            key = s.get("recurrence_key")
            if tk and key:
                try:
                    u = storage.generate_signed_url(tk, expiry_seconds=3600)
                    if u:
                        out[key] = u
                except Exception as _e:  # noqa: BLE001
                    logger.warning("[SCENES] firma de thumb %s falló: %s", key, _e)
    return {"thumbs": out}


@app.post("/enable-prores/{job_id}")
async def enable_prores_for_job(
    job_id: str,
    body: EnableProResRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Habilita ProRes export retroactivo para un job que se rindió como
    MP4 only (delivery_profile=youtube en el upload original). Persiste
    umg_spec en la fila del job y dispara el transcoding via prewarm
    queue. La descarga posterior va por /download/{id}/umg_master que
    ya tiene el lazy path armado (202 + Retry-After mientras transcode,
    302 a R2 cuando está listo).

    Re-llamar con specs distintas sobreescribe umg_spec en la DB y
    re-encola, PERO si el .mov anterior ya existe en disco o R2,
    ensure_prores_exists hace short-circuit y devuelve ese archivo
    (las specs nuevas no toman efecto). Si querés forzar re-transcode
    con specs distintas, primero borrá el .mov de R2 + outputs/.
    """
    from database import Job as JobModel, AuditLog

    if not has_prores_access(current_user):
        raise HTTPException(
            status_code=403,
            detail="Broadcast (ProRes) delivery is not enabled for your account.",
        )

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Job must be done before enabling ProRes export (current: {job.status})",
        )

    # Reusa la validación canónica. delivery_profile="umg" fuerza el
    # parseo y rechaza inputs inválidos con HTTPException 400.
    umg_spec = _parse_umg_params(
        delivery_profile="umg",
        umg_frame_size=body.umg_frame_size,
        umg_fps=body.umg_fps,
        umg_prores_profile=body.umg_prores_profile,
        current_user=current_user,
    )

    job.umg_spec = umg_spec
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.enable_prores",
        detail={"job_id": job_id, "umg_spec": umg_spec},
    ))
    db.commit()

    # Encola ambos masters. enqueue_prores_prewarm es best-effort: si el
    # tenant tiene la cola enterprise saturada hace skip (el lazy path
    # del /download los va a generar bajo demanda igual).
    enqueued = []
    try:
        for file_type in ("umg_master", "umg_short"):
            rq_id = enqueue_prores_prewarm(job_id, file_type)
            if rq_id:
                enqueued.append(file_type)
    except Exception as e:  # pragma: no cover
        logger.warning("[PRORES] enable-prores prewarm enqueue failed: %s", e)

    return {
        "ok": True,
        "job_id": job_id,
        "umg_spec": umg_spec,
        "enqueued": enqueued,
        "status": "queued",
        # Cliente debe poll /status hasta prores_ready=true, luego pegar
        # /download/{id}/umg_master para bajar el .mov.
        "retry_after": 90,
    }


# ---------------------------------------------------------------------------
# Drive delivery — botón "Guardar en Drive"
# ---------------------------------------------------------------------------

@app.post("/jobs/{job_id}/deliver-to-drive")
async def deliver_to_drive(
    job_id: str,
    body: DeliverToDriveRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Encola una transferencia R2 → Google Drive del user. Devuelve un
    transfer_id que el frontend usa para polear progress vía
    GET /drive/transfers/{transfer_id}.

    Requiere que el user haya conectado Drive previamente
    (GET /drive/status devuelve connected=true). Si no, 412.

    Filename en Drive: '<job_id>__<filename>' para evitar colisiones
    cuando varios jobs tienen el mismo umg_master.mov.
    """
    if not has_drive_access(current_user):
        raise HTTPException(status_code=403, detail="Drive integration not enabled for your account.")
    import uuid
    from database import Job as JobModel, DriveTransfer, UserDriveTokens
    from drive_uploader import FILE_TYPE_TO_DRIVE_NAME

    if body.file_type not in FILE_TYPE_TO_DRIVE_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"file_type debe ser uno de {list(FILE_TYPE_TO_DRIVE_NAME)}",
        )

    # Verificar que el user tiene Drive conectado
    drive_tokens = db.query(UserDriveTokens).filter(
        UserDriveTokens.user_id == current_user["id"]
    ).first()
    if drive_tokens is None:
        raise HTTPException(
            status_code=412,
            detail="Drive no está conectado. Conectalo en Settings antes de exportar.",
        )

    # Verificar que el job existe + es del tenant del user + está done
    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Job debe estar done para exportar a Drive (actual: {job.status})",
        )

    # Para umg_master / umg_short, el job debe tener umg_spec persistido
    # (sino no existe el archivo). Para video / short, siempre existe
    # post-done.
    if body.file_type in ("umg_master", "umg_short") and not job.umg_spec:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este job no tiene ProRes generado. Pegale a /enable-prores "
                "primero o seleccioná file_type=video/short."
            ),
        )

    transfer = DriveTransfer(
        id=uuid.uuid4().hex[:32],
        user_id=current_user["id"],
        job_id=job_id,
        file_type=body.file_type,
        status="queued",
        progress_pct=0,
    )
    db.add(transfer)
    db.commit()

    try:
        enqueue_drive_delivery(transfer.id, plan=current_user.get("plan", "100"))
    except Exception as e:
        # Si Redis cae, dejamos la row queued con error visible.
        transfer.status = "error"
        transfer.error = f"No se pudo encolar el job: {e}"
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="No se pudo encolar la transferencia. Reintentá en unos segundos.",
        )

    return {
        "ok": True,
        "transfer_id": transfer.id,
        "status": "queued",
        "poll_url": f"/drive/transfers/{transfer.id}",
    }


@app.get("/drive/transfers/{transfer_id}")
def get_drive_transfer(
    transfer_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Status actual de una transferencia. Frontend polea esto cada 3s
    mientras el modal de transferencia está abierto."""
    if not has_drive_access(current_user):
        raise HTTPException(status_code=403, detail="Drive integration not enabled for your account.")
    from database import DriveTransfer

    transfer = (
        db.query(DriveTransfer)
        .filter(DriveTransfer.id == transfer_id)
        .filter(DriveTransfer.user_id == current_user["id"])
        .first()
    )
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfer not found")

    return {
        "id": transfer.id,
        "job_id": transfer.job_id,
        "file_type": transfer.file_type,
        "status": transfer.status,
        "progress_pct": transfer.progress_pct or 0,
        "bytes_transferred": transfer.bytes_transferred or 0,
        "bytes_total": transfer.bytes_total or 0,
        "drive_file_id": transfer.drive_file_id,
        "web_view_link": transfer.web_view_link,
        "error": transfer.error,
        "created_at": transfer.created_at.isoformat() if transfer.created_at else None,
        "completed_at": transfer.completed_at.isoformat() if transfer.completed_at else None,
    }


# ---------------------------------------------------------------------------


class RetryJobRequest(BaseModel):
    """Optional body for POST /retry. All fields are overrides — when
    omitted, the existing values on the job row are kept. Today we only
    support overriding frame_size (UMG accepts HD/2K/UHD-4K/DCI-4K) so
    operators can downgrade a 4K render that OOMed the worker to HD on
    retry, without re-uploading the audio."""
    frame_size: str | None = Field(default=None, max_length=16)
    # When True, set render_params["bypass_content_validation"]=True
    # before re-enqueuing so the worker skips _validate_fn. Same semantics
    # as EditJobRequest/VariantJobRequest. Use case: a job died in
    # validation_failed because the prompt intentionally triggered the
    # validator (e.g. "rock guitarist hands as subject"), and the
    # operator wants to retry without recreating the variant manually.
    bypass_content_validation: bool = Field(default=False)
    force_content_validation: bool = Field(default=False)


@app.post("/retry/{job_id}")
async def retry_job(
    job_id: str,
    body: RetryJobRequest | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-enqueue a failed or validation_failed job using the audio still stored
    in R2. Avoids forcing the user to re-upload a 30-50 MB WAV. Only allowed
    when the job is in an unrecoverable terminal state (error or
    validation_failed) and the source audio is still available in object
    storage (input_r2_key is set and the object exists).

    Body (all optional):
      frame_size: "HD" | "UHD-4K" | "DCI-4K" | "DCI-2K" — override the
        job's stored umg_spec.frame_size. Used by the JobDetail Retry
        button's HD/2K/4K selector so the user can downgrade a 4K render
        that OOMed to HD without re-uploading.
    """
    from database import Job as JobModel, AuditLog

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("error", "validation_failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be retried from status '{job.status}'. "
                   "Only 'error' and 'validation_failed' jobs are retryable.",
        )
    if not job.input_r2_key:
        raise HTTPException(
            status_code=422,
            detail="Source audio no longer available — please upload the file again.",
        )
    # Pre-flight check that the R2 object actually exists. The DB column
    # records the key the upload landed at, but the object can be gone
    # later if a sibling job (parent or another variant) was deleted and
    # the input_r2_key reference-counting (jobs._delete_r2_objects) is
    # incorrect, or if cleanup_old_inputs reclaimed it. Without this
    # check the retry succeeds, the worker picks up the job, and a 6-min
    # render crashes loudly on "Could not download source audio from R2"
    # — the operator sees "Edit failed" with no actionable hint.
    # 2026-05-19 incident: agus.cafisi hit this twice in a row on staging.
    # Skipped silently when R2 is disabled (dev/test environments where
    # storage.is_enabled() is False — object_exists always returns False
    # there and would false-trigger this gate).
    try:
        import storage as _storage
        if _storage.is_enabled() and not _storage.object_exists(job.input_r2_key):
            raise HTTPException(
                status_code=422,
                detail=(
                    "El audio original ya no está en storage. "
                    "Probablemente fue limpiado o un job hermano lo borró. "
                    "Subí el MP3 de nuevo para regenerar el video."
                ),
            )
    except HTTPException:
        raise
    except Exception as _exc:
        # Storage probe error (network, credentials). Conservative
        # default: let the retry proceed — the worker will fail loud if
        # the file is actually missing, and the operator can still try.
        logger.warning(
            "[RETRY] R2 pre-check failed for %s key=%r — proceeding anyway: %s",
            job_id, job.input_r2_key, _exc,
        )

    # Apply optional frame_size override BEFORE we capture umg_spec for
    # the enqueue below. Validates against the same allow-list the
    # upload endpoint enforces.
    if body and body.frame_size is not None:
        allowed_frame_sizes = ("HD", "DCI-2K", "UHD-4K", "DCI-4K")
        if body.frame_size not in allowed_frame_sizes:
            raise HTTPException(
                status_code=400,
                detail=f"frame_size must be one of {allowed_frame_sizes}",
            )
        if job.umg_spec:
            new_spec = dict(job.umg_spec)
            new_spec["frame_size"] = body.frame_size
            job.umg_spec = new_spec

    # Operator opt-in flags forwarded to render_params before re-enqueue.
    # When False/missing, render_params is untouched and tenant-gated
    # defaults in pipeline.Step 1b apply.
    _rp = dict(job.render_params or {})
    _rp.pop("bypass_content_validation", None)
    _rp.pop("force_content_validation", None)
    if body and body.force_content_validation:
        _rp["force_content_validation"] = True
    elif body and body.bypass_content_validation:
        _rp["bypass_content_validation"] = True
    else:
        _rp["force_content_validation"] = True
    job.render_params = _rp

    # Capturar status PREVIO antes de mutar. Sin esto el AuditLog
    # registraba siempre "processing" como previous_status (la línea de
    # abajo lee job.status DESPUÉS del mutate), haciendo el log
    # inservible para forensics ("¿en qué estado estaba el job cuando
    # el operador apretó retry?" → siempre 'processing').
    _previous_status = job.status
    # Same reasoning for job.error — read it before the reset so the
    # bg-preservation logic downstream can introspect the failure cause.
    _previous_error = job.error or ""

    # Reset job to initial processing state before re-enqueueing.
    job.status = "processing"
    job.current_step = "whisper"
    job.progress = 0
    job.error = None
    job.validation_result = None
    job.video_url = None
    job.short_url = None
    job.thumbnail_url = None
    job.umg_master_url = None
    job.umg_short_url = None
    job.s3_keys = None
    job.completed_at = None
    job.approved_by = None
    job.approved_at = None
    # Resetear edit_count: el retry trae el job a estado limpio para
    # re-procesar; los edits hechos antes del fail quedan en AuditLog
    # pero el job vuelve a tener 3 edits disponibles. Sin esto, un job
    # que hizo 3 edits y falló queda permanentemente bloqueado de
    # re-editar tras el retry.
    job.edit_count = 0
    # Resetear el reloj del reaper. Sin esto, un job creado hace 12 h
    # que el usuario reintenta ahora cae inmediatamente en find_stalled_renders
    # (last_progress_at viejo) o find_stuck_jobs (created_at viejo) y la
    # próxima pasada del reaper lo mata otra vez. Incidente 2026-05-15:
    # /retry programático restauró 4 omg jobs a `processing`, el reaper
    # los killió 5 min después porque created_at era de 13:45 (>100 min).
    # NOW() sobre last_progress_at es la fuente de verdad nueva — find_stuck_jobs
    # ahora hace coalesce(last_progress_at, created_at) en ese mismo PR.
    job.last_progress_at = datetime.now(timezone.utc)

    # Cancel any ProRes prewarm still queued from the PRIOR render. The retry
    # re-renders lyric_video.mp4 from scratch, so an in-flight prewarm of the
    # old cut would otherwise publish a stale .mov / re-add a stale s3_key
    # after we cleared s3_keys above (same hazard run_edit_pipeline guards on
    # the edit path). A prewarm already mid-ffmpeg won't stop here, but it's
    # independently fenced by the source-mtime freshness check in
    # ensure_prores_exists (the retry's rewrite bumps the source mtime).
    try:
        from queue_jobs import cancel_rq_job
        for _ft in ("umg_master", "umg_short"):
            cancel_rq_job(f"prewarm:{job_id}:{_ft}")
    except Exception as _exc:
        logger.warning("retry_job: prewarm cancel skipped for %s: %s", job_id, _exc)

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.retry",
        detail={"job_id": job_id, "previous_status": _previous_status},
    ))
    db.commit()

    umg_spec = job.umg_spec or {}
    # Preserve the user's lyric edits across retries. Without this, the
    # pipeline re-ran Whisper from scratch on every retry and silently
    # blew away any manual corrections the user had made in the wizard.
    # The pipeline ALSO falls back to job.segments_json if we don't
    # pass segments_override (belt-and-suspenders), but passing it
    # explicitly here makes the intent visible at the call site and
    # keeps the retry path symmetrical with the /generate path.
    segments_override = job.segments_json if job.segments_json else None

    # Preserve the previously-approved background across retries.
    # Without this the pipeline regenerates a fresh Veo, which silently
    # discards an operator-approved aesthetic just because some downstream
    # step (lyrics edit, render compositor, R2 upload) blew up.
    # Incident 2026-05-19, Amanda Pujó "Ser Anti" (default tenant, no
    # background_hint): operator approved a bg, ran a lyrics edit, the
    # edit died mid-pipeline (worker SIGTERM during deploy storm), /retry
    # kicked off a full re-render → Gemini picked an entirely different
    # scene since there was no hint to anchor on. Operator called it
    # "me cambió todo".
    #
    # Guard: skip bg-reuse when the validator was the cause of the
    # original failure — re-using a bg that previously failed Guideline
    # 15 would just fail again. For every other error class (worker
    # death, R2 timeout, post-bg pipeline failure) the cached bg is the
    # right asset to reuse.
    preserved_bg_r2_key = None
    _err_lower = _previous_error.lower()
    _bg_was_blamed = (
        "content policy" in _err_lower
        or "guideline 15" in _err_lower
        or _previous_status == "validation_failed"
    )
    if job.bg_r2_key_cached and not _bg_was_blamed:
        preserved_bg_r2_key = job.bg_r2_key_cached

    # Forward render_params to the worker. Without this, /retry stripped
    # background_hint / concept / typography settings and the worker ran
    # a default pipeline — Gemini picked an unrelated scene instead of
    # the operator's prompt. Real incident 2026-05-19: operator's
    # "rock guitarist hands" variant failed validation, they hit Reintentar
    # (fondo libre), and the regen produced an old man in a rainy alley
    # because the prompt was lost between create-time and retry-time.
    # Mirrors the /variant endpoint pattern (see line 5814 region).
    _retry_render_params = job.render_params or {}
    retry_pipeline_kwargs = {}
    # lyric_transition + text_motion deprecados 2026-05-23 — sacados de la
    # whitelist; si están en render_params viejos quedan como dato muerto,
    # no se propagan al re-render.
    for k in ("font", "font_scale", "text_case", "frame_format", "text_contrast",
              "movement_style", "animate_image", "genre", "match_lyrics",
              "background_hint", "concept", "bg_verbatim",
              "effect", "custom_colors",
              # Lyric animation + line transition (libass templates from the
              # wizard). Added 2026-05-22 along with /variant: if a job with
              # karaoke/reveal fell to validation_failed and the operator hit
              # Reintentar, animations silently reset to "none" because they
              # weren't in this whitelist when the feature was wired (#357a1a5).
              "lyrics_animation", "line_transition",
              # Lyric text colors 2026-05-25 — heredables igual que
              # custom_colors, así los re-renders/variantes mantienen el
              # color elegido por el operador.
              "lyric_color", "lyric_sung_color",
              # Title-card customization (Full Rotor v1) — heredables.
              "title_template", "title_size",
              "title_artist_font", "title_song_font",
              # UI v1.1 (2026-05-30): manual song split. Inheritable so
              # a retry/variant respects the operator's chosen break.
              "title_song_break",
              # Escenas (multi-escena): heredable, así un retry de un job
              # multi-escena vuelve a armar las escenas en vez de caer al
              # fondo único. Persistido por pipeline en render_params cuando
              # el render corre con enable_scenes=True.
              "enable_scenes"):
        if k in _retry_render_params and _retry_render_params[k] not in (None, ""):
            retry_pipeline_kwargs[k] = _retry_render_params[k]

    # Audit A5: re-gatear enable_scenes con el acceso ACTUAL del usuario, igual
    # que /generate y /upload. Un tenant al que se le sacó el acceso (o se cayó
    # de SCENES_ENABLED_TENANTS) no debe seguir generando multi-escena —y su
    # costo Veo extra— al reintentar un job viejo.
    if retry_pipeline_kwargs.get("enable_scenes"):
        retry_pipeline_kwargs["enable_scenes"] = has_scenes_access(current_user)

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=None,
        artist=job.artist,
        style=job.style or "oscuro",
        plan=current_user.get("plan", "100"),
        tenant_id=current_user.get("tenant_id", ""),
        delivery_profile=job.delivery_profile or "youtube",
        input_r2_key=job.input_r2_key,
        song_title=job.song_title or "",
        umg_spec=umg_spec,
        segments_override=segments_override,
        bg_r2_key=preserved_bg_r2_key,
        **retry_pipeline_kwargs,
    )

    return {
        "ok": True,
        "status": "processing",
        "job_id": job_id,
        "preserved_lyrics": segments_override is not None,
        "preserved_background": preserved_bg_r2_key is not None,
    }


# ---------------------------------------------------------------------------
# Variantes — re-generar un job aprobado con otro Veo background
# ---------------------------------------------------------------------------

class VariantJobRequest(BaseModel):
    """Body para POST /jobs/{parent_job_id}/variant.

    Crea un job NUEVO (cuenta como video pago del plan) que hereda del
    padre: audio (input_r2_key), segments_json (lyrics aprobadas), artist,
    song_title, umg_spec, delivery_profile, typography (font/case/etc).
    Re-genera SOLO el background Veo, opcionalmente con un hint o concept
    distinto al original. Use case: el operador ya tiene un video aprobado
    pero quiere probar otra estética sin perder el trabajo de lyrics ya
    afinadas.

    Todos los campos son opcionales — si no se mandan, el job hereda los
    valores del padre. La única forma "barata" de crear variante es no
    mandar nada y dejar que Gemini re-elija el prompt con el system prompt
    desbiaseado (PR #116).
    """
    # Mismo formato y max_length que EditJobRequest.background_hint —
    # va al user_content de Gemini con header [OPERATOR OVERRIDE].
    # 2000 chars (bumped 2026-05-18, ver EditJobRequest para rationale).
    background_hint: str | None = Field(default=None, max_length=2000)
    # Espejo del flag de EditJobRequest — operator override del content
    # validator (UMG Guideline 15). Misma semántica, ver EditJobRequest.
    bypass_content_validation: bool = Field(default=False)
    force_content_validation: bool = Field(default=False)
    # Override del concept del padre. 2000 chars igual que /generate.
    # Alimenta _get_unique_prompt() junto con genre/style/lyrics.
    concept: str | None = Field(default=None, max_length=2000)
    # Override del style preset (gradient palette + visual register).
    style: str | None = Field(default=None, max_length=50)
    # 2026-05-29 — Variant cap policy: each plan includes 3 renders of
    # the same song (original + 2 variants). The 4th onward costs
    # VARIANT_OVERAGE_COST_USD passthrough (Veo background generation
    # fee). The endpoint returns 402 with `code: variant_overage_unconfirmed`
    # if the operator tries to create the 4th+ without setting this flag.
    # Re-submit with `acknowledge_variant_overage: true` to proceed and
    # accept the charge. The acknowledgement is logged via AuditLog so
    # month-close billing surfaces the line items per tenant.
    acknowledge_variant_overage: bool = Field(default=False)


# Variant-overage policy constants. Module-level so tests can monkey-
# patch them and operators can grep for the magic numbers from the FAQ
# without spelunking through endpoint code.
VARIANT_INCLUDED_PER_SONG = 3  # original + 2 variants free; 4th+ paid.
VARIANT_OVERAGE_COST_USD = 0.90  # Veo cost passthrough — keep in sync
                                  # with FAQ #7 and PLANS["250"] comment.


@app.post("/jobs/{parent_job_id}/variant")
async def create_variant(
    parent_job_id: str,
    body: VariantJobRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea una variante del job padre. Job nuevo con su propio job_id,
    propio review flow, y propio cobro al plan. Mismo audio + mismo
    segments_json — solo cambia el background Veo (y opcionalmente
    typography si el padre tenía y se quiere mantener).

    El padre debe estar en status='done'. La variante arranca en 'processing'
    y va por el pipeline normal saltando Whisper (segments_override).

    400 si padre no done. 402 si el plan está sin capacidad. 403 si el
    padre no es del tenant del user. 404 si el padre no existe.
    """
    import uuid
    from database import Job as JobModel, AuditLog

    parent = (
        db.query(JobModel)
        .filter(JobModel.job_id == parent_job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not parent:
        raise HTTPException(status_code=404, detail="Parent job not found")
    if parent.status != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Solo se pueden crear variantes de jobs aprobados (status='done'). "
                   f"Este job está en status='{parent.status}'.",
        )
    if not parent.segments_json:
        # Sanity: un job "done" SIN segments_json no sirve como padre
        # porque la variante no podría saltar Whisper. No debería pasar
        # — todos los jobs done post-PR #106 persisten segments — pero
        # guard explícito.
        raise HTTPException(
            status_code=422,
            detail="Este job no tiene lyrics persistidas — no se puede "
                   "crear variante sin re-subir el audio.",
        )
    if not parent.input_r2_key:
        raise HTTPException(
            status_code=422,
            detail="Audio del job padre ya no está disponible en storage — "
                   "no se puede crear variante.",
        )
    # Verify the R2 object actually still exists (not just the DB column).
    # Variants copy the parent's input_r2_key — a stale parent column
    # would point the variant at a ghost file and the worker would crash
    # on download. Same pre-check as /retry and /edit. Skipped silently
    # when R2 is disabled (dev/test).
    try:
        import storage as _storage
        if _storage.is_enabled() and not _storage.object_exists(parent.input_r2_key):
            raise HTTPException(
                status_code=422,
                detail=(
                    "El audio del job padre ya no está en storage — "
                    "no se puede crear variante. Subí el MP3 de nuevo."
                ),
            )
    except HTTPException:
        raise
    except Exception as _exc:
        logger.warning(
            "[VARIANT] R2 pre-check failed for parent=%s key=%r — proceeding anyway: %s",
            parent_job_id, parent.input_r2_key, _exc,
        )

    # Plan capacity check — misma lógica que /generate. Variante cuenta
    # como 1 video del plan.
    plan = current_user.get("plan", "100")
    usage_info = get_plan_usage(db, current_user["id"], current_user["tenant_id"], plan,
                                billing_group=current_user.get("billing_group"))
    if usage_info["alert_100"] and plan == "free":
        raise HTTPException(
            status_code=429,
            detail="Free plan limit reached. Upgrade to continue.",
        )
    # Para planes pagos, allow_overage decide si se permite pasarse del cap.
    user_model = db.query(User).filter(User.id == current_user["id"]).first()
    if usage_info.get("alert_100") and not (user_model and user_model.allow_overage):
        raise HTTPException(
            status_code=402,
            detail=f"Llegaste al límite de tu plan ({usage_info.get('limit', '?')} canciones). "
                   f"Activá overage o subí de plan para crear más variantes.",
        )

    # 2026-05-29 — Variant cap policy. Each plan includes 3 renders of the
    # same song (original + 2 variants); the 4th onward costs
    # VARIANT_OVERAGE_COST_USD passthrough.
    #
    # "Same song" identity matches get_plan_usage (auth.py): LOWER(TRIM(...))
    # on artist + song_title. We count every existing render of the song
    # in this tenant — including the parent — that isn't in a bg_preview /
    # deleted-equivalent state. Renders in `error`, `validation_failed`,
    # `pending_review`, `editing`, `queued`, `processing`, `done`,
    # `rejected` all consume Veo at some point and therefore consume the
    # included-variants budget.
    #
    # `bg_preview_*` jobs are ephemeral background-browse helpers, NOT
    # rendered videos — they have their own cleanup thread (main.py:562)
    # and never count toward variant cap.
    #
    # `transcribed_pending` is excluded because no Veo render fired yet
    # (the operator hasn't /generate'd from the transcribe sandbox);
    # those rows can disappear via supersede without cost.
    from sqlalchemy import func as _sql_func
    _song_artist = (parent.artist or "").strip().lower()
    _song_title = (parent.song_title or "").strip().lower()
    _RENDERED_OR_PENDING = (
        "queued", "processing", "pending_review", "editing", "done",
        "rejected", "error", "validation_failed",
    )
    existing_renders = (
        db.query(JobModel)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .filter(_sql_func.lower(_sql_func.trim(JobModel.artist)) == _song_artist)
        .filter(
            _sql_func.lower(
                _sql_func.coalesce(_sql_func.trim(JobModel.song_title), "")
            ) == _song_title
        )
        .filter(JobModel.status.in_(_RENDERED_OR_PENDING))
        .count()
    )
    # existing_renders includes the parent (1) + every prior sibling.
    # A would-be NEW variant is the (existing_renders + 1)-th render.
    # If that exceeds VARIANT_INCLUDED_PER_SONG, charge overage.
    would_be_render_n = existing_renders + 1
    if would_be_render_n > VARIANT_INCLUDED_PER_SONG:
        if not body.acknowledge_variant_overage:
            # Operator hasn't acknowledged the extra charge. Return a
            # structured 402 so the frontend can show a confirm modal
            # with the exact cost + count, then retry with the ack flag.
            # Status 402 (Payment Required) is the standard HTTP code
            # for "this action costs money beyond the plan".
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "variant_overage_unconfirmed",
                    "message": (
                        f"Esta canción ya tiene {existing_renders} versiones "
                        f"renderizadas (incluido el original). El plan incluye "
                        f"{VARIANT_INCLUDED_PER_SONG} versiones por canción; "
                        f"a partir de la {VARIANT_INCLUDED_PER_SONG + 1}ª se "
                        f"factura ${VARIANT_OVERAGE_COST_USD:.2f} adicional al "
                        f"cierre del mes. Confirmá enviando "
                        f"`acknowledge_variant_overage: true` para proceder."
                    ),
                    "existing_renders": existing_renders,
                    "included_per_song": VARIANT_INCLUDED_PER_SONG,
                    "cost_extra_usd": VARIANT_OVERAGE_COST_USD,
                    "artist": parent.artist,
                    "song_title": parent.song_title,
                },
            )
        # Acknowledged. Log to AuditLog for month-close billing —
        # tenant ops will run a SUM(cost_usd) over audit rows with
        # action="variant.overage_charge" to invoice the extra videos.
        # We don't write to the Invoice table directly because the
        # billing cycle batches at month-close; storing here keeps the
        # signal close to the operator action and decouples from the
        # batch job timing.
        db.add(AuditLog(
            user_id=current_user["id"],
            action="variant.overage_charge",
            detail={
                "parent_job_id": parent_job_id,
                "tenant_id": current_user["tenant_id"],
                "artist": parent.artist,
                "song_title": parent.song_title,
                "existing_renders_before_this_one": existing_renders,
                "would_be_render_number": would_be_render_n,
                "cost_usd": VARIANT_OVERAGE_COST_USD,
                "policy": {
                    "included_per_song": VARIANT_INCLUDED_PER_SONG,
                    "overage_cost_usd": VARIANT_OVERAGE_COST_USD,
                },
            },
        ))
        # Commit the audit row eagerly so a crash between here and
        # the job-create-flush doesn't lose the billing event.
        db.commit()

    # Merge: render_params del padre + overrides del body.
    parent_render_params = dict(parent.render_params or {})
    new_render_params = dict(parent_render_params)
    if body.background_hint is not None:
        new_render_params["background_hint"] = body.background_hint
    if body.concept is not None:
        new_render_params["concept"] = body.concept
    new_render_params.pop("bypass_content_validation", None)
    new_render_params.pop("force_content_validation", None)
    if body.force_content_validation:
        new_render_params["force_content_validation"] = True
    elif body.bypass_content_validation:
        new_render_params["bypass_content_validation"] = True
    else:
        new_render_params["force_content_validation"] = True

    # Style: override o herencia.
    new_style = body.style if body.style is not None else (parent.style or "oscuro")

    # Crear el job nuevo. NO usamos jobs.create_job() porque queremos
    # control fino sobre segments_json + parent_job_id + render_params
    # mergeados, y create_job no acepta esos params.
    new_job_id = uuid.uuid4().hex[:12]

    # Variant gets its own copy of the input audio in R2 — server-side
    # CopyObject, no bytes round-trip through us. This makes each variant
    # self-contained: deleting the parent (or any sibling) no longer
    # breaks the lineage.
    #
    # Before this PR variants inherited `parent.input_r2_key` literally.
    # PR #220 already prevents the cascade-delete via a sibling-count
    # check in jobs._delete_r2_objects, but copying the audio is the
    # belt-and-suspenders: if anything else ever deletes the parent's
    # raw key (R2 lifecycle policy, manual ops, future regression),
    # this variant still has its own copy.
    #
    # Storage cost: ~30-80 MB per variant WAV/MP3. Marginal vs the
    # ~$0.90 Veo cost per variant. Tradeoff worth the safety.
    #
    # Fallback semantics: if copy fails for any reason (R2 disabled,
    # transient error, source missing despite the pre-check above), we
    # fall back to sharing parent.input_r2_key — same as pre-fix behavior.
    # The audit log records which mode was used so admin can spot the
    # silently-degraded case.
    variant_input_r2_key = parent.input_r2_key
    variant_owns_input = False
    try:
        import os as _os_mod
        import storage as _storage_mod
        src_key = parent.input_r2_key
        src_filename = _os_mod.path.basename(src_key) if src_key else ""
        if src_key and src_filename and _storage_mod.is_enabled():
            candidate_dst = _storage_mod._input_object_key(
                current_user["tenant_id"], new_job_id, src_filename
            )
            if _storage_mod.copy_object(src_key, candidate_dst):
                variant_input_r2_key = candidate_dst
                variant_owns_input = True
                logger.info(
                    "[VARIANT] Audio copied: %s -> %s (parent=%s, new_job=%s)",
                    src_key, candidate_dst, parent.job_id, new_job_id,
                )
            else:
                logger.warning(
                    "[VARIANT] Audio copy returned False, falling back to shared key: %s",
                    src_key,
                )
    except Exception as _exc:
        # Copy is best-effort. If it explodes, the variant still works —
        # just shares its input with the parent (pre-#220 behavior).
        logger.warning(
            "[VARIANT] copy_object failed for parent=%s src=%r, falling back to shared key: %s",
            parent.job_id, parent.input_r2_key, _exc,
        )

    new_job = JobModel(
        job_id=new_job_id,
        user_id=current_user["id"],
        tenant_id=current_user["tenant_id"],
        artist=parent.artist,
        song_title=parent.song_title,
        style=new_style,
        filename=parent.filename,
        delivery_profile=parent.delivery_profile,
        umg_spec=parent.umg_spec,
        status="processing",
        current_step="background",  # salta Whisper
        progress=0,
        input_r2_key=variant_input_r2_key,
        segments_json=parent.segments_json,
        render_params=new_render_params,
        edit_count=0,
        parent_job_id=parent.job_id,
    )
    db.add(new_job)
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.variant_created",
        detail={
            "parent_job_id": parent.job_id,
            "new_job_id": new_job_id,
            "background_hint": body.background_hint,
            "concept_overridden": body.concept is not None,
            "style_overridden": body.style is not None,
            "variant_owns_input": variant_owns_input,
            "bypass_content_validation": bool(body.bypass_content_validation),
            "force_content_validation": bool(body.force_content_validation),
            "tenant_id": current_user.get("tenant_id"),
        },
    ))
    db.commit()
    logger.info(
        "[VARIANT] created job=%s parent=%s tenant=%s bypass=%s force=%s",
        new_job_id, parent.job_id, current_user.get("tenant_id"),
        bool(body.bypass_content_validation), bool(body.force_content_validation),
    )

    # Encolar con segments_override para saltar Whisper. Mismo kwargs
    # shape que /retry, más concept/background_hint si vinieron.
    pipeline_kwargs = {
        "delivery_profile": parent.delivery_profile or "youtube",
        # Use the variant's own input key (post-copy) when available, so
        # the worker downloads the variant-owned WAV, not the parent's
        # shared one. Falls back to parent's key if copy_object failed.
        "input_r2_key": variant_input_r2_key,
        "song_title": parent.song_title or "",
        "umg_spec": parent.umg_spec or {},
        "segments_override": parent.segments_json,
    }
    # render_params del padre + overrides — los param de typography
    # (font, font_scale, etc) se pasan como kwargs individuales que
    # run_pipeline acepta. concept también va por kwarg.
    # lyric_transition + text_motion deprecados 2026-05-23 — fuera del whitelist.
    for k in ("font", "font_scale", "text_case", "frame_format", "text_contrast",
              "movement_style", "animate_image", "genre", "match_lyrics",
              "bg_verbatim",
              # FX layer + lyric animation/transition (libass templates from
              # the wizard). Added 2026-05-22: variantes heredaban tipografía
              # y movimiento del padre pero perdían el efecto encima (nieve/
              # lluvia/etc.) y las animaciones de letra (karaoke/reveal/...)
              # porque no estaban en este whitelist cuando se cableó (#51946bf
              # + #357a1a5). custom_colors va con effect porque su flow es el
              # mismo (paleta opcional para el grade).
              "effect", "custom_colors",
              "lyrics_animation", "line_transition",
              # Title-card customization (Full Rotor v1) — variantes heredan.
              "title_template", "title_size",
              "title_artist_font", "title_song_font"):
        if k in new_render_params:
            pipeline_kwargs[k] = new_render_params[k]
    if body.concept is not None:
        pipeline_kwargs["concept"] = body.concept
    elif parent_render_params.get("concept"):
        pipeline_kwargs["concept"] = parent_render_params["concept"]
    # background_hint llega solo si el operador escribió algo en el
    # textarea del modal. Si está vacío, run_pipeline lo recibe como
    # None y _ensure_background sigue el flow default (PR #116
    # system prompt desbiaseado).
    if body.background_hint is not None:
        pipeline_kwargs["background_hint"] = body.background_hint

    enqueue_pipeline(
        job_id=new_job_id,
        mp3_path=None,
        artist=parent.artist,
        style=new_style,
        plan=plan,
        tenant_id=current_user.get("tenant_id", ""),
        **pipeline_kwargs,
    )

    return {
        "ok": True,
        "job_id": new_job_id,
        "parent_job_id": parent.job_id,
        "status": "processing",
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
def get_settings(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return app settings for the current user."""
    settings = db.query(UserSettings).filter(UserSettings.user_id == current_user["id"]).first()
    return settings.settings_json if settings else {}


@app.post("/settings")
def save_settings(
    body: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save app settings for the current user."""
    settings = db.query(UserSettings).filter(UserSettings.user_id == current_user["id"]).first()
    if settings:
        settings.settings_json = body
    else:
        settings = UserSettings(user_id=current_user["id"], settings_json=body)
        db.add(settings)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

class YoutubeUploadBody(BaseModel):
    privacy: str = "unlisted"
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


def _load_yt_settings(db: Session, user_id: int) -> dict:
    """The user's YouTube template (title format, header/footer, hashtags,
    mandatory tags, language) from UserSettings — so the template configured
    in Settings → YouTube is actually applied at publish time."""
    row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    return (row.settings_json or {}) if row else {}


@app.get("/youtube/connection-status")
async def youtube_connection_status(current_user: dict = Depends(get_current_user)):
    """Check if a YouTube account is connected and return channel info."""
    import asyncio
    from youtube_upload import get_connection_status
    loop = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, get_connection_status)
    return status


@app.get("/youtube/auth-url")
async def youtube_auth_url(current_user: dict = Depends(get_current_user)):
    """Return the Google OAuth URL for connecting the system YouTube account.

    Solo admin: YouTube es una cuenta central del sistema (todos los tenants
    suben ahí), así que conectarla/cambiarla es una acción de administración.
    El state token bindea el flujo a este admin (CSRF)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede conectar la cuenta de YouTube.")
    from youtube_oauth import build_authorization_url, YoutubeOAuthError
    try:
        url = build_authorization_url(current_user["id"])
    except YoutubeOAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"auth_url": url}


@app.get("/youtube/callback")
async def youtube_oauth_callback(
    code: str = Query("", max_length=2048),
    state: str = Query("", max_length=2048),
    error: str = Query("", max_length=200),
    db: Session = Depends(get_db),
):
    """Callback de Google tras el consent. Verifica el state (HMAC),
    intercambia el code, cachea el channel y guarda el token encriptado en
    system_youtube_token. Cierra el popup vía postMessage.

    NO usa get_current_user: Google no manda el JWT — la identidad viene
    del state token firmado al construir la auth URL."""
    from fastapi.responses import HTMLResponse
    from youtube_oauth import (
        YoutubeOAuthError, verify_state_token, exchange_code_for_tokens,
        fetch_channel_info, save_system_token,
    )

    def _popup(html_body: str, status: int = 200):
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding-top:3rem;background:#111;color:#eee;">
        {html_body}
        </body></html>""", status_code=status)

    if error:
        return _popup(f'<h2 style="color:#f87171">Conexión cancelada</h2><p style="color:#aaa">{error}</p>'
                      '<script>setTimeout(()=>window.close(),2500);</script>', 400)

    try:
        user_id = verify_state_token(state)
    except YoutubeOAuthError as e:
        return _popup(f'<h2 style="color:#f87171">Error de seguridad</h2><p style="color:#aaa">{e}</p>'
                      '<script>setTimeout(()=>window.close(),3000);</script>', 400)

    # Re-chequeo de rol: el admin pudo perder el rol entre auth-url y callback.
    cb_user = db.query(User).filter(User.id == user_id).first()
    if cb_user is None or cb_user.role != "admin":
        return _popup('<h2 style="color:#f87171">Sin permisos</h2>'
                      '<p style="color:#aaa">Solo un administrador puede conectar YouTube.</p>'
                      '<script>setTimeout(()=>window.close(),3000);</script>', 403)

    try:
        token_data = exchange_code_for_tokens(code)
    except YoutubeOAuthError as e:
        return _popup(f'<h2 style="color:#f87171">Error al conectar</h2><p style="color:#aaa">{e}</p>'
                      '<script>setTimeout(()=>window.close(),3000);</script>', 400)

    # Channel info es best-effort (para mostrar "Conectado como X").
    channel = fetch_channel_info(token_data.get("token", ""))
    save_system_token(db, token_data, channel=channel, user_id=user_id)

    ch_name = channel.get("channel_name") or "tu canal"
    return _popup(
        '<div style="font-size:3rem;margin-bottom:1rem">✓</div>'
        '<h2 style="color:#a3e635">YouTube conectado</h2>'
        f'<p style="color:#888;font-size:0.9rem">Cuenta: {ch_name}. Ya podés cerrar esta ventana.</p>'
        '<script>if(window.opener){window.opener.postMessage("yt_connected","*");}'
        'setTimeout(()=>window.close(),1500);</script>'
    )


@app.post("/youtube/disconnect")
async def youtube_disconnect(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnect the system YouTube account (admin only)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede desconectar YouTube.")
    from youtube_oauth import delete_system_token
    removed = delete_system_token(db)
    return {"disconnected": removed}


@app.get("/youtube/upload-progress/{job_id}")
async def youtube_upload_progress(job_id: str, current_user: dict = Depends(get_current_user)):
    """Return the current upload progress (0-100) for a job, or -1 if not uploading."""
    from youtube_upload import get_upload_progress
    return {"progress": get_upload_progress(job_id), "short_progress": get_upload_progress(job_id + "_short")}


@app.post("/youtube/upload/{job_id}")
async def youtube_upload(
    job_id: str,
    body: YoutubeUploadBody = None,
    privacy: str = "unlisted",
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a completed job's video to YouTube with AI-generated metadata."""
    if body:
        privacy = body.privacy
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")

    video_path = os.path.join(OUTPUTS_DIR, job_id, "lyric_video.mp4")
    thumb_path = os.path.join(OUTPUTS_DIR, job_id, "thumbnail.jpg")

    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video file not found.")

    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    artist = job.get("artist", "")

    yt_settings = _load_yt_settings(db, current_user["id"])
    import asyncio
    from functools import partial
    from youtube_upload import upload_to_youtube
    loop = asyncio.get_event_loop()
    try:
        fn = partial(
            upload_to_youtube,
            video_path, thumb_path, artist, song, "", privacy, job_id,
            body.title if body else None,
            body.description if body else None,
            tags_override=body.tags if body else None,
            settings=yt_settings,
        )
        result = await loop.run_in_executor(None, fn)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"message": f"YouTube upload failed: {e}", "code": str(e)})

    update_job(job_id, youtube=result)
    return result


@app.post("/youtube/upload-short/{job_id}")
async def youtube_upload_short(
    job_id: str,
    body: YoutubeUploadBody = None,
    privacy: str = "unlisted",
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a completed job's Short (vertical 9:16) to YouTube Shorts."""
    if body:
        privacy = body.privacy
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")

    short_path = os.path.join(OUTPUTS_DIR, job_id, "short.mp4")
    thumb_path = os.path.join(OUTPUTS_DIR, job_id, "thumbnail.jpg")

    if not os.path.exists(short_path):
        raise HTTPException(status_code=404, detail="Short file not found. The job may not have generated a short.")

    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    artist = job.get("artist", "")

    yt_settings = _load_yt_settings(db, current_user["id"])
    import asyncio
    from functools import partial
    from youtube_upload import upload_short_to_youtube
    loop = asyncio.get_event_loop()
    try:
        fn = partial(
            upload_short_to_youtube,
            short_path, thumb_path, artist, song, "", privacy, job_id,
            body.title if body else None,
            body.description if body else None,
            tags_override=body.tags if body else None,
            settings=yt_settings,
        )
        result = await loop.run_in_executor(None, fn)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"message": f"YouTube upload failed: {e}", "code": str(e)})

    update_job(job_id, youtube_short=result)
    return result


@app.post("/youtube/metadata-short/{job_id}")
async def youtube_short_metadata_preview(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview the AI-generated YouTube Shorts metadata without uploading."""
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    yt_settings = _load_yt_settings(db, current_user["id"])
    from youtube_upload import generate_short_metadata
    from functools import partial
    import asyncio
    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(
        None, partial(generate_short_metadata, job.get("artist", ""), song, "",
                      job_id=job_id, settings=yt_settings),
    )
    return metadata


@app.post("/youtube/metadata/{job_id}")
async def youtube_metadata_preview(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview the AI-generated YouTube metadata without uploading."""
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    yt_settings = _load_yt_settings(db, current_user["id"])
    from youtube_upload import generate_youtube_metadata
    from functools import partial
    import asyncio
    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(
        None, partial(generate_youtube_metadata, job.get("artist", ""), song, "",
                      job_id=job_id, settings=yt_settings),
    )
    return metadata


# =============================================================================
# UMG deliveries portal (umg.genly.pro)
# =============================================================================
# Two surfaces:
#   1. /admin/deliveries/*   — JWT auth, admin role only. Used by the
#      "Enviar a UMG" button in JobDetail.jsx.
#   2. /api/deliveries/*     — portal-token auth (X-Portal-Token header).
#      Used by the static portal page to fetch the listing and to soft-
#      delete entries. The portal token is the same shared password
#      Universal already uses to enter the portal — separate from JWT
#      because the portal has no login flow.
#
# R2 keys are computed deterministically from (tenant, job_id, file_type)
# — same convention gen_page.py used. The listing endpoint signs URLs
# on demand with 7-day expiry (R2 max) and caches the response 60 s in
# memory so a page refresh doesn't re-sign 80+ URLs per click.
# =============================================================================

# File type → filename in R2 + extension + label. Single source of truth
# for the portal listing. Adding a new file type (e.g. lossless audio)
# means appending here and the rest of the pipeline picks it up.
_DELIVERY_FILE_TYPES: dict[str, dict[str, str]] = {
    "umg_master": {"filename": "umg_master.mov", "ext": "mov", "label": "ProRes Master (broadcast)"},
    "umg_short":  {"filename": "umg_short.mov",  "ext": "mov", "label": "ProRes Short vertical (broadcast)"},
    "video":      {"filename": "lyric_video.mp4","ext": "mp4", "label": "MP4 HD (web/YouTube)"},
    "short":      {"filename": "short.mp4",      "ext": "mp4", "label": "MP4 Short vertical (Reels/TikTok)"},
    "thumbnail":  {"filename": "thumbnail.jpg",  "ext": "jpg", "label": "Thumbnail (cover art)"},
}
# All five files we ship to UMG by default. Validated against R2 before
# we even create the Delivery row — no point publishing a partial entry.
_DEFAULT_DELIVERY_FILE_TYPES = ["umg_master", "video", "umg_short", "short", "thumbnail"]
_DELIVERY_URL_EXPIRY_S = 7 * 24 * 3600  # R2 max
# No in-process cache for the deliveries listing. Railway runs the app
# with multiple uvicorn workers (Dockerfile: --workers 2), so a per-
# process cache silently desynced after a POST/DELETE: the writer
# invalidated its own cache, the next read landed on another worker
# whose cache was still warm with the pre-publish snapshot, and the
# operator saw "everything OK" while the portal listing was stale for
# up to 60 s. The listing query is cheap (single filter + order_by),
# the only meaningful cost is the per-file head_object calls — those
# happen on every render anyway since they validate file availability.


def _r2_key_for_delivery(tenant: str, job_id: str, file_type: str) -> str:
    """Build the R2 key for a delivery file.

    Must use storage._safe_filename on each segment because the writer
    side does the same: tenants stored as email addresses
    (tomas@epical.digital) get written under tomas_epical.digital/...
    in R2 — `@` and other non-[A-Za-z0-9._-] chars become underscores.
    Without sanitisation here, the portal builds the unsanitised key,
    head_object returns NoSuchKey, and the operator sees
    "SIN PREVIEW" + dash file sizes on the listing even though the
    files exist in R2 under the correct (sanitised) path.
    """
    info = _DELIVERY_FILE_TYPES[file_type]
    return (
        f"{storage._safe_filename(tenant)}"
        f"/{storage._safe_filename(job_id)}"
        f"/{storage._safe_filename(info['filename'])}"
    )


def _delivery_safe_filename(artist: str, song: str) -> str:
    """User-facing download filename for the Content-Disposition header."""
    base = f"{artist} - {song}".strip()
    out = "".join(c if (c.isalnum() or c in " -_") else "_" for c in base).strip()
    return out.replace(" ", "_") or "video"


def _verify_portal_token(authorization: str | None) -> None:
    """Raise 401 unless the X-Portal-Token header matches the configured
    portal password. The portal is a static page so we can't use JWT —
    this is the same shared password Universal enters in the portal UI."""
    expected = os.environ.get("DELIVERY_PORTAL_TOKEN") or os.environ.get("DELIVERY_PASSWORD")
    if not expected:
        # If the env var isn't set the portal endpoints are effectively
        # disabled — better than silently allowing unauth access.
        raise HTTPException(status_code=503, detail="Portal not configured")
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid portal token")


class SendToUMGRequest(BaseModel):
    """Optional overrides when publishing a job to the portal."""
    label: str | None = None  # default: "Renderizado" or "Opción N"


@app.post("/admin/deliveries/from-job/{job_id}")
async def admin_create_delivery_from_job(
    job_id: str,
    body: SendToUMGRequest | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Publish an approved job to the UMG portal. Admin only.

    Behaviour when the same job is re-sent: the existing active row is
    UPDATED (label refreshed, added_at bumped) instead of duplicated.
    Matches the manual workflow today — corrected re-renders replace
    the previous version rather than stack up as "Opción N".
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    job = db.query(Job).filter(Job.job_id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done" or job.approved_at is None:
        raise HTTPException(
            status_code=400,
            detail="Job must be approved (status=done) before it can be published",
        )

    # Validate all 5 files exist in R2. If even one is missing we refuse
    # to create the Delivery — a half-empty portal entry is worse than
    # asking the operator to wait for the render to finish.
    missing = []
    for ft in _DEFAULT_DELIVERY_FILE_TYPES:
        key = _r2_key_for_delivery(job.tenant_id, job.job_id, ft)
        if not storage.object_exists(key):
            missing.append(ft)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Files not yet in R2: {', '.join(missing)}. Wait for the render to finish.",
        )

    # Compute label. If caller passed one, honor it. Otherwise: first
    # delivery for this song gets "Renderizado"; subsequent ones get
    # "Opción N". Matches the manual items.json conventions.
    label = (body.label if body else None) or _compute_default_delivery_label(
        db, job.artist, job.song_title
    )

    # Replace-not-duplicate: if there's already an active Delivery for
    # this job_id, update it in place. Operator clicks "Enviar a UMG"
    # again after a re-render → we refresh the label + timestamp, the
    # R2 files stay the same (worker overwrites on edit).
    existing = (
        db.query(Delivery)
        .filter(Delivery.job_id == job_id)
        .filter(Delivery.removed_at.is_(None))
        .first()
    )
    if existing:
        existing.label = label
        existing.file_types = _DEFAULT_DELIVERY_FILE_TYPES
        existing.added_by_user_id = current_user["id"]
        existing.added_at = datetime.now(timezone.utc)
        # Refresh snapshot in case the artist/title was corrected on the
        # job row between the original publish and now.
        existing.artist_snapshot = job.artist
        existing.song_title_snapshot = job.song_title or ""
        existing.tenant_snapshot = job.tenant_id
        existing.frame_size_snapshot = (job.umg_spec or {}).get("frame_size")
        delivery = existing
        action = "delivery.update"
    else:
        delivery = Delivery(
            job_id=job_id,
            label=label,
            file_types=_DEFAULT_DELIVERY_FILE_TYPES,
            artist_snapshot=job.artist,
            song_title_snapshot=job.song_title or "",
            tenant_snapshot=job.tenant_id,
            frame_size_snapshot=(job.umg_spec or {}).get("frame_size"),
            added_by_user_id=current_user["id"],
            added_at=datetime.now(timezone.utc),
        )
        db.add(delivery)
        action = "delivery.create"

    db.add(AuditLog(
        user_id=current_user["id"],
        action=action,
        detail={"job_id": job_id, "label": label, "artist": job.artist, "song": job.song_title},
    ))
    db.commit()
    db.refresh(delivery)

    return {
        "ok": True,
        "delivery_id": delivery.id,
        "job_id": delivery.job_id,
        "label": delivery.label,
        "artist": delivery.artist_snapshot,
        "song": delivery.song_title_snapshot,
        "replaced": action == "delivery.update",
    }


def _compute_default_delivery_label(db: Session, artist: str, song_title: str | None) -> str:
    """Default label for a new delivery.

    Rule: first active delivery for an (artist, song) gets "Renderizado".
    If there's already at least one, the new one is "Opción N" where N
    is the count of active deliveries + 1. This mirrors the way items.json
    labels were written by hand.
    """
    existing_count = (
        db.query(Delivery)
        .filter(Delivery.artist_snapshot == artist)
        .filter(Delivery.song_title_snapshot == (song_title or ""))
        .filter(Delivery.removed_at.is_(None))
        .count()
    )
    if existing_count == 0:
        return "Renderizado"
    return f"Opción {existing_count + 1}"


@app.delete("/admin/deliveries/{delivery_id}")
async def admin_delete_delivery(
    delivery_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a portal entry. Admin only (JWT)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return _soft_delete_delivery(db, delivery_id, current_user["id"])


def _soft_delete_delivery(db: Session, delivery_id: int, actor_user_id: int | None):
    delivery = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    if delivery is None or delivery.removed_at is not None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    delivery.removed_at = datetime.now(timezone.utc)
    delivery.removed_by_user_id = actor_user_id
    db.add(AuditLog(
        user_id=actor_user_id,
        action="delivery.delete",
        detail={
            "delivery_id": delivery_id,
            "job_id": delivery.job_id,
            "artist": delivery.artist_snapshot,
            "song": delivery.song_title_snapshot,
        },
    ))
    db.commit()
    return {"ok": True}


@app.delete("/api/deliveries/{delivery_id}")
async def portal_delete_delivery(
    delivery_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Soft-delete from the portal itself. Auth: shared portal token."""
    _verify_portal_token(x_portal_token)
    # actor_user_id=None because the portal has no per-user identity.
    # The audit log entry records the action and which delivery; if we
    # later add per-recipient logins to the portal this will carry their
    # user id instead.
    return _soft_delete_delivery(db, delivery_id, actor_user_id=None)


@app.post("/api/deliveries/{delivery_id}/change-request")
async def portal_submit_change_request(
    delivery_id: int,
    body: dict,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Portal user (UMG) submits a free-form comment asking for changes
    on a specific delivery. Operator picks it up in the GenLy admin.

    Body: {"comment": "<text>"}.

    Auth via X-Portal-Token (same shared password as the rest of /api).
    """
    _verify_portal_token(x_portal_token)
    comment = (body.get("comment") or "").strip() if isinstance(body, dict) else ""
    if not comment:
        raise HTTPException(status_code=400, detail="El comentario no puede estar vacío.")
    # Cap at 5000 chars — generous for paragraph-style feedback but
    # stops a runaway client from filling the table with megabytes.
    if len(comment) > 5000:
        raise HTTPException(
            status_code=400,
            detail="El comentario es demasiado largo (máximo 5000 caracteres).",
        )
    delivery = (
        db.query(Delivery)
        .filter(Delivery.id == delivery_id)
        .filter(Delivery.removed_at.is_(None))
        .first()
    )
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery no encontrada.")
    cr = DeliveryChangeRequest(
        delivery_id=delivery_id,
        comment=comment,
    )
    db.add(cr)
    db.add(AuditLog(
        user_id=None,
        action="delivery.change_request.create",
        detail={
            "delivery_id": delivery_id,
            "job_id": delivery.job_id,
            "artist": delivery.artist_snapshot,
            "song": delivery.song_title_snapshot,
            "comment_preview": comment[:200],
        },
    ))
    db.commit()
    db.refresh(cr)
    return {"ok": True, "id": cr.id, "submitted_at": cr.submitted_at.isoformat()}


@app.post("/api/deliveries/{delivery_id}/approve")
async def portal_approve_delivery(
    delivery_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Portal user (UMG) approves a delivery via the "Aprobar" button.

    Idempotent — a second click on an already-approved row returns the
    existing timestamp instead of erroring. approved_by_label defaults
    to "UMG" because the portal authenticates via a shared password
    (no per-user identity); future portals with individual logins can
    plumb the user identifier through here.

    Auth: shared portal token (same envelope as the rest of /api).
    """
    _verify_portal_token(x_portal_token)
    delivery = (
        db.query(Delivery)
        .filter(Delivery.id == delivery_id)
        .filter(Delivery.removed_at.is_(None))
        .first()
    )
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery no encontrada.")
    if delivery.approved_at is not None:
        return {
            "ok": True,
            "already_approved": True,
            "approved_at": delivery.approved_at.isoformat(),
            "approved_by_label": delivery.approved_by_label,
        }
    delivery.approved_at = datetime.now(timezone.utc)
    delivery.approved_by_label = "UMG"
    db.add(AuditLog(
        user_id=None,
        action="delivery.approve",
        detail={
            "delivery_id": delivery_id,
            "job_id": delivery.job_id,
            "artist": delivery.artist_snapshot,
            "song": delivery.song_title_snapshot,
            "label": delivery.label,
        },
    ))
    db.commit()
    return {
        "ok": True,
        "approved_at": delivery.approved_at.isoformat(),
        "approved_by_label": delivery.approved_by_label,
    }


@app.post("/api/deliveries/{delivery_id}/un-approve")
async def portal_unapprove_delivery(
    delivery_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Portal user undoes a previous approval. Clears approved_at and
    approved_by_label so the row goes back to pending state on the
    portal listing. Idempotent — calling on an unapproved row is a no-op.
    """
    _verify_portal_token(x_portal_token)
    delivery = (
        db.query(Delivery)
        .filter(Delivery.id == delivery_id)
        .filter(Delivery.removed_at.is_(None))
        .first()
    )
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery no encontrada.")
    if delivery.approved_at is None:
        return {"ok": True, "already_pending": True}
    delivery.approved_at = None
    delivery.approved_by_label = None
    db.add(AuditLog(
        user_id=None,
        action="delivery.un_approve",
        detail={
            "delivery_id": delivery_id,
            "job_id": delivery.job_id,
            "artist": delivery.artist_snapshot,
            "song": delivery.song_title_snapshot,
        },
    ))
    db.commit()
    return {"ok": True}


@app.get("/api/deliveries/meta")
async def portal_get_meta(
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
):
    """Title/description/expiry for the portal header. Public (portal token)."""
    _verify_portal_token(x_portal_token)
    import time
    return {
        "title": "Entregables — GenLy AI",
        "description": (
            "Lyric videos generados con GenLy AI. Cada canción puede ofrecer más de "
            "una versión; elegí la que mejor se adapte a tu uso (ProRes master para "
            "broadcast, MP4 para web/YouTube, Short vertical y Thumbnail)."
        ),
        # URLs en /items son válidas por 7 días desde la firma. El cache TTL
        # es de 60s así que efectivamente las URLs entregadas duran entre
        # 7d-60s y 7d. Mostramos 7d para no confundir al cliente.
        "expires_at_ts": time.time() + _DELIVERY_URL_EXPIRY_S,
    }


@app.get("/api/deliveries/items")
async def portal_get_items(
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
):
    """Return the portal listing in the shape the frontend expects.

    Queries the DB fresh on every call. Previous in-process cache broke
    under Railway's multi-worker setup — see the module-level note next
    to _DELIVERY_URL_EXPIRY_S."""
    _verify_portal_token(x_portal_token)
    import time
    from concurrent.futures import ThreadPoolExecutor

    now = time.time()
    deliveries = (
        db.query(Delivery)
        .filter(Delivery.removed_at.is_(None))
        .order_by(Delivery.artist_snapshot, Delivery.song_title_snapshot, Delivery.added_at)
        .all()
    )

    # Resolve every (delivery, file) pair's R2 size in parallel BEFORE
    # building the response. Sequential head_object calls were the root
    # cause of Vercel 502s on the portal: 5 files × ~30 deliveries × 200ms
    # blew past the 30s rewrite timeout. A bounded thread pool brings the
    # whole batch down to a couple of seconds. Per-call timeout is short
    # so a single hung R2 request can't poison the listing — failed
    # head_objects fall through to size=None / available=False, same as
    # the old per-iteration except.
    head_jobs: list[tuple[int, str, str]] = []  # (delivery_idx, file_type, r2_key)
    for di, d in enumerate(deliveries):
        for ft in (d.file_types or []):
            if ft not in _DELIVERY_FILE_TYPES:
                continue
            r2_key = _r2_key_for_delivery(d.tenant_snapshot, d.job_id, ft)
            head_jobs.append((di, ft, r2_key))

    size_map: dict[tuple[int, str], int | None] = {}

    # File sizes are immutable once rendered, so HEAD'ing R2 for every file
    # on every page load is pure waste — and it was the reliability bug:
    # 5 files × N deliveries × R2 latency spikes past Vercel's 30s rewrite
    # timeout when R2 is slow → "no se pudo cargar" on the live portal.
    # Cache sizes in Redis (shared across the API replicas) so each object
    # is HEAD'd at most once. 30-day TTL so a re-rendered file self-heals.
    _rcache = None
    try:
        from queue_jobs import _init_redis
        _rcache, _, _ = _init_redis()
    except Exception:
        _rcache = None

    uncached: list[tuple[int, str, str]] = []
    for di, ft, r2_key in head_jobs:
        cached = None
        if _rcache is not None:
            try:
                raw = _rcache.get("dlsize:" + r2_key)
                if raw is not None:
                    cached = int(raw)
            except Exception:
                cached = None
        if cached is not None:
            size_map[(di, ft)] = cached
        else:
            uncached.append((di, ft, r2_key))

    def _head_size(job: tuple[int, str, str]) -> tuple[tuple[int, str], str, int | None]:
        di, ft, r2_key = job
        try:
            client = storage._get_client()
            if client is None:
                return (di, ft), r2_key, None
            head = client.head_object(Bucket=storage.R2_BUCKET, Key=r2_key)
            return (di, ft), r2_key, head.get("ContentLength")
        except Exception:
            return (di, ft), r2_key, None

    if uncached:
        # Only HEAD the files we haven't cached yet. 16-way concurrency cap
        # so we don't open hundreds of R2 sockets at once.
        with ThreadPoolExecutor(max_workers=16) as pool:
            for k, r2_key, v in pool.map(_head_size, uncached):
                size_map[k] = v
                if v is not None and _rcache is not None:
                    try:
                        _rcache.setex("dlsize:" + r2_key, 2592000, int(v))
                    except Exception:
                        pass

    # Bulk-fetch change requests for all visible deliveries in one query
    # (avoid N+1). Group into {delivery_id: [requests]} so the per-version
    # loop below can attach them without another DB round-trip.
    cr_map: dict[int, list[dict]] = {}
    if deliveries:
        delivery_ids = [d.id for d in deliveries]
        crs = (
            db.query(DeliveryChangeRequest)
            .filter(DeliveryChangeRequest.delivery_id.in_(delivery_ids))
            .order_by(DeliveryChangeRequest.submitted_at.desc())
            .all()
        )
        for cr in crs:
            cr_map.setdefault(cr.delivery_id, []).append({
                "id": cr.id,
                "comment": cr.comment,
                "submitted_at": cr.submitted_at.isoformat() if cr.submitted_at else None,
                "resolved_at": cr.resolved_at.isoformat() if cr.resolved_at else None,
                "resolution_note": cr.resolution_note,
            })

    # Group by (artist, song). Within each group, versions stay in
    # added_at order (oldest first), matching how items.json reads.
    songs: dict[tuple[str, str], dict] = {}
    file_type_labels = {ft: info["label"] for ft, info in _DELIVERY_FILE_TYPES.items()}

    for di, d in enumerate(deliveries):
        key = (d.artist_snapshot, d.song_title_snapshot)
        bucket = songs.setdefault(key, {"artist": d.artist_snapshot, "song": d.song_title_snapshot, "versions": []})
        files = []
        preview_url: str | None = None
        short_preview_url: str | None = None
        for ft in (d.file_types or []):
            if ft not in _DELIVERY_FILE_TYPES:
                continue
            r2_key = _r2_key_for_delivery(d.tenant_snapshot, d.job_id, ft)
            dl_name = f"{_delivery_safe_filename(d.artist_snapshot, d.song_title_snapshot)}.{_DELIVERY_FILE_TYPES[ft]['ext']}"
            try:
                url = storage.generate_signed_url(
                    r2_key,
                    expiry_seconds=_DELIVERY_URL_EXPIRY_S,
                    download_filename=dl_name,
                )
                size_bytes = size_map.get((di, ft))
                available = url is not None and size_bytes is not None
            except Exception:
                url = None
                size_bytes = None
                available = False
            files.append({
                "type": ft,
                "label": file_type_labels.get(ft, ft),
                "url": url,
                "size": _fmt_size_mb(size_bytes),
                "available": available,
            })
            if ft == "video" and url is not None:
                preview_url = storage.generate_signed_url(r2_key, expiry_seconds=_DELIVERY_URL_EXPIRY_S)
            elif ft == "short" and url is not None:
                short_preview_url = storage.generate_signed_url(r2_key, expiry_seconds=_DELIVERY_URL_EXPIRY_S)
        change_requests = cr_map.get(d.id, [])
        bucket["versions"].append({
            "delivery_id": d.id,
            "job_id": d.job_id,
            "label": d.label,
            "frame_size": d.frame_size_snapshot,
            "files": files,
            "preview_url": preview_url,
            "short_preview_url": short_preview_url,
            "change_requests": change_requests,
            "pending_change_requests": sum(
                1 for cr in change_requests if cr.get("resolved_at") is None
            ),
            # Portal-side approval state. The portal hides Aprobar/
            # Rechazar when approved_at is set and shows a green pill
            # + "deshacer" link instead.
            "approved_at": d.approved_at.isoformat() if d.approved_at else None,
            "approved_by_label": d.approved_by_label,
        })

    return {
        "songs": list(songs.values()),
        "file_type_labels": file_type_labels,
        "expires_at_ts": now + _DELIVERY_URL_EXPIRY_S,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fmt_size_mb(size_bytes: int | None) -> str:
    if not size_bytes:
        return "—"
    mb = size_bytes / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


# ─────────────────────────────────────────────────────────────────────────
# Admin endpoints for change requests (operator side)
#
# UMG submits change requests via the portal (POST /api/deliveries/{id}
# /change-request). They land in delivery_change_requests pending. The
# operator reviews them here. Two actions: resolve (with optional note)
# and reopen (undo a wrong resolve). Both write AuditLog entries so the
# trail of who-did-what survives.
# ─────────────────────────────────────────────────────────────────────────

@app.get("/admin/change-requests")
async def admin_list_change_requests(
    status: str = "pending",
    limit: int = 200,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List change requests for the operator UI. Admin only.

    status: "pending" (default), "resolved", or "all".
    Returns request + delivery context (artist/song/label/frame/job_id)
    plus resolved_by username so the admin sees who acted on each one.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if status not in ("pending", "resolved", "all"):
        raise HTTPException(status_code=400, detail="status must be pending|resolved|all")
    if limit < 1 or limit > 1000:
        limit = 200

    q = db.query(DeliveryChangeRequest)
    if status == "pending":
        q = q.filter(DeliveryChangeRequest.resolved_at.is_(None))
    elif status == "resolved":
        q = q.filter(DeliveryChangeRequest.resolved_at.isnot(None))
    crs = (
        q.order_by(DeliveryChangeRequest.submitted_at.desc())
        .limit(limit)
        .all()
    )

    # Bulk-fetch deliveries + resolver usernames in one shot each (no N+1).
    delivery_ids = list({cr.delivery_id for cr in crs})
    user_ids = list({cr.resolved_by_user_id for cr in crs if cr.resolved_by_user_id})
    deliveries_by_id = {
        d.id: d
        for d in (db.query(Delivery).filter(Delivery.id.in_(delivery_ids)).all() if delivery_ids else [])
    }
    users_by_id = {
        u.id: u
        for u in (db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else [])
    }

    # Bulk-fetch the underlying jobs (NO tenant scope — this is an admin-only
    # endpoint, so the operator legitimately sees every tenant's job here)
    # plus their owners, so each card can show a preview + who generated the
    # video. Without these, the operator has to open each job in a separate
    # tab to know what they're correcting and whom to ask. Requested
    # 2026-05-20.
    from database import Job as _JobModel
    job_ids = list({d.job_id for d in deliveries_by_id.values() if d and d.job_id})
    jobs_by_jobid = {
        j.job_id: j
        for j in (db.query(_JobModel).filter(_JobModel.job_id.in_(job_ids)).all() if job_ids else [])
    }
    owner_ids = list({j.user_id for j in jobs_by_jobid.values() if j and j.user_id})
    owners_by_id = {
        u.id: u
        for u in (db.query(User).filter(User.id.in_(owner_ids)).all() if owner_ids else [])
    }

    # Short-lived signed R2 URLs for the in-card preview. Generated here
    # (admin context) rather than via the per-tenant /media-token flow,
    # which would 404 for the admin on another tenant's job. thumbnail for
    # the still, video for click-to-play. Best-effort: missing keys / R2
    # disabled just yield None and the card renders without a preview.
    import storage as _storage

    def _signed(job, file_type: str) -> str | None:
        try:
            if not (job and _storage.is_enabled()):
                return None
            key = (job.s3_keys or {}).get(file_type)
            if not key:
                return None
            return _storage.generate_signed_url(key, expiry_seconds=3600)
        except Exception:
            return None

    items = []
    for cr in crs:
        d = deliveries_by_id.get(cr.delivery_id)
        resolver = users_by_id.get(cr.resolved_by_user_id) if cr.resolved_by_user_id else None
        job = jobs_by_jobid.get(d.job_id) if d and d.job_id else None
        owner = owners_by_id.get(job.user_id) if job and job.user_id else None
        items.append({
            "id": cr.id,
            "comment": cr.comment,
            "submitted_at": cr.submitted_at.isoformat() if cr.submitted_at else None,
            "resolved_at": cr.resolved_at.isoformat() if cr.resolved_at else None,
            "resolution_note": cr.resolution_note,
            "resolved_by": resolver.username if resolver else None,
            "delivery": (
                {
                    "id": d.id,
                    "artist": d.artist_snapshot,
                    "song": d.song_title_snapshot,
                    "label": d.label,
                    "frame_size": d.frame_size_snapshot,
                    "job_id": d.job_id,
                    "tenant": d.tenant_snapshot,
                    "removed_at": d.removed_at.isoformat() if d.removed_at else None,
                    # Who generated the video — so the operator knows whom to
                    # ask when correcting. Falls back gracefully if the job
                    # or owner was deleted.
                    "owner_username": owner.username if owner else None,
                    "owner_email": owner.email if owner else None,
                    # In-card preview (signed, ~1h). thumbnail = still,
                    # video = click-to-play.
                    "thumbnail_url": _signed(job, "thumbnail"),
                    "video_url": _signed(job, "video"),
                }
                if d
                else None
            ),
        })

    # Totals are cheap and the admin UI shows them as headline counters.
    pending_count = (
        db.query(DeliveryChangeRequest)
        .filter(DeliveryChangeRequest.resolved_at.is_(None))
        .count()
    )
    resolved_count = (
        db.query(DeliveryChangeRequest)
        .filter(DeliveryChangeRequest.resolved_at.isnot(None))
        .count()
    )

    return {
        "items": items,
        "pending_count": pending_count,
        "resolved_count": resolved_count,
    }


class CreditGrantRequest(BaseModel):
    """Body para emitir créditos de regalo (promos)."""
    scope: str = "all"                       # "all" | "tenant" | "billing_group"
    tenant_id: str | None = None
    billing_group: str | None = None
    amount: int | None = None                # scope tenant/billing_group: monto explícito
    paid_amount: int | None = None           # scope "all": monto a planes pagos
    free_amount: int | None = None           # scope "all": monto a plan free
    ttl_days: int | None = None
    reason: str = "escenas_launch"
    dry_run: bool = False


@app.get("/admin/credit-grants")
async def admin_list_credit_grants(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista los créditos de regalo emitidos (persistidos), para el panel admin.
    Admin only. Devuelve los grants más recientes + un resumen de los activos."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    now = datetime.now(timezone.utc)

    def _active(g):
        if g.revoked:
            return False
        if g.expires_at is None:
            return True
        exp = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)
        return exp > now

    rows = (
        db.query(CreditGrant)
        .order_by(CreditGrant.granted_at.desc())
        .limit(200)
        .all()
    )
    items, active_accounts, active_credits, by_reason = [], 0, 0, {}
    for g in rows:
        is_active = _active(g)
        if is_active:
            active_accounts += 1
            active_credits += g.amount or 0
            by_reason[g.reason] = by_reason.get(g.reason, 0) + 1
        d = g.to_dict()
        d["active"] = is_active
        items.append(d)

    # Cuentas para el selector del panel: una CUENTA = billing_group (si existe)
    # o tenant. Así el operador puede dirigir el regalo a la cuenta madre (ej.
    # Universal) en vez de a cada usuario. El conteo es la cantidad de usuarios
    # activos que comparten ese pool.
    _accs = {}
    for _tenant, _bg in (
        db.query(User.tenant_id, User.billing_group)
        .filter(User.is_active == True)  # noqa: E712
        .all()
    ):
        key = ("billing_group", _bg) if _bg else ("tenant", _tenant)
        _accs[key] = _accs.get(key, 0) + 1
    accounts = [
        {"type": _t, "id": _v, "users": _n}
        for (_t, _v), _n in sorted(_accs.items(), key=lambda kv: -kv[1])
    ]

    return {
        "items": items,
        "summary": {
            "active_accounts": active_accounts,
            "active_credits": active_credits,
            "by_reason": by_reason,
        },
        "accounts": accounts,
    }


@app.post("/admin/credit-grants")
async def admin_create_credit_grants(
    body: CreditGrantRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Emitir créditos de regalo (promos como el lanzamiento de Escenas). Admin only.

    - scope="all": siembra UN grant por CUENTA (billing_group si existe, si no
      tenant) entre los usuarios activos. Monto por plan: free → free_amount,
      resto → paid_amount. Idempotente por `reason`: no re-otorga si la cuenta
      ya tiene un grant activo con ese reason.
    - scope="tenant" / "billing_group": un grant puntual con `amount`.

    Defaults por env: LAUNCH_CREDITS_PAID (30), LAUNCH_CREDITS_FREE (6),
    LAUNCH_CREDITS_TTL_DAYS (30). `dry_run=true` devuelve el plan sin escribir.

    El medidor (/usage) refleja el grant dentro de ~30 s (TTL del cache).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    def _env_int(name, dflt):
        # Un env mal seteado (no numérico) NO debe tumbar el endpoint con 500.
        try:
            return int(os.environ.get(name, str(dflt)))
        except (TypeError, ValueError):
            return dflt

    paid = body.paid_amount if body.paid_amount is not None else _env_int("LAUNCH_CREDITS_PAID", 30)
    free = body.free_amount if body.free_amount is not None else _env_int("LAUNCH_CREDITS_FREE", 6)
    ttl_days = body.ttl_days if body.ttl_days is not None else _env_int("LAUNCH_CREDITS_TTL_DAYS", 30)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ttl_days) if ttl_days and ttl_days > 0 else None
    reason = (body.reason or "escenas_launch").strip()

    def _has_active_grant(_bg, _tn):
        q = db.query(CreditGrant).filter(
            CreditGrant.reason == reason,
            CreditGrant.revoked.is_(False),
        )
        if _bg:
            q = q.filter(CreditGrant.billing_group == _bg)
        else:
            q = q.filter(CreditGrant.tenant_id == _tn, CreditGrant.billing_group.is_(None))
        # SQLite devuelve expires_at naive; normalizar antes de comparar con `now`.
        return any(
            g.expires_at is None
            or (g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)) > now
            for g in q.all()
        )

    created, skipped, preview = 0, 0, []

    if body.scope == "all":
        # Una CUENTA = billing_group (si existe) o tenant. Agrupamos usuarios
        # activos por esa clave; el monto sale del plan (free vs pago).
        users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
        accounts = {}
        for u in users:
            key = ("bg", u.billing_group) if u.billing_group else ("tn", u.tenant_id)
            acc = accounts.setdefault(key, {
                "billing_group": u.billing_group,
                "tenant_id": None if u.billing_group else u.tenant_id,
                "all_free": True,
            })
            if (u.plan_id or "").lower() != "free":
                acc["all_free"] = False
        for acc in accounts.values():
            amount = free if acc["all_free"] else paid
            if amount <= 0 or _has_active_grant(acc["billing_group"], acc["tenant_id"]):
                skipped += 1
                continue
            preview.append({"billing_group": acc["billing_group"],
                            "tenant_id": acc["tenant_id"], "amount": amount})
            if not body.dry_run:
                db.add(CreditGrant(
                    billing_group=acc["billing_group"], tenant_id=acc["tenant_id"],
                    amount=amount, reason=reason, granted_by=current_user["id"],
                    granted_at=now, expires_at=expires_at,
                ))
                created += 1
    else:
        bg = (body.billing_group or "").strip() or None
        tn = (body.tenant_id or "").strip() or None
        if body.scope == "billing_group" and not bg:
            raise HTTPException(status_code=400, detail="billing_group required")
        if body.scope == "tenant" and not tn:
            raise HTTPException(status_code=400, detail="tenant_id required")
        if bg:
            tn = None  # un grant es de billing_group XOR tenant
        amount = body.amount if body.amount is not None else paid
        if amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        if _has_active_grant(bg, tn):
            skipped += 1
        else:
            preview.append({"billing_group": bg, "tenant_id": tn, "amount": amount})
            if not body.dry_run:
                db.add(CreditGrant(
                    billing_group=bg, tenant_id=tn, amount=amount, reason=reason,
                    granted_by=current_user["id"], granted_at=now, expires_at=expires_at,
                ))
                created += 1

    if not body.dry_run and created:
        db.add(AuditLog(
            user_id=current_user["id"],
            action="admin.credit_grants",
            detail={"reason": reason, "scope": body.scope, "created": created,
                    "skipped": skipped,
                    "expires_at": expires_at.isoformat() if expires_at else None},
        ))
        db.commit()

    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "dry_run": body.dry_run,
        "amount_paid": paid,
        "amount_free": free,
        "ttl_days": ttl_days,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "preview": preview[:50],
    }


@app.post("/admin/change-requests/{cr_id}/resolve")
async def admin_resolve_change_request(
    cr_id: int,
    body: dict | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a change request resolved. Optional resolution_note (<=2000 chars)
    so the operator can leave a one-liner explaining what was done."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    cr = db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.resolved_at is not None:
        # Idempotent — return current state instead of erroring, so a
        # double-click in the UI doesn't surface a scary error.
        return {"ok": True, "already_resolved": True}
    note = ((body or {}).get("resolution_note") or "").strip() if isinstance(body, dict) else ""
    if len(note) > 2000:
        raise HTTPException(status_code=400, detail="resolution_note too long (max 2000)")
    cr.resolved_at = datetime.now(timezone.utc)
    cr.resolved_by_user_id = current_user["id"]
    cr.resolution_note = note or None
    db.add(AuditLog(
        user_id=current_user["id"],
        action="delivery.change_request.resolve",
        detail={
            "change_request_id": cr_id,
            "delivery_id": cr.delivery_id,
            "note_preview": note[:200] if note else None,
        },
    ))
    db.commit()
    return {"ok": True, "resolved_at": cr.resolved_at.isoformat()}


@app.post("/admin/change-requests/{cr_id}/reopen")
async def admin_reopen_change_request(
    cr_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo a resolution. The original submission stays — only the
    resolved_at/resolved_by/resolution_note get cleared. Audit log
    records who reopened it."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    cr = db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.resolved_at is None:
        return {"ok": True, "already_pending": True}
    cr.resolved_at = None
    cr.resolved_by_user_id = None
    cr.resolution_note = None
    db.add(AuditLog(
        user_id=current_user["id"],
        action="delivery.change_request.reopen",
        detail={"change_request_id": cr_id, "delivery_id": cr.delivery_id},
    ))
    db.commit()
    return {"ok": True}
