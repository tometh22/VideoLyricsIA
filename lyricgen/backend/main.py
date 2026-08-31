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


def _business_alerts_enabled() -> bool:
    """Business/churn alerts are production-only unless explicitly enabled."""
    configured = os.environ.get("BUSINESS_ALERTS_ENABLED")
    if configured is not None:
        return configured.strip().lower() in ("1", "true", "yes", "on")
    return ENVIRONMENT == "production"

# --- Sentry ---
# 2026-06-01 UMG-launch hardening: the inline sentry_sdk.init() that used
# to live here was being silently OVERRIDDEN by the second, lighter init
# inside observability.init_sentry() (called below) — the SDK keeps only
# the last init, so prod ran without release tag or SQLAlchemy tracing.
# All Sentry config now lives in observability.init_sentry() (single
# source of truth, shared with worker.py).

from fastapi import FastAPI, File, Form, Header, Query, UploadFile, HTTPException, Depends, Request, Response, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from auth import (
    authenticate_user,
    create_token,
    start_login_session,
    invalidate_user_access,
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
    MEDIA_TOKEN_EXPIRE_SECONDS,
    validate_password_strength,
    has_prores_access,
    has_drive_access,
    has_scenes_access,
    has_art_track_access,
    has_canvas_access,
    scenes_credit_cost,
    telemetry_enabled,
    editor_v2_enabled,
    generate_api_key,
    is_explicitly_local_environment,
    is_super_admin,
)
import storage
from machine_evidence import MachineSnapshotMissing
from datetime import datetime, timedelta, timezone

from database import (
    Job, User, UserSettings, AuditLog, APIKey, get_db, init_db,
    BackgroundAsset, AssetUsage, Delivery, DeliveryChangeRequest,
    SalesLead, UserSession, LoginSession, UiEvent, CreditGrant,
    ProductEvent, EditorDocument, EditorVersion,
    scoped_db, pool_stats,
    get_deliveries_db, deliveries_added_by, DELIVERIES_DATABASE_URL,
)
from jobs import bulk_delete_jobs, create_job, delete_job, get_job, get_all_jobs, update_job
from editor import (
    apply_quality_proposal,
    QualityProposalsDisabled,
    approve_document,
    acquire_lock,
    get_job_for_tenant,
    get_version,
    get_or_create_document,
    list_versions,
    normalize_segments,
    release_lock,
    restore_version,
    resolve_conflict,
    save_document,
    serialize_document,
    sync_legacy_snapshot,
    ensure_document,
    dismiss_quality_proposal,
    rebase_operator_suggestions_after_manual_edit,
    require_machine_snapshot,
    reject_operator_suggestion,
    record_quality_observation,
    revoke_quality_proposal_if_disabled,
)
from observability import init_sentry, init_logging, health_snapshot
from pipeline import (run_pipeline, transcribe, _normalize_movement_style,
                      CANVAS_FILE_TYPES)
from segment_timing import normalize_segments_timing, normalize_editor_segments, timing_anomalies
from queue_jobs import enqueue_pipeline, enqueue_edit, queue_depth, enqueue_prores_prewarm, enqueue_drive_delivery
from render_spec import umg_catalog, validate_umg_config
from transcription_language import (
    detect_text_languages,
    normalize_language,
    resolve_transcription_language,
)
from provenance import job_was_delivered
from batch_profiles import (
    RenderProfileError, normalize_render_profile, pipeline_fields,
)
from billing import router as billing_router
from admin import router as admin_router
from corpus import router as corpus_router
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


# --- Graceful DB-pressure / race handlers ---
# Two SQLAlchemy exceptions were reaching Sentry as *unhandled* 500s (and,
# through the BaseHTTPMiddleware task group, as noisy "ExceptionGroup:
# unhandled errors in a TaskGroup") even though each has a correct, non-500
# HTTP semantics. Handling them here converts the crash into the right
# status code AND stops it from being captured as an error event.
#
#   - pool TimeoutError: every connection in the per-process pool is checked
#     out and the 30s checkout wait elapsed (seen in get_current_user under
#     UMG polling bursts). This is backpressure, not a bug — 503 + Retry-After
#     tells our own polling frontend to back off and retry instead of erroring.
#
#   - ObjectDeletedError: the row (typically a Job) was hard-deleted by the
#     reaper or a concurrent bulk-delete while this request still held the ORM
#     object and then touched a lazy attribute. The resource is simply gone,
#     so 404 is the honest answer — same as if the id had never existed.
async def _pool_timeout_handler(request: Request, exc: SQLAlchemyTimeoutError):
    logger.warning(
        "DB pool exhausted on %s %s — returning 503 (backpressure, not a crash)",
        request.method, request.url.path,
    )
    return JSONResponse(
        {"detail": "El servidor está momentáneamente saturado. Reintentá en unos segundos."},
        status_code=503,
        headers={"Retry-After": "3"},
    )


async def _object_deleted_handler(request: Request, exc: ObjectDeletedError):
    logger.info(
        "ObjectDeleted on %s %s — row removed mid-request (reaper/bulk-delete race), returning 404",
        request.method, request.url.path,
    )
    return JSONResponse(
        {"detail": "El recurso ya no existe."},
        status_code=404,
    )


app.add_exception_handler(SQLAlchemyTimeoutError, _pool_timeout_handler)
app.add_exception_handler(ObjectDeletedError, _object_deleted_handler)
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
# checkout gets a fresh one. A small bounded retry window absorbs the short
# bursts Railway showed during this incident.
#
# Implemented as raw ASGI middleware (not BaseHTTPMiddleware) because we
# need to buffer the request body before the inner app consumes it and
# then synthesize a fresh `receive` callable on retry. BaseHTTPMiddleware
# does not let you re-call the inner app with a replayed body.
_TRANSIENT_DB_MARKERS = (
    "ssl connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
    "bad record mac",
    "ssl syscall error",
    "eof detected",
)

# Hard cap on request bodies eligible for replay-on-retry. Above this we
# let the request fail naturally — buffering 50+ MB MP3 uploads into
# memory just to recover from a transient DB blip costs more than the bug.
_RETRY_BODY_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


class DbTransientRetryMiddleware:
    """Retry a bounded number of times if Postgres drops mid-request.

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
            path = scope.get("path", "")
            try:
                content_length = int(headers.get("content-length", "") or 0)
            except ValueError:
                content_length = 0
            # These POST endpoints are explicitly bodyless and idempotent.
            # Browsers commonly omit Content-Length for an empty fetch body,
            # so replaying an explicit empty body is the safe retry contract.
            empty_idempotent_post = method == "POST" and (
                re.fullmatch(r"/editor/[^/]+/lock/heartbeat", path) is not None
                or path == "/telemetry/heartbeat"
            )
            if empty_idempotent_post:
                body_buffered = True
            elif (not content_type.startswith("multipart/")
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

        max_attempts = 3
        captured_exc = None
        replayable = body_buffered or method in ("GET", "HEAD", "OPTIONS", "DELETE")

        for attempt in range(max_attempts):
            response_started = False

            async def wrapped_send(message):
                nonlocal response_started
                if message.get("type") == "http.response.start":
                    response_started = True
                await send(message)

            # Preserve the real disconnect channel on the first bodyless
            # request. StreamingResponse (notably /events SSE) listens to it;
            # replacing it with an immediate synthetic disconnect cancels the
            # stream before its first event. Buffered bodies and retries need
            # a synthetic request body because the original was consumed.
            attempt_receive = (
                _make_replay_receive(body_bytes)
                if body_buffered or (replayable and attempt > 0)
                else receive
            )
            try:
                await self.app(scope, attempt_receive, wrapped_send)
                return
            except OperationalError as exc:
                error_text = str(exc).lower()
                if not any(marker in error_text for marker in _TRANSIENT_DB_MARKERS):
                    raise
                captured_exc = captured_exc or exc
                if response_started:
                    logger.warning(
                        "Transient DB error on %s %s after response started — can't retry",
                        method, scope.get("path", ""),
                    )
                    raise
                if not replayable:
                    logger.warning(
                        "Transient DB error on %s %s but body not buffered — not retrying",
                        method, scope.get("path", ""),
                    )
                    raise
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "Transient DB error on %s %s — retrying (%d/%d)",
                        method, scope.get("path", ""), attempt + 1, max_attempts - 1,
                    )
                    try:
                        from ops_metrics import increment
                        increment("db_transient_retry")
                    except Exception:
                        pass
                    await asyncio.sleep(0.15 * (2 ** attempt))
                    continue

        # A transient infrastructure failure is recoverable and must not look
        # like an application 500. Capture one richly-tagged Sentry event,
        # then give the browser an explicit retry contract.
        path = scope.get("path", "")
        logger.error(
            "Transient DB error exhausted %d attempts on %s %s — returning 503",
            max_attempts, method, path,
        )
        try:
            from ops_metrics import increment
            increment("db_transient_exhausted")
        except Exception:
            pass
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as sentry_scope:
                sentry_scope.set_tag("db.transient", True)
                sentry_scope.set_tag("db.retry_attempts", max_attempts)
                sentry_scope.set_tag("http.path", path)
                job_match = re.search(r"/([0-9a-f]{12})(?:/|$)", path)
                if job_match:
                    sentry_scope.set_tag("job_id", job_match.group(1))
                sentry_sdk.capture_exception(captured_exc)
        except Exception:
            pass
        response = JSONResponse(
            status_code=503,
            content={
                "detail": "La base de datos está reconectando. Reintentá en unos segundos.",
                "code": "db_transient_unavailable",
            },
            headers={"Retry-After": "2"},
        )
        await response(scope, _make_replay_receive(b""), send)


def _make_replay_receive(body: bytes):
    """Return an ASGI `receive` callable that yields `body` once and
    then waits like a still-connected client until the response completes."""
    delivered = False
    connected = asyncio.Event()

    async def _replay_receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await connected.wait()
        return {"type": "http.disconnect"}

    return _replay_receive


async def _disconnect_receive():
    return {"type": "http.disconnect"}


class RejectNulPathMiddleware:
    """Reject URL paths containing a NUL before they reach DB lookups."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and "\x00" in scope.get("path", ""):
            await JSONResponse(
                {"detail": "Malformed request path."},
                status_code=400,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(DbTransientRetryMiddleware)
app.add_middleware(RejectNulPathMiddleware)


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


@app.middleware("http")
async def enforce_submissions_switch(request: Request, call_next):
    """Reject new/enqueued work while allowing reads and in-flight uploads."""
    from ops_control import get_submissions_state, is_submission_path

    if is_submission_path(request.method, request.url.path):
        state = await asyncio.to_thread(get_submissions_state)
        if state.get("paused"):
            from ops_metrics import increment
            await asyncio.to_thread(increment, "submissions_blocked")
            retry_after = str(state.get("retry_after") or 60)
            logger.warning(
                "[OPS] submission blocked method=%s path=%s reason=%s",
                request.method, request.url.path, state.get("reason") or "maintenance",
            )
            return JSONResponse(
                status_code=503,
                content={
                    "code": "submissions_paused",
                    "detail": state.get("reason") or "New submissions are temporarily paused.",
                    "until": state.get("until"),
                },
                headers={"Retry-After": retry_after},
            )
    return await call_next(request)


# --- Include routers ---
app.include_router(billing_router)
app.include_router(admin_router)
app.include_router(corpus_router)


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
    # Deployment lockstep evidence for the background-policy rollout. Railway
    # runs API, Worker and ShortWorker as separate services; this line makes a
    # mixed-commit or mixed-flag fleet immediately visible in their logs.
    from background_policy import (
        POLICY_ENV as _bg_policy_env,
        POLICY_VERSION as _bg_policy_version,
        VALID_POLICY_MODES as _bg_policy_modes,
        policy_mode as _bg_policy_mode,
    )
    from observability import _resolve_release as _resolve_runtime_release
    logger.info(
        "[BG_POLICY][STARTUP] process=api release=%s environment=%s "
        "policy_version=%s policy_mode=%s cache_namespace=%s",
        _resolve_runtime_release(), ENVIRONMENT,
        _bg_policy_version, _bg_policy_mode(), _bg_policy_version,
    )
    _raw_bg_policy_mode = os.environ.get(_bg_policy_env, "off").strip().lower()
    if _raw_bg_policy_mode not in _bg_policy_modes:
        logger.warning(
            "[BG_POLICY][STARTUP] invalid %s=%r; resolved fail-safe to off",
            _bg_policy_env, _raw_bg_policy_mode,
        )

    # Background reaper. Daemon → dies with the container. Single
    # instance is enough; if the API ever scales horizontally, the
    # reap_all_stuck call is idempotent (filters by status="processing"
    # so duplicate runs are no-ops on already-reaped rows).
    import time as _time
    from reaper import reap_all_stuck as _reap

    # CV3 (audit 2026-05-25) — Multi-replica coordination helper.
    # Wraps a callable in a Postgres advisory lock. Cuando hay 2+ replicas
    # API, ambas ejecutan los daemon threads (bg_cache_cleanup) — sin
    # coordinación corren N veces por ciclo, generando
    # ruido Sentry, posibles double-deletes y emails duplicados. El
    # reaper YA tiene su propio lock interno; este helper extiende el
    # mismo patrón a los otros loops.
    _BG_PREVIEW_CLEANUP_LOCK_KEY = 9118364455199102

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

    # Output files live on each worker's isolated filesystem. Cleanup runs in
    # worker.py; an API replica cannot recover or delete another container's
    # render directory.

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

    if _business_alerts_enabled():
        threading.Thread(
            target=_business_alerts_loop, daemon=True, name="business-alerts",
        ).start()
        logger.info("business-alerts thread started (daily)")
    else:
        logger.info(
            "business-alerts disabled outside production "
            "(set BUSINESS_ALERTS_ENABLED=1 to override)"
        )

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


def _legacy_background_path(filename: str) -> str | None:
    """Resolve a legacy local asset without permitting path traversal."""
    if not filename or os.path.basename(filename) != filename:
        return None
    library_root = os.path.realpath(_BACKGROUNDS_LIB)
    candidate = os.path.realpath(os.path.join(library_root, filename))
    if not candidate.startswith(library_root + os.sep):
        return None
    return candidate


def _background_asset_is_available(asset: "BackgroundAsset") -> bool:
    """Whether this API/worker topology can actually materialize an asset."""
    filename = asset.filename or ""
    if filename.startswith("library/"):
        # Tests/dev may model R2 rows without credentials. Deployed
        # or unknown environments must never advertise an object they
        # cannot fetch. Unknown values fail closed to cover config typos.
        return storage.is_enabled() or is_explicitly_local_environment(ENVIRONMENT)
    local_path = _legacy_background_path(filename)
    return bool(local_path and os.path.isfile(local_path))


def _user_can_use_asset(asset: "BackgroundAsset", current_user: dict) -> bool:
    """Tenant gate: only platform super-admins bypass asset ownership."""
    if current_user.get("is_super_admin"):
        return True
    if asset.owner_tenant_id is None:
        return True
    return asset.owner_tenant_id == current_user.get("tenant_id")


def _apply_asset_tenant_filter(query, current_user: dict):
    """Scope assets to global + caller tenant unless caller is super-admin."""
    if current_user.get("is_super_admin"):
        return query
    from sqlalchemy import or_
    return query.filter(
        or_(
            BackgroundAsset.owner_tenant_id.is_(None),
            BackgroundAsset.owner_tenant_id == current_user.get("tenant_id"),
        )
    )


class BackgroundPreviewTokensRequest(BaseModel):
    asset_ids: list[int] = Field(..., min_length=1, max_length=50)


@app.post("/backgrounds/preview-tokens")
def issue_background_preview_tokens(
    body: BackgroundPreviewTokensRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint five-minute tokens for only the visible/authorized asset ids."""
    unique_ids = list(dict.fromkeys(body.asset_ids))
    query = db.query(BackgroundAsset).filter(BackgroundAsset.id.in_(unique_ids))
    if not current_user.get("is_super_admin"):
        query = query.filter(BackgroundAsset.is_active == True)
    assets = _apply_asset_tenant_filter(query, current_user).all()
    visible = {
        asset.id for asset in assets
        if _background_asset_is_available(asset)
    }
    user_model = db.query(User).filter(User.id == current_user["id"]).first()
    return {
        "tokens": {
            str(asset_id): create_media_token(
                user_model, f"background:{asset_id}", "preview",
            )
            for asset_id in unique_ids if asset_id in visible
        },
        "expires_in": 300,
    }


@app.get("/backgrounds")
def list_backgrounds(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List active pre-approved background assets visible to the caller.

    Tenant scope: callers see global assets plus their own tenant's assets.
    Only platform super-admins see cross-tenant inventory.
    """
    q = db.query(BackgroundAsset).filter(BackgroundAsset.is_active == True)
    q = _apply_asset_tenant_filter(q, current_user)
    assets = q.order_by(BackgroundAsset.created_at.desc()).all()
    return [
        asset.to_dict() for asset in assets
        if _background_asset_is_available(asset)
    ]


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
    if (not asset
            or not _user_can_use_asset(asset, current_user)
            or not _background_asset_is_available(asset)):
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
    q = db.query(BackgroundAsset).filter(BackgroundAsset.is_active == True)  # noqa: E712
    q = _apply_asset_tenant_filter(q, current_user)
    visible_ids = {
        asset.id for asset in q.all()
        if _background_asset_is_available(asset)
    }
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
    if not _background_asset_is_available(asset):
        logger.warning(
            "background asset unavailable id=%s storage=%s",
            asset.id,
            "r2" if (asset.filename or "").startswith("library/") else "legacy_local",
        )
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
        local_path = _legacy_background_path(asset.filename)
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
        user = verify_media_token(
            token, f"background:{asset_id}", "preview", db,
        )
        asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
        if (not asset
                or not _user_can_use_asset(asset, user)
                or not _background_asset_is_available(asset)
                or (not user.get("is_super_admin") and not asset.is_active)):
            raise HTTPException(status_code=404, detail="Asset not found")
        # Snapshot the fields we need before closing the session.
        asset_filename = asset.filename
        asset_file_type = asset.file_type

    if asset_filename.startswith("library/"):
        if not storage.is_enabled():
            raise HTTPException(status_code=503, detail="Asset storage unavailable")
        url = storage.generate_signed_url(
            asset_filename,
            expiry_seconds=MEDIA_TOKEN_EXPIRE_SECONDS,
        )
        if url:
            return RedirectResponse(url, status_code=302)
        raise HTTPException(status_code=503, detail="Asset preview unavailable")

    file_path = _legacy_background_path(asset_filename)
    if not file_path or not os.path.exists(file_path):
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
    # DB/Redis/R2 probes are synchronous SDK calls; keep them off uvicorn's
    # event loop so a slow object-store HEAD cannot stall unrelated requests.
    snap = await asyncio.to_thread(health_snapshot)
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


@app.get("/health/deploy")
async def health_deploy():
    """Railway deploy gate — critical dependencies, rollout-safe fleet check.

    Exact API/worker release equality cannot be a deploy precondition: during
    a rolling release the new API necessarily starts while the old worker
    fleet is still serving. Keep reporting that mismatch as degraded, while
    DB/Redis failures remain hard 503s. `/health/ready` stays strict for the
    post-deploy operational gate.
    """
    snap = await asyncio.to_thread(
        health_snapshot,
        enforce_fleet_readiness=False,
    )
    if snap.get("status") == "down":
        return JSONResponse(snap, status_code=503)
    return snap


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
    snap = await asyncio.to_thread(health_snapshot)
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
                # Art Track gateado por tenant (default OFF salvo admin). El
                # front oculta la opción "Art Track" si esto es false.
                "art_track": has_art_track_access(user),
                # Canvas de Spotify: SOLO admin, sin env var que lo abra.
                # El front esconde el botón de descarga si esto es false.
                "canvas": has_canvas_access(user),
                "telemetry": telemetry_enabled(),
                "editor_v2": editor_v2_enabled(user),
                # Versión B (letra anclada): el frontend gatea el textarea
                # del wizard y el botón "Re-sincronizar con IA" con esto.
                "anchor_lyrics": _anchor_lyrics_enabled(),
                "youtube_publish": _youtube_publish_enabled(),
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
            commit=False,
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
                # Art Track gateado por tenant (default OFF salvo admin). El
                # front oculta la opción "Art Track" si esto es false.
                "art_track": has_art_track_access(user),
                # Canvas de Spotify: SOLO admin, sin env var que lo abra.
                # El front esconde el botón de descarga si esto es false.
                "canvas": has_canvas_access(user),
                "telemetry": telemetry_enabled(),
                "editor_v2": editor_v2_enabled(user),
                # Versión B (letra anclada): el frontend gatea el textarea
                # del wizard y el botón "Re-sincronizar con IA" con esto.
                "anchor_lyrics": _anchor_lyrics_enabled(),
                "youtube_publish": _youtube_publish_enabled(),
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

    user = verify_password_reset_token(db, body.token, commit=False)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.hashed_password = pwd_context.hash(body.password)
    invalidate_user_access(db, user)
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
            "art_track": has_art_track_access(_u),
            "canvas": has_canvas_access(_u),
            "telemetry": telemetry_enabled(),
            "editor_v2": editor_v2_enabled(_u),
            # Versión B (letra anclada): el frontend gatea el textarea
            # del wizard y el botón "Re-sincronizar con IA" con esto.
            "anchor_lyrics": _anchor_lyrics_enabled(),
            "youtube_publish": _youtube_publish_enabled(),
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
    invalidate_user_access(db, user, keep_jti=current_user.get("jti"))
    db.add(AuditLog(
        user_id=user.id, action="auth.change_password",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    # The current session row survives, but its old token has the stale
    # auth_version. Return a replacement bound to the same device/session.
    token = create_token(user, jti=current_user.get("jti"))
    return {"ok": True, "token": token}


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


def _save_custom_background(background_file, job_dir: str, job_id: str, tenant_id: str,
                            persist_cache: bool = True):
    """Materializa el fondo subido por el usuario: valida (extensión +
    magic bytes + tamaño), escribe a disco, sube a R2 y — clave del fix
    2026-06-11 — persiste la key en `bg_r2_key_cached` para que los edits
    rápidos y /retry preserven el archivo del usuario en vez de
    regenerar con Veo. Devuelve (bg_path, bg_r2_key) o (None, None) si
    no vino archivo.

    persist_cache=False lo usa el upload de fondo custom en EDICIÓN
    (POST /edit/{job}/custom-background): ahí el archivo aún no es el
    fondo durable — el job ya tiene un video renderizado con el fondo
    viejo, y bg_r2_key_cached recién debe apuntar al nuevo DESPUÉS de que
    el edit re-renderice y valide (vía _pending_background_recache, igual
    que background_library). Persistirlo antes haría que un edit de
    tipografía posterior reusara un fondo que el operador nunca aprobó."""
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
        if bg_r2_key and persist_cache:
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


def _audit_cross_tenant_access(db: Session, current_user: dict, job: dict, kind: str,
                               commit: bool = True) -> None:
    """Deja rastro cuando un admin accede a media/edición de OTRO tenant.

    Parte del contrato de la apertura cross-tenant: la visibilidad de
    plataforma para admins viene con trail de auditoría (compliance UMG).
    Best-effort: un fallo acá no bloquea el acceso.

    `commit=False` para callers que sostienen un `with_for_update()` (p.ej.
    /edit): un commit intermedio soltaría el row-lock de quota antes de
    tiempo. En ese caso el AuditLog se persiste con el commit final del flow."""
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
        if commit:
            db.commit()
    except Exception as e:
        logger.warning("[AUDIT] cross-tenant access log failed: %s", e)


def _quota_scope_key(current_user: dict) -> str:
    """Stable lock identity for the same account scope used by usage queries."""
    billing_group = (current_user.get("billing_group") or "").strip()
    if billing_group:
        return f"billing_group:{billing_group}"
    return f"tenant:{current_user['tenant_id']}"


def _lock_quota_scope(db: Session, current_user: dict) -> None:
    """Serialize count → mutation for every member of a billing account.

    Locking one User row is insufficient because quota is tenant-wide (or
    billing-group-wide): two different users could each lock their own row and
    both spend the final credit. A transaction-scoped advisory lock gives the
    logical account a single mutex without introducing a new account table.
    """
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if bind.dialect.name != "postgresql":
        raise RuntimeError("atomic quota locking requires PostgreSQL")
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": _quota_scope_key(current_user)},
    ).scalar()


def _lock_user_for_quota(db: Session, user_id: int) -> None:
    """Compatibility wrapper for older tests/callers.

    New code must call :func:`_lock_quota_scope` with the complete account
    identity. The wrapper resolves the user before taking that logical lock.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return
    _lock_quota_scope(db, user.to_dict())


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

        # Do not commit here. Quota callers hold a transaction-scoped account
        # lock and must keep it until the job/approval mutation commits.
        db.add(AuditLog(user_id=user_obj.id, action=action, detail={"percent": percent}))

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


def _enforce_plan_quota(
    db: Session,
    current_user: dict,
    credits_needed: int = 1,
    *,
    lock_scope: bool = True,
    send_alert: bool = True,
) -> None:
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
    if lock_scope:
        _lock_quota_scope(db, current_user)
    usage = get_plan_usage(db, current_user["id"], tenant_id, plan,
                           billing_group=current_user.get("billing_group"))
    if send_alert and plan != "unlimited" and usage["percent"] >= 80:
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


def _commit_pipeline_publication(
    db: Session,
    job,
    purpose: str,
    *,
    mp3_path: str | None,
    artist: str,
    style: str,
    plan: str,
    tenant_id: str,
    **pipeline_kwargs,
) -> dict:
    """Atomically persist a job mutation and its durable RQ publication."""
    from transactional_outbox import (
        create_pipeline_outbox_event,
        dispatch_outbox_event,
    )

    event = create_pipeline_outbox_event(
        db,
        job=job,
        purpose=purpose,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=plan,
        tenant_id=tenant_id,
        pipeline_kwargs=pipeline_kwargs,
    )
    event_id = event.id
    db.commit()
    delivery = dispatch_outbox_event(
        event_id, pipeline_publisher=enqueue_pipeline,
    )
    if delivery.get("status") != "dispatched":
        logger.warning(
            "[OUTBOX] pipeline publication pending job=%s event=%s status=%s",
            job.job_id, event_id, delivery.get("status"),
        )
        try:
            from queue_jobs import ensure_job_outbox_reconciler_scheduled
            ensure_job_outbox_reconciler_scheduled()
        except Exception as exc:
            logger.warning("[OUTBOX] reconciler scheduling failed: %s", exc)
    return delivery


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
    # Versión B (ANCHOR_LYRICS_ENABLED, default off): letra oficial pegada
    # por el operador — se ancla con CTC en vez de usar el texto transcrito.
    anchor_lyrics: str = Field(default="", max_length=20000)


@app.post("/transcribe-uploaded")
@limiter.limit("60/minute")
async def transcribe_uploaded(
    request: Request,
    body: _TranscribeUploadedReq,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Promote an awaiting_upload job into the transcription flow.

    The default async path only verifies stored size and enqueues; ShortWorker
    owns R2 materialization and validation. The legacy synchronous fallback
    still downloads locally because the API process consumes the audio.

    Returns the legacy /transcribe payload in sync mode. Async returns the
    queued job id and the frontend polls for the editor payload.
    """
    from jobs import get_job_model, supersede_sibling_drafts, touch_user_activity
    job_row = get_job_model(db, body.job_id)
    if (not job_row
            or job_row.user_id != current_user["id"]
            or job_row.tenant_id != current_user["tenant_id"]):
        raise HTTPException(status_code=404, detail="Job not found.")
    # Idempotent browser retry: once the durable intent exists, do not create
    # another outbox event or touch the active RQ record. This check must
    # precede the general allowed-state guard below.
    if (
        job_row.status == "transcribing_queued"
        and job_row.active_transcription_attempt_id
    ):
        return {
            "job_id": job_row.job_id,
            "status": "transcribing_queued",
            "deduplicated": True,
        }
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
    if int(getattr(job_row, "segments_revision", 0) or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="La letra ya tiene ediciones guardadas; creá una nueva transcripción para no sobrescribirlas.",
        )

    # Back-to-back upload tickets can resolve out of order.  Whichever ID the
    # authenticated browser explicitly promotes is the live draft; revive it
    # and only soft-archive its siblings.  This commits before releasing the
    # request session for R2 I/O, so a concurrent dedup can never delete the
    # selected row (supersede_sibling_drafts is non-destructive).
    touch_user_activity(db, job_row)
    supersede_sibling_drafts(
        db, keep_job_id=job_row.job_id, user_id=current_user["id"],
        tenant_id=current_user["tenant_id"], filename=job_row.filename or "",
    )
    db.commit()

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

    # Build the destination path once and pass it to the worker. API and
    # ShortWorker do not share a filesystem on Railway, so materializing the
    # object in the API container before enqueue only makes R2 transfer the
    # same file twice. In the 03e2cb7f7321 incident that redundant download
    # held this request for 249 s while the wizard sat at 0%; the worker then
    # downloaded the WAV again and completed normally in 35 s.
    job_id = body.job_id
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    audio_path = os.path.join(job_dir, _row_filename)

    # Size gate against the REAL stored object before enqueue/download. The
    # single-PUT path never passes through /upload-multipart-complete, so a
    # client that under-declared size_bytes could otherwise land an
    # arbitrarily large object. HEAD is intentionally the only R2 operation
    # on the async request path; the worker owns download + header validation.
    import asyncio as _asyncio
    _real_size = await _asyncio.to_thread(storage.head_object_size, _r2_key)
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

    if _async_enabled:
        # Async path — enqueue + 202 + status polling.
        # Flippeamos status a "transcribing_queued" para que /transcription-status
        # devuelva un estado coherente desde el momento del enqueue.
        from database import SessionLocal as _SL
        _db2 = _SL()
        event_id = None
        try:
            _row2 = (
                _db2.query(Job)
                .filter(
                    Job.job_id == job_id,
                    Job.user_id == current_user["id"],
                    Job.tenant_id == current_user["tenant_id"],
                )
                .with_for_update()
                .first()
            )
            if _row2 is None:
                raise HTTPException(status_code=404, detail="Job not found.")
            if (
                _row2.status == "transcribing_queued"
                and _row2.active_transcription_attempt_id
            ):
                return {
                    "job_id": job_id,
                    "status": "transcribing_queued",
                    "deduplicated": True,
                }
            if _row2.status not in (
                "awaiting_upload", "transcribed_pending", "transcription_failed",
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Job is in state {_row2.status!r}, cannot transcribe.",
                )
            if int(getattr(_row2, "segments_revision", 0) or 0) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="La letra ya tiene ediciones guardadas; creá una nueva transcripción para no sobrescribirlas.",
                )
            _row2.status = "transcribing_queued"
            # Publish a frontend-recognised stage before enqueue. The wizard
            # used to sit at an unexplained 0% throughout the API-side R2
            # transfer; now enqueue is immediate and even a slow worker-side
            # download is represented as audio preparation.
            _row2.current_step = "transcribe.prepare"
            _row2.progress = 1
            # Reset the reaper clock. find_stuck_transcriptions anchors on
            # coalesce(last_progress_at, created_at) with a 120-min threshold
            # (reaper.py). A retried `transcription_failed` job has an OLD
            # created_at, so without this NOW() bump the very next reaper pass
            # would re-kill it instantly (same class of bug retry_job guards at
            # main.py:9107). Harmless for fresh awaiting_upload jobs.
            _row2.last_progress_at = datetime.now(timezone.utc)
            from transactional_outbox import create_transcription_outbox_event
            event = create_transcription_outbox_event(
                _db2,
                job=_row2,
                audio_path=audio_path,
                transcription_kwargs={
                    "language": body.language,
                    "artist": _row_artist,
                    "title": _row_title,
                    "filename": _row_filename,
                    "tenant_id": current_user.get("tenant_id", ""),
                    "live": bool(body.live),
                    "anchor_lyrics": body.anchor_lyrics or "",
                },
            )
            event_id = event.id
            _db2.commit()
        finally:
            _db2.close()
        from transactional_outbox import dispatch_outbox_event
        delivery = dispatch_outbox_event(event_id)
        if delivery.get("status") != "dispatched":
            logger.warning(
                "[OUTBOX] transcription pending job=%s event=%s status=%s",
                job_id, event_id, delivery.get("status"),
            )
            try:
                from queue_jobs import ensure_job_outbox_reconciler_scheduled
                ensure_job_outbox_reconciler_scheduled()
            except Exception as exc:
                logger.warning("[OUTBOX] transcription reconciler unavailable: %s", exc)
        # 202 Accepted con el job_id para polling. No incluye segments —
        # el frontend pollea /transcription-status hasta status=transcribed.
        return {
            "job_id": job_id,
            "status": "transcribing_queued",
            "queue_pending": delivery.get("status") != "dispatched",
        }

    # Legacy sync path (fallback con ASYNC_TRANSCRIBE_ENABLED=0).
    # Unlike the async path this process consumes the file itself, therefore
    # it must still materialize and validate it before entering Whisper.
    os.makedirs(job_dir, exist_ok=True)
    if not os.path.exists(audio_path):
        _loop = _asyncio.get_event_loop()
        for _attempt in range(5):
            # boto3 is synchronous; keep it off the uvicorn event loop.
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
                # The job is not editor-ready until the finalizer commits its
                # immutable pre-human snapshot and family hypotheses.
                _row3.status = "transcribing"
                _row3.current_step = "transcribing"
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
            filename=_row_filename,
            live=bool(body.live),
        )
        from line_evidence import freeze_result_provider_evidence
        _result = freeze_result_provider_evidence(_result)
        # Versión B: si el operador pegó la letra oficial, anclarla con CTC
        # ANTES del retime normal; si ancló, saltear el retime (no doble).
        if (body.anchor_lyrics or "").strip():
            _result = await _maybe_anchor_align(_result, audio_path, job_id,
                                                body.anchor_lyrics)
            if (
                isinstance(_result, dict)
                and (_result.get("anchor_alignment") or {}).get("status")
                == "declined"
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Recibimos la letra oficial, pero no pudimos "
                        "sincronizarla con seguridad. No la reemplazamos por "
                        "una transcripción automática. Revisá que corresponda "
                        "a esta versión del audio y reintentá."
                    ),
                )
        if not (isinstance(_result, dict)
                and _result.get("timing_source") == "anchor_ctc"):
            _result = await _maybe_ctc_retime(_result, audio_path, job_id,
                                              _row_artist, _row_title)
        _post_lang = _resolve_postprocess_language(
            body.language, _result, job_id=job_id,
        )
        _result = await _maybe_adlib_filter(
            _result, audio_path, job_id,
            live_hint=bool(getattr(body, "live", False))
            or _looks_live(_row_title, _row_filename),
            language=_post_lang,
        )
        _result = _maybe_repetition_reconcile(_result, job_id)
        _result = await _maybe_gap_rescue(_result, audio_path, job_id,
                                          _post_lang)
        _result = await _maybe_word_vote(
            _result, audio_path, job_id, _post_lang,
            live_hint=bool(getattr(body, "live", False))
            or _looks_live(_row_title, _row_filename),
        )
        _result = _maybe_chorus_snap(_result, job_id)
        _result = _maybe_phrase_segment(_result, job_id)
        from lyrics_format import format_lyrics_pass as _fmt
        _result = await _fmt(_result, language=_post_lang)
        # Último post-pase: la ventana de cada cartel debe coincidir con sus
        # propias palabras (audit 2026-08-13). Lockstep con el worker y con
        # /transcribe — si se agrega acá y no allá, los caminos divergen.
        _result = _maybe_timing_consistency(_result, job_id)
        return await _finalize_inline_transcription_quality(
            _result, audio_path, job_id, _post_lang,
            live_hint=bool(getattr(body, "live", False))
            or _looks_live(_row_title, _row_filename),
        )
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

        quality_payload = getattr(job_row, "transcription_quality", None)
        if isinstance(quality_payload, dict):
            from transcription_quality import effective_policy_mode
            quality_payload = dict(quality_payload)
            quality_payload["mode"] = effective_policy_mode(
                job_id=job_id, tenant_id=str(job_row.tenant_id or ""),
            )

        payload = {
            "job_id": job_id,
            "status": status,
            "segments": None,
            "reference_lyrics": None,
            "coverage_warning": bool(getattr(job_row, "coverage_warning", False)),
            "transcription_quality": quality_payload,
            "segments_revision": int(getattr(job_row, "segments_revision", 0) or 0),
            "recovery_source": getattr(job_row, "recovery_source", None),
            "error": None,
            "error_code": getattr(job_row, "error_code", None),
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
            detail={"code": "transcription_status_unavailable",
                    "message": "No pudimos consultar el estado de la transcripción."},
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
    va acá (audio, font, animation, transition...).

    NOTA COMPLIANCE: este modelo NO acepta bypass_content_validation /
    force_content_validation a propósito — el preview genera SIEMPRE con
    allow_people=False y valida antes de cachear. Agregar esos campos acá
    rompería el aislamiento del cache global bg_cache/ (el key no separa
    por people-policy). Test-guard en test_bg_cache_validation.py.
    """
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
    # v6 (2026-07-17): con match_lyrics el prompt del fondo depende de la
    # LETRA — sin ella el preview generaba ciego al texto y el render podía
    # heredar ese fondo por cache-hit. Entra al hash como fingerprint del
    # texto normalizado (timestamps fuera: la corrección típica es timing).
    lyrics_text: str = Field(default="", max_length=20000)


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
    from bg_preview import (
        bg_preview_enabled, compute_bg_cache_key, cache_check, cache_r2_key,
        track_request,
    )

    params = body.model_dump()
    bg_cache_key = compute_bg_cache_key(params)

    # Cache is already-paid work, not pre-generation. Serve it before both
    # spend gates so disabling new previews cannot force `/generate` to buy
    # the exact same background again.
    if cache_check(bg_cache_key):
        track_request(cache_hit=True)
        return {
            "bg_cache_key": bg_cache_key,
            "cached": True,
            "r2_key": cache_r2_key(bg_cache_key),
            "status": "bg_preview_done",
        }

    # Kill-switch de entorno en el límite de trabajo nuevo, antes del gate
    # por plan y de crear/encolar un job.
    #
    # Medido en jul-2026 sobre los dos entornos: 147 fondos pre-generados,
    # **4 reusados**. $91/mes fabricando fondos que se descartan.
    #
    # El motivo NO es que el operador cambie las opciones (eso se creyó
    # primero y los datos lo desmienten): son dos flujos que no se cruzan.
    # El 79% de los renders de staging entra por API — el bot de regresión y
    # el preflight — y esos nunca disparan preview. Y los que sí usan el
    # wizard renderizan 51-56 min después, cuando la ventana útil del
    # pre-generado es de 30-90 s. La función asume un flujo de una canción
    # de punta a punta; la producción real trabaja en lotes.
    #
    # Apagarlo NO hace esperar más al operador: hoy el render genera el fondo
    # igual porque el pre-generado ya se descartó. Sólo se deja de pagar la
    # fabricación duplicada.
    #
    # Se reusa el contrato `skipped` que el frontend ya maneja, así que
    # apagarlo no rompe la UI. `BG_PREVIEW_ENABLED=1` lo vuelve a prender.
    if not bg_preview_enabled():
        return {
            "skipped": True,
            "reason": "disabled",
            "message": "El pre-render del fondo está desactivado. El video se genera igual al apretar 'Crear video'.",
        }

    from auth import PLANS
    plan_id = (current_user.get("plan") or "free").strip()
    plan_cfg = PLANS.get(plan_id, PLANS["free"])
    if not plan_cfg.get("bg_preview_enabled", False):
        return {
            "skipped": True,
            "reason": "plan_tier",
            "message": "El pre-render del fondo está disponible en planes paid. El video se genera igual al apretar 'Crear video'.",
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
        from error_taxonomy import public_error
        error_code, error_message = public_error(exc, context="background_preview")
        update_job(job_id, status="bg_preview_failed", current_step="error",
                   error=error_message, error_code=error_code)
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
            "error_code": getattr(job_row, "error_code", None),
        }
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        logger.exception("[BG_PREVIEW_STATUS] job=%s 500: %s", job_id, exc)
        raise HTTPException(
            500,
            detail={"code": "background_preview_status_unavailable",
                    "message": "No pudimos consultar el estado del fondo."},
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
        # Keep the durable row non-runnable until the outbox intent is committed.
        initial_status="awaiting_upload",
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

    job_row = db.query(Job).filter(Job.job_id == job_id).with_for_update().one()
    job_row.status = initial_status
    job_row.current_step = "queued"
    job_row.progress = 0
    job_row.input_r2_key = input_r2_key
    _commit_pipeline_publication(
        db, job_row, "legacy_upload",
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
        initial_status="transcribing",
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
        language=language,
        artist=artist, title=title,
        filename=file.filename,
    )
    from line_evidence import freeze_result_provider_evidence
    _result = freeze_result_provider_evidence(_result)
    _result = await _maybe_ctc_retime(_result, audio_path, job_id, artist, title)
    _post_lang = _resolve_postprocess_language(
        language, _result, job_id=job_id,
    )
    _result = await _maybe_adlib_filter(_result, audio_path, job_id,
                                        live_hint=_looks_live(title, file.filename),
                                        language=_post_lang)
    _result = _maybe_repetition_reconcile(_result, job_id)
    _result = await _maybe_gap_rescue(_result, audio_path, job_id,
                                      _post_lang)
    _result = await _maybe_word_vote(
        _result, audio_path, job_id, _post_lang,
        live_hint=_looks_live(title, file.filename),
    )
    _result = _maybe_chorus_snap(_result, job_id)
    _result = _maybe_phrase_segment(_result, job_id)
    from lyrics_format import format_lyrics_pass as _fmt
    _result = await _fmt(_result, language=_post_lang)
    # Lockstep con el worker y con /transcribe-uploaded — ver el comentario ahí.
    _result = _maybe_timing_consistency(_result, job_id)
    return await _finalize_inline_transcription_quality(
        _result, audio_path, job_id, _post_lang,
        live_hint=_looks_live(title, file.filename),
    )


from ctc_cascade_veto import _ctc_cascade_veto  # noqa: E402


_LIVE_MARKER_RE = re.compile(
    r"\b(live|en\s+vivo|vivo|ac[uú]stic[oa]|unplugged|directo|concert|"
    r"session(?:es)?|sesi[oó]n)\b", re.IGNORECASE)


def _looks_live(*texts) -> bool:
    """¿Algún texto (título/filename) sugiere una versión en vivo/alternativa?
    Señal barata para armar la auditoría de sufijo. El catálogo etiqueta los
    vivos en el título ('Live In Buenos Aires 2001'); para archivos sin
    etiquetar existe el toggle del operador (body.live)."""
    return any(
        t and _LIVE_MARKER_RE.search(re.sub(r"[_-]+", " ", str(t)))
        for t in texts
    )


def _resolve_postprocess_language(requested_language, result, *, job_id: str):
    """Resolve auto once, then reuse the same language in every post-pass."""
    reference = result.get("reference_lyrics", "") if isinstance(result, dict) else ""
    resolved = resolve_transcription_language(
        requested_language,
        result=result if isinstance(result, dict) else None,
        reference_text=reference,
    )
    logger.info(
        "[LANGUAGE] requested=%s resolved=%s job=%s",
        requested_language or "auto",
        resolved or "provider-auto",
        job_id,
    )
    return resolved


def _quality_mutation_authorized(job_id: str) -> bool:
    """Authorize legacy lyric mutations only behind the signed v5 gate."""
    try:
        from quality_mutation import mutation_authorized
        return mutation_authorized(job_id=job_id)
    except Exception:
        return False


def _maybe_repetition_reconcile(result, job_id: str):
    """Post-pass gateado (REPETITION_RECONCILE_ENABLED, default off): cuando
    el audio canta un estribillo M veces y la referencia listó K<M, el grupo
    entero queda corrido una repetición (~±5,6 s medido contra Rotor, job
    6f4047db). Inserta la ocurrencia huérfana / reasigna miembros flotantes
    usando repetition_group + ctc_lr + uncovered_spans — señales que ya
    existían y nadie consumía. Puro (CPU-only, sin I/O): sync a propósito.
    Corre en los 3 call sites (worker + 2 endpoints HTTP) en lockstep — ver
    tests/test_postpass_lockstep.py. Never raises."""
    if not isinstance(result, dict):
        return result
    try:
        import repetition_reconcile as _rr
        if not _rr.is_enabled():
            return result
        if not _quality_mutation_authorized(job_id):
            return result
        segs = result.get("segments") or []
        words = result.get("_asr_words") or []
        if len(segs) < 3 or not words:
            return result
        import lead_in as _li
        nuevo, stats = _rr.reconcile(
            segs, words, lead_s=_li.lead_seconds(), hold_s=_li.hold_seconds(),
        )
        if stats.get("inserted") or stats.get("reassigned"):
            result = dict(result)
            result["segments"] = nuevo
            logger.info(
                "[REP-RECONCILE] %d insertada(s), %d reasignada(s) "
                "(grupos=%d) job=%s", stats["inserted"], stats["reassigned"],
                stats["groups"], job_id)
        elif stats.get("declined"):
            logger.info("[REP-RECONCILE] sin cambios (declines=%s) job=%s",
                        stats["declined"][:4], job_id)
        result.setdefault("postpass_stats", {})["rep_reconcile"] = {
            k: v for k, v in stats.items() if k != "declined"}
        return result
    except Exception as e:  # nunca romper la transcripción
        logger.warning("[REP-RECONCILE] wrapper declinó: %r (job=%s)",
                       e, job_id)
        return result


async def _maybe_gap_rescue(result, audio_path: str, job_id: str,
                            language: str | None = None):
    """Post-pass gateado (GAP_RESCUE_ENABLED, default off): re-transcribe los
    tramos donde el ASR de la cascada quedó SORDO.

    Los otros dos post-pases colocan texto donde el ASR oyó algo; cuando el
    ASR no oyó nada, no tienen con qué trabajar — así quedó un hueco de 30 s
    sin un solo cartel en el job dcf773b5 mientras el audio cantaba. Medido
    sobre ese hueco: transcribir la MEZCLA devuelve 1 palabra; transcribir el
    STEM de voz devuelve 16. Por eso usa el stem cacheado por la cascada
    (nunca corre demucs: `cache_only=True`) y sólo cae a la mezcla si no está.

    Never raises: ante cualquier fallo devuelve el resultado intacto."""
    if not isinstance(result, dict):
        return result
    _stem = None
    try:
        import gap_rescue as _gr
        if not _gr.is_enabled():
            return result
        if not _quality_mutation_authorized(job_id):
            return result
        segs = result.get("segments") or []
        if len(segs) < 3 or not audio_path or not os.path.exists(audio_path):
            return result
        from pipeline import _audio_duration
        dur = await asyncio.to_thread(_audio_duration, audio_path)
        _include_leading = bool(result.get("live_audio_truth"))
        _hay_huecos = bool(_gr.find_gaps(
            segs, dur, include_leading=_include_leading,
        ))
        _hay_manchados = False
        _words_pre = result.get("_asr_words") or []
        if _words_pre:
            try:
                from audio_coverage import text_mismatches as _tm_pre
                _hay_manchados = any(
                    (m["end"] - m["start"]) >= 4.0
                    for m in _tm_pre(segs, _words_pre, min_ratio=0.3))
            except Exception:
                pass
        if not _hay_huecos and not _hay_manchados:
            return result          # nada que rescatar: ni tocamos el stem

        try:
            import vocal_sep as _vs
            _stem = await asyncio.wait_for(
                asyncio.to_thread(_vs.separate_vocals, audio_path,
                                  cache_only=True),
                timeout=120,
            )
        except Exception as e:
            logger.info("[GAP-RESCUE] sin stem cacheado (%r) — uso la mezcla", e)

        import lead_in as _li
        nuevo, stats = await asyncio.to_thread(
            _gr.rescue, segs, audio_path, stem_path=_stem, audio_duration=dur,
            language=language, lead_s=_li.lead_seconds(),
            hold_s=_li.hold_seconds(),
            asr_words=result.get("_asr_words"),
            job_id=job_id,
            include_leading=_include_leading,
            reference_text=result.get("reference_lyrics") or "",
        )
        if stats.get("rescued_lines"):
            result = dict(result)
            result["segments"] = nuevo
            logger.warning(
                "[GAP-RESCUE] %d línea(s) rescatada(s) (%d hueco(s), %d "
                "cartel(es) manchado(s) reemplazado(s)) usando %s job=%s",
                stats["rescued_lines"], stats["gaps"],
                stats.get("mismatch_replaced", 0), stats["source"], job_id)
        elif stats.get("gaps"):
            logger.info("[GAP-RESCUE] %d hueco(s) sin contenido recuperable "
                        "(%s) job=%s", stats["gaps"], stats["skipped"], job_id)
        if stats.get("skipped"):
            # Siempre, aunque haya habido rescates: son los veredictos que
            # el circuit breaker necesita para no contradecir al sondeo.
            logger.info("[GAP-RESCUE] descartados: %s job=%s",
                        stats["skipped"], job_id)
        result.setdefault("postpass_stats", {})["gap_rescue"] = {
            "gaps": stats.get("gaps", 0),
            "rescued_lines": stats.get("rescued_lines", 0),
            "skipped": stats.get("skipped", []),
            "source": stats.get("source")}
        return result
    except Exception as e:
        logger.warning("[GAP-RESCUE] wrapper declinó: %r (job=%s)", e, job_id)
        return result
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass


async def _maybe_word_vote(result, audio_path: str, job_id: str,
                           language: str | None = None, *,
                           live_hint: bool = False):
    """Post-pass gateado (WORD_VOTE_ENABLED, default off): el audio corrige
    a la referencia palabra por palabra, la referencia aporta la ortografía.

    El testigo TIENE que ser independiente: el ASR principal se ceba con la
    letra de referencia como prompt y hereda sus errores (el mix-ASR primed
    oyó "estaciones" donde se canta "canciones"). Por eso el testigo es un
    whisper-1 SIN prompt sobre el stem cacheado — sin stem, se declina
    entero (votar con un testigo sesgado es peor que no votar).

    Validado contra el job real: corrige exactamente las 3 palabras en las
    que Rotor nos ganaba (canciones, embriagué, "Cuando…mi vida") y las
    inserciones quedan con review=True para el operador. Never raises."""
    if not isinstance(result, dict):
        return result
    _stem = None
    try:
        import word_vote as _wv
        _live_verify = bool(
            (
                live_hint
                or os.environ.get("TRANSCRIPTION_QUALITY_CALIBRATED", "0")
                .strip().lower() in ("1", "true", "yes", "on")
            )
            and os.environ.get("LIVE_INDEPENDENT_VERIFY_ENABLED", "0")
            .strip().lower() in ("1", "true", "yes", "on")
        )
        if not (_wv.is_enabled() or _live_verify):
            return result
        segs = result.get("segments") or []
        if ((len(segs) < 3 and not _live_verify)
                or not segs or not audio_path or not os.path.exists(audio_path)):
            return result
        import vocal_sep as _vs
        _stem = await asyncio.wait_for(
            asyncio.to_thread(_vs.separate_vocals, audio_path,
                              cache_only=True),
            timeout=120,
        )
        if not _stem:
            logger.info("[WORD-VOTE] sin stem cacheado — declino (el "
                        "testigo debe ser independiente) job=%s", job_id)
            return result
        from pipeline import _audio_duration
        dur = await asyncio.to_thread(_audio_duration, audio_path)
        try:
            import math as _math
            _duration_valid = (
                _math.isfinite(float(dur)) and float(dur) > 0.0
            )
        except (TypeError, ValueError):
            _duration_valid = False
        if _live_verify and not _duration_valid:
            result = dict(result)
            result.setdefault("postpass_stats", {})["word_vote"] = {
                "substitutions": 0, "insertions": 0, "lines_changed": 0,
                "independent_verifier": True,
                "declined": ["duration_unavailable"],
                "audio_seconds_billed": 0.0,
            }
            logger.warning(
                "[WORD-VOTE] live witness declined: invalid duration=%r job=%s",
                dur, job_id,
            )
            return result
        try:
            _max_verify_s = min(
                600.0,
                max(30.0, float(os.environ.get(
                    "LIVE_INDEPENDENT_VERIFY_MAX_SECONDS", "480",
                ))),
            )
        except (TypeError, ValueError):
            _max_verify_s = 480.0
        try:
            _job_asr_budget = min(
                600.0,
                max(30.0, float(os.environ.get(
                    "LIVE_ASR_MAX_BILLED_SECONDS", "600",
                ))),
            )
        except (TypeError, ValueError):
            _job_asr_budget = 600.0
        _max_verify_s = min(_max_verify_s, _job_asr_budget)
        if _live_verify and float(dur or 0.0) > _max_verify_s:
            result = dict(result)
            result.setdefault("postpass_stats", {})["word_vote"] = {
                "substitutions": 0, "insertions": 0, "lines_changed": 0,
                "independent_verifier": True,
                "declined": ["duration_budget"],
                "audio_seconds_billed": 0.0,
            }
            logger.warning(
                "[WORD-VOTE] live witness declined: duration %.1fs > %.1fs job=%s",
                float(dur or 0.0), _max_verify_s, job_id,
            )
            return result
        from gap_rescue import _transcribe_window
        witness = await asyncio.to_thread(
            _transcribe_window, _stem, 0.0, float(dur or 600.0),
            language, job_id, provenance_step="live_independent_verify",
        )
        _witness_source = "stem"
        _provider_attempts = 1
        _submitted_audio_seconds = float(dur or 0.0)
        _raw_witness_words = len(witness)

        def _sanitize_live_witness(_words):
            # A whole-song blind Whisper pass can emit training-data credits
            # over instrumental breaks. They are not independent evidence of
            # singing and must not manufacture unsafe windows.
            from gap_rescue import _agrupar_en_lineas
            from pipeline import _is_whisper_hallucination
            return [
                word
                for group in _agrupar_en_lineas(_words or [])
                if not _is_whisper_hallucination(
                    " ".join(str(w.get("word") or "") for w in group)
                )
                for word in group
            ]
        if _live_verify:
            witness = _sanitize_live_witness(witness)

        # Vocal isolation is often decisive, but some live stems damage the
        # very consonants we need to adjudicate (Los Pericos: Hoy/Muy and
        # alejaste/alejas). If the blind stem witness has poor physical/text
        # agreement, make one bounded blind pass on the original mix and keep
        # whichever witness is objectively less inconsistent. No prompt or
        # catalogue is sent to either call.
        _mix_fallback_enabled = (
            os.environ.get("LIVE_INDEPENDENT_MIX_FALLBACK_ENABLED", "1")
            .strip().lower() in ("1", "true", "yes", "on")
        )
        if _live_verify and _mix_fallback_enabled:
            try:
                from audio_coverage import (
                    audio_coverage as _witness_coverage,
                    text_mismatches as _witness_mismatches,
                )

                def _witness_rank(_words):
                    return (
                        len(_witness_mismatches(segs, _words)),
                        -float(_witness_coverage(segs, _words)),
                    )

                _stem_rank = _witness_rank(witness)
                _poor_stem = _stem_rank[0] >= 2 or -_stem_rank[1] < 0.70
                _can_afford_mix = (
                    _submitted_audio_seconds + float(dur or 0.0)
                    <= _job_asr_budget
                )
                if _poor_stem and _can_afford_mix:
                    _mix_raw = await asyncio.to_thread(
                        _transcribe_window, audio_path, 0.0,
                        float(dur or 600.0), language, job_id,
                        provenance_step="live_independent_verify_mix",
                    )
                    _provider_attempts += 1
                    _submitted_audio_seconds += float(dur or 0.0)
                    _mix_witness = _sanitize_live_witness(_mix_raw)
                    _mix_rank = _witness_rank(_mix_witness)
                    if _mix_rank < _stem_rank:
                        witness = _mix_witness
                        _raw_witness_words = len(_mix_raw)
                        _witness_source = "mix"
            except Exception as _mix_exc:
                logger.warning(
                    "[WORD-VOTE] mix witness fallback declined: %r job=%s",
                    _mix_exc, job_id,
                )
        # In live verification mode this witness may apply only a pre-existing
        # primary-ASR + catalogue proposal, making the decision three-way.
        # Observe mode remains non-mutating; enforce mode applies verified
        # proposals and the final gate rechecks the exact resulting payload.
        if _live_verify:
            from live_lexical_consensus import apply_verified_proposals
            _verified_candidate, _lexical_stats = apply_verified_proposals(
                segs, witness,
            )
            from transcription_quality import effective_policy_mode
            _apply_verified = _quality_mutation_authorized(job_id)
            nuevo = _verified_candidate if _apply_verified else segs
            stats = {
                "substitutions": (
                    _lexical_stats["applied"] if _apply_verified else 0
                ),
                "insertions": 0, "lines_changed": 0,
                "declined": [], "verification_only": True,
                "live_lexical": _lexical_stats,
                "lines_suggested": (
                    0 if _apply_verified else _lexical_stats["applied"]
                ),
            }
            stats["lines_changed"] = (
                _lexical_stats["applied"] if _apply_verified else 0
            )
        else:
            if not _quality_mutation_authorized(job_id):
                return result
            nuevo, stats = _wv.vote(segs, witness)
        result = dict(result)
        if _live_verify:
            # Internal transport only: the quality finalizer compares
            # delivered text against this independent Whisper-1 witness, then
            # strips it. Studio WORD_VOTE keeps its pre-existing behavior and
            # does not silently opt into the new live quality policy.
            result["_independent_asr_words"] = witness
        stats["independent_verifier"] = _live_verify
        stats["witness_words"] = len(witness)
        stats["witness_words_filtered"] = max(
            0, _raw_witness_words - len(witness),
        )
        stats["witness_source"] = _witness_source
        stats["provider_attempts"] = _provider_attempts
        stats["submitted_audio_seconds"] = round(
            _submitted_audio_seconds, 2,
        )
        stats["audio_seconds_billed"] = round(
            _submitted_audio_seconds, 2,
        )
        if stats.get("lines_changed"):
            result["segments"] = nuevo
            logger.info(
                "[WORD-VOTE] %d sustitución(es) + %d inserción(es) en %d "
                "línea(s) — el stem contradijo a la referencia job=%s",
                stats["substitutions"], stats["insertions"],
                stats["lines_changed"], job_id)
        result.setdefault("postpass_stats", {})["word_vote"] = {
            k: v for k, v in stats.items() if k != "declined"}
        return result
    except Exception as e:  # nunca romper la transcripción
        logger.warning("[WORD-VOTE] wrapper declinó: %r (job=%s)", e, job_id)
        return result
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass


def _maybe_chorus_snap(result, job_id: str):
    """Post-pass gateado (CHORUS_SNAP_ENABLED, default off): en zonas de coro
    repetido, repara los fragmentos mal cortados a la frase canónica del
    grupo. Rotor gana en el outro porque estampa la frase del coro limpia en
    cada repetición en vez de confiar palabra-por-palabra en un ASR que
    patina sobre voz enterrada; esto hace lo mismo con repetition_group.
    Corre DESPUÉS de gap_rescue/word_vote (repara lo que ellos dejaron) y
    ANTES del segmentador. Puro, sync, never raises."""
    if not isinstance(result, dict):
        return result
    try:
        import chorus_snap as _cs
        if not _cs.is_enabled():
            return result
        if not _quality_mutation_authorized(job_id):
            return result
        segs = result.get("segments") or []
        if len(segs) < 3:
            return result
        nuevo, stats = _cs.snap(segs)
        if stats.get("snapped") or stats.get("merged"):
            result = dict(result)
            result["segments"] = nuevo
            logger.info(
                "[CHORUS-SNAP] %d fragmento(s) del coro reparados, %d "
                "órfano(s) absorbidos (grupos=%d) job=%s",
                stats["snapped"], stats["merged"], stats["groups"], job_id)
            result.setdefault("postpass_stats", {})["chorus_snap"] = stats
        return result
    except Exception as e:
        logger.warning("[CHORUS-SNAP] wrapper declinó: %r (job=%s)", e, job_id)
        return result


def _maybe_timing_consistency(result, job_id: str):
    """Post-pass FINAL (TIMING_CONSISTENCY_ENABLED, default ON): la ventana de
    cada cartel tiene que coincidir con las palabras que ese cartel muestra.

    Corre ÚLTIMO a propósito. Las etapas anteriores (scaffold, ctc_align,
    word_vote, phrase_segmenter, gap_rescue, chorus_snap, lead_in) reescriben
    start/end y/o words de forma independiente y ninguna verifica el
    invariante al cierre. Medido sobre 60 días de producción: la mediana está
    sana (0,25 s en ctc_align) pero la cola está rota — p90 de 22,2 s en
    synced_scaffold, peor caso de 79,7 s en ctc_align, y ~10% de las líneas
    (25% en scaffold) terminan ANTES de que se termine de cantar la última
    palabra, o sea el cartel desaparece a mitad de frase.

    Ese es el reporte del operador que originó esto (UMG Chile, 2026-08-13:
    "líneas que quedaron cortas respecto a lo que se escucha") — le costó 48
    arrastres manuales en una sola canción.

    La corrección vive en karaoke_align (donde ya están las primitivas de
    confianza de forced-align) y es pura. Acá solo va el wrapper con la forma
    de result-dict, igual que los demás post-pases. Never raises."""
    if not isinstance(result, dict):
        return result
    try:
        import karaoke_align as _ka
        segs = result.get("segments") or []
        if not segs:
            return result
        nuevo = _ka.enforce_line_word_consistency(segs)
        # This pass is the last boundary after adlibs, word-vote, chorus
        # snap, phrase segmentation and formatting. Those stages can replace
        # rows or reintroduce equal/backward starts after _emit_segments has
        # already normalized the first candidate. Keep the final payload
        # monotonic too; otherwise the editor can still receive a valid-looking
        # response whose playback cursor jumps between rows.
        before_order = timing_anomalies(nuevo)
        ordered = normalize_segments_timing(nuevo)
        after_order = timing_anomalies(ordered)
        if ordered != nuevo:
            result = dict(result)
            result["segments"] = ordered
            result.setdefault("postpass_stats", {})["timing_order_final"] = {
                "before": before_order,
                "after": after_order,
                "repaired": len(ordered),
            }
            logger.warning(
                "[TIMING-FINAL] repaired postpass order regressions=%s "
                "duplicate_starts=%s overlaps=%s → regressions=%s "
                "duplicate_starts=%s overlaps=%s job=%s",
                before_order["regressions"], before_order["duplicate_starts"],
                before_order["overlaps"], after_order["regressions"],
                after_order["duplicate_starts"], after_order["overlaps"],
                job_id,
            )
            nuevo = ordered

        if nuevo is not segs or ordered != segs:
            _ajustadas = sum(
                1 for s in nuevo
                if isinstance(s, dict) and s.get("timing_snapped_to_words")
            )
            result = dict(result)
            result["segments"] = nuevo
            result.setdefault("postpass_stats", {})["timing_consistency"] = {
                "snapped": _ajustadas,
            }
            result["segments"] = nuevo
            logger.info(
                "[TIMING-CONSISTENCY] %d/%d carteles re-encuadrados a sus "
                "palabras job=%s", _ajustadas, len(nuevo), job_id)
        return result
    except Exception as e:
        logger.warning("[TIMING-CONSISTENCY] wrapper declinó: %r (job=%s)", e, job_id)
        return result


async def _finalize_inline_transcription_quality(result, audio_path: str,
                                                 job_id: str, language: str, *,
                                                 live_hint: bool = False):
    """Keep the two legacy HTTP transcription paths aligned with the worker.

    Async RQ is the normal path, but a rollback flag can still execute these
    handlers inline. A safety gate that disappears during rollback is not a
    safety gate, so they use the exact same finalizer and persist its verdict.
    """
    from transcription_worker import _quality_gate_and_retry

    finalized = await _quality_gate_and_retry(
        result, audio_path, job_id, language, None,
        _maybe_timing_consistency, live_hint=live_hint,
    )
    quality_to_enqueue = None
    persisted_revision = 0
    persisted_hash = ""
    persisted_tenant_id = ""
    machine_evidence = finalized.pop("_machine_evidence", None)
    try:
        from database import SessionLocal as _QualitySession
        _quality_db = _QualitySession()
        try:
            row = (
                _quality_db.query(Job).filter(Job.job_id == job_id)
                .with_for_update().first()
            )
            if row is None:
                raise LookupError("job disappeared before machine snapshot persistence")
            if row is not None:
                segments = finalized.get("segments") or []
                revision = int(row.segments_revision or 0)
                if revision > 0 and segments != row.segments_json:
                    logger.warning(
                        "[QUALITY-GATE] inline result discarded after editor race job=%s revision=%s",
                        job_id, revision,
                    )
                else:
                    row.segments_json = segments
                    from editor import get_or_create_document
                    document = get_or_create_document(
                        _quality_db, job_id, row.tenant_id, segments,
                        initial_reason="transcription",
                    )
                    previous_quality = (
                        dict(row.transcription_quality)
                        if isinstance(row.transcription_quality, dict) else {}
                    )
                    quality = finalized.get("transcription_quality")
                    if isinstance(quality, dict):
                        quality = dict(quality)
                        quality["machine_evidence_required"] = True
                        quality["machine_evidence_schema"] = (
                            "machine-transcription-evidence-v1"
                        )
                        from quality_cache import sha256_file
                        quality["audio_sha256"] = sha256_file(audio_path)
                        quality["evaluated_revision"] = revision
                        quality["timing_source"] = row.timing_source or "unknown"
                        try:
                            from quality_learning_model import shadow_prediction_for_quality
                            quality["learning_shadow"] = shadow_prediction_for_quality(
                                quality, quality["timing_source"],
                            )
                        except Exception:
                            quality["learning_shadow"] = {
                                "available": False, "reason": "prediction_failed",
                                "mutated_segments": False,
                            }
                        from transcription_quality import quality_fingerprint
                        quality["quality_fingerprint"] = quality_fingerprint(
                            quality, revision=revision,
                            content_hash=str(quality.get("segments_hash") or ""),
                        )
                        from correction_learning import machine_snapshot_provenance
                        from editor import attach_machine_provenance
                        attach_machine_provenance(
                            _quality_db, job_id,
                            machine_snapshot_provenance(row, quality),
                        )
                        quality_to_enqueue = quality
                        persisted_revision = revision
                        persisted_hash = str(quality.get("segments_hash") or "")
                        persisted_tenant_id = str(row.tenant_id or "")
                    quality = dict(quality or {})
                    quality["machine_evidence_required"] = True
                    quality["machine_evidence_schema"] = (
                        "machine-transcription-evidence-v1"
                    )
                    row.transcription_quality = quality
                    if isinstance(quality, dict):
                        from quality_shadow import record_shadow_decision
                        record_shadow_decision(
                            _quality_db, row, quality,
                            previous_quality=previous_quality,
                            evaluation_stage=(
                                "terminal" if not quality.get("unsafe_windows")
                                else "initial"
                            ),
                        )
                    from machine_evidence import finalize_machine_evidence
                    durable_evidence = finalize_machine_evidence(
                        machine_evidence,
                        original_segments=document.original_segments or [],
                        quality=quality,
                        audio_sha256=quality.get("audio_sha256"),
                        audio_revision=int(row.audio_revision or 0),
                    )
                    from editor import attach_machine_evidence, require_machine_snapshot
                    attach_machine_evidence(_quality_db, document, durable_evidence)
                    row.machine_snapshot_required = True
                    require_machine_snapshot(row, document)
                    row.status = "transcribed_pending"
                    row.current_step = "editing"
                _quality_db.commit()
        finally:
            _quality_db.close()
    except Exception as exc:
        logger.warning("[QUALITY-GATE] inline persistence failed: %s job=%s", exc, job_id)
        try:
            from jobs import update_job
            update_job(
                job_id, status="transcription_failed", current_step="error",
                error="No pudimos guardar la evidencia de la transcripción. Reintentá.",
            )
        finally:
            raise RuntimeError("machine_snapshot_persistence_failed") from exc
    if (
        isinstance(quality_to_enqueue, dict)
        and quality_to_enqueue.get("decision") != "pass"
        and quality_to_enqueue.get("unsafe_windows")
    ):
        try:
            from queue_jobs import enqueue_transcription_quality
            enqueue_transcription_quality(
                job_id, expected_revision=persisted_revision,
                expected_segments_hash=persisted_hash,
                filename=os.path.basename(audio_path),
                tenant_id=persisted_tenant_id,
            )
        except Exception as exc:
            logger.warning(
                "[QUALITY-QUEUE] inline enqueue declined job=%s: %r",
                job_id, exc,
            )
    return finalized


def _maybe_phrase_segment(result, job_id: str):
    """Post-pass gateado (PHRASE_SEGMENTER_ENABLED, default off): re-corta
    los carteles largos en frases de ~6 palabras por DP sobre word-stamps
    (nuestros carteles medían 11-38 palabras vs ~5,8 de Rotor). Corre
    DESPUÉS de repetition_reconcile (necesita líneas enteras con membresía
    final) y ANTES del formatter. Re-anota repetition_group porque los
    grupos cambian al partir. Puro y sync. Never raises."""
    if not isinstance(result, dict):
        return result
    try:
        import phrase_segmenter as _ps
        if not _ps.is_enabled():
            return result
        if not _quality_mutation_authorized(job_id):
            return result
        segs = result.get("segments") or []
        if not segs:
            return result
        if any(isinstance(s, dict) and s.get("llm_segmented") for s in segs):
            result.setdefault("postpass_stats", {})["phrase_seg"] = {
                "before": len(segs), "after": len(segs),
                "skipped": "already_llm_segmented",
            }
            return result
        import lead_in as _li
        nuevo = _ps.resegment(segs, lead_s=_li.lead_seconds(),
                              hold_s=_li.hold_seconds())
        if len(nuevo) != len(segs):
            from chorus_trim import mark_repetitions as _mr
            nuevo = _mr(nuevo)
            _pal = sorted(len((s.get("text") or "").split())
                          for s in nuevo if isinstance(s, dict))
            result = dict(result)
            result["segments"] = nuevo
            logger.info(
                "[PHRASE-SEG] %d → %d carteles (mediana %d pal/cartel) "
                "job=%s", len(segs), len(nuevo),
                _pal[len(_pal) // 2] if _pal else 0, job_id)
            result.setdefault("postpass_stats", {})["phrase_seg"] = {
                "before": len(segs), "after": len(nuevo)}
        return result
    except Exception as e:  # nunca romper la transcripción
        logger.warning("[PHRASE-SEG] wrapper declinó: %r (job=%s)", e, job_id)
        return result


async def _maybe_adlib_filter(result, audio_path: str, job_id: str,
                              live_hint: bool = False,
                              language: str | None = None):
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
    if not _quality_mutation_authorized(job_id):
        result.setdefault("postpass_stats", {})["adlib_consensus"] = {
            "mode": "observe", "mutation_authorized": False,
        }
        return result
    segs = result.get("segments") or []
    if len(segs) < 3:
        return result
    # Audio-first live results have already bypassed catalogue reconciliation.
    # Their suffix cannot contain a studio scaffold to replace, and `wx_raw`
    # is the same acoustic stream (possibly before polishing), not independent
    # evidence. Auditing then swapping that suffix only duplicates collapsed
    # timestamps. Keep ordinary ad-lib/tail filtering available, but disable
    # this catalogue-repair mechanism for authoritative live audio.
    _catalogue_suffix_repair = bool(
        live_hint and not result.get("live_audio_truth")
    )
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
        _audit_on = _catalogue_suffix_repair and os.environ.get(
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
            # Timeout derivado de la MISMA fuente que el presupuesto
            # interno (REPLICATE_BUDGET_S_DEMUCS). Ver vocal_sep.
            _stem = await asyncio.wait_for(
                asyncio.to_thread(_vs.separate_vocals, audio_path),
                timeout=_vs.thread_budget_s())
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
        _tw = _make_stem_window_transcriber(_stem, language=language)
        _before = len(segs)
        filtered = await asyncio.to_thread(
            _ac.filter_and_collapse, segs, _tw, tail_after=_tail_after,
            audit_suffix=_audit_on)
        # MODO VIVO (Perro live, 06/07): el sufijo que la auditoría marcó
        # se reemplaza por los segmentos crudos de whisperX de esa zona —
        # la letra de estudio no puede representar el final de un vivo
        # (call-response, presentaciones de la banda). Las líneas
        # insertadas conservan review=True.
        if (_catalogue_suffix_repair and _wx_raw
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


def _make_stem_window_transcriber(
    stem_path: str,
    language: str | None = None,
):
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
            segs = _wx(clip, language=language) or []
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
            # Timeout derivado de la MISMA fuente que el presupuesto
            # interno (REPLICATE_BUDGET_S_DEMUCS). Antes eran 360s
            # fijos: el loop abandonaba la espera mientras el thread
            # seguía corriendo, y ese huérfano bloqueaba el
            # shutdown_default_executor() del teardown de asyncio.run.
            _stem = await asyncio.wait_for(
                asyncio.to_thread(_vs.separate_vocals, audio_path),
                timeout=_vs.thread_budget_s(),
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
        _short_motif_decline = False
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
            _short_motif_decline = (
                retimed is None
                and _ctc.last_decline_reason == "short_repeated_motif"
            )
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
                                  result.get("reference_lyrics") or "",
                                  job_id),
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
        if (retimed is None and _mix_fallback and not _stem_structural
                and not _short_motif_decline):
            retimed = await asyncio.wait_for(
                asyncio.to_thread(_ctc.retime_segments, audio_path, segs, job_id),
                timeout=420,
            )
            _short_motif_decline = (
                retimed is None
                and _ctc.last_decline_reason == "short_repeated_motif"
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
        elif _short_motif_decline:
            # A compact repeated chant is underdetermined for one global CTC
            # pass.  Preserve the cascade output and route only the affected
            # refrain to Quality.  This metadata is intentionally text-free.
            result = dict(result)
            postpass = dict(result.get("postpass_stats") or {})
            postpass["ctc_retime"] = {
                "declined": True,
                "reason": "short_repeated_motif",
                "unsafe_windows": _ctc.short_repeated_motif_windows(segs),
                "mutated_segments": False,
            }
            result["postpass_stats"] = postpass
            logger.info(
                "[CTC] compact motif routed to bounded quality windows "
                "windows=%d job=%s",
                len(postpass["ctc_retime"]["unsafe_windows"]), job_id,
            )
    except Exception as e:
        logger.warning("[CTC] retime wrapper declined: %r (job=%s)", e, job_id)
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass
    return result


def _anchor_lyrics_enabled() -> bool:
    """Feature flag de la Versión B (letra anclada, default OFF).

    Un solo lector del env para que /transcribe-uploaded (vía
    `_maybe_anchor_align`), POST /jobs/{id}/reanchor y los `features`
    de /auth/* (el frontend gatea la UI con esto) no puedan divergir
    en el parsing del valor."""
    return os.environ.get("ANCHOR_LYRICS_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on")


async def _maybe_anchor_align(result, audio_path: str, job_id: str,
                              anchor_lyrics: str):
    """Align operator-provided lyrics without ever silently discarding them.

    Local CTC remains the preferred timing engine. If it declines (including
    repeated live refrains or a non-Spanish reference), Whisper-1 word stamps
    plus monotonic DP provide an independent fallback. If both decline, the
    result carries ``anchor_alignment.status=declined``; upload callers must
    fail closed instead of publishing free ASR as if the reference never
    existed (incident c6553b32b6c1, 2026-08-31).
    """
    _stem = None
    anchor_text = (anchor_lyrics or "").strip()
    psegs = [
        {"text": line, "start": 0.0, "end": 0.0}
        for line in anchor_text.splitlines() if line.strip()
    ]

    def _declined(base, reason: str, *, error_type: str = ""):
        out = dict(base) if isinstance(base, dict) else base
        if isinstance(out, dict):
            out["reference_lyrics"] = anchor_text
            out["anchor_alignment"] = {
                "status": "declined",
                "reason": str(reason or "unknown")[:80],
                "error_type": str(error_type or "")[:80],
                "content_source": "operator_reference",
                "original_provider_segment_count": len(
                    (base or {}).get("segments") or []
                ),
            }
        return out

    def _apply(base, aligned, *, timing_source: str, decline_reason: str = ""):
        try:
            review_min = float(
                os.environ.get("ANCHOR_REVIEW_MIN_SCORE", "0.25")
            )
        except (TypeError, ValueError):
            review_min = 0.25
        from statistics import median

        flagged = 0
        anchored = []
        for segment in aligned:
            segment = dict(segment)
            segment["content_source"] = "operator_reference"
            segment["provider_evidence"] = {
                "source": "operator_reference",
                "text": str(segment.get("text") or ""),
                "start": round(float(segment.get("start") or 0.0), 3),
                "end": round(float(segment.get("end") or 0.0), 3),
                "words": [],
                "word_count": 0,
                "mean_score": None,
                "min_score": None,
            }
            segment["evidence_lineage"] = [
                "operator_reference_content", timing_source,
            ]
            scores = [
                word.get("score") for word in (segment.get("words") or [])
                if isinstance(word.get("score"), (int, float))
            ]
            needs_review = bool(scores and median(scores) < review_min)
            if timing_source == "whisper_align" and not scores:
                # This line was interpolated between acoustic word anchors.
                needs_review = True
            if needs_review:
                segment["review"] = True
                flagged += 1
            anchored.append(segment)

        out = dict(base)
        out["_pre_anchor_provider_segments"] = [
            dict(segment) for segment in (base.get("segments") or [])
            if isinstance(segment, dict)
        ]
        out["segments"] = anchored
        out["reference_lyrics"] = anchor_text
        # Historical internal marker used by worker + reanchor to skip a
        # second CTC pass. The actual timing engine is recorded below.
        out["timing_source"] = "anchor_ctc"
        out["anchor_alignment"] = {
            "status": "applied",
            "content_source": "operator_reference",
            "timing_source": timing_source,
            "ctc_decline_reason": str(decline_reason or "")[:80],
            "original_provider_segment_count": len(
                out["_pre_anchor_provider_segments"]
            ),
            "review_count": flagged,
        }
        logger.info(
            "[ANCHOR] anchored %d líneas via %s (%d en review, job=%s)",
            len(anchored), timing_source, flagged, job_id,
        )
        return out

    def _safe_alignment(aligned) -> bool:
        """Reject partial, reordered, or occurrence-collapsed fallbacks."""
        if not isinstance(aligned, list) or len(aligned) != len(psegs):
            return False
        expected_texts = [segment["text"] for segment in psegs]
        actual_texts = [str(segment.get("text") or "").strip() for segment in aligned]
        if actual_texts != expected_texts:
            return False
        try:
            starts = [float(segment.get("start")) for segment in aligned]
            ends = [float(segment.get("end")) for segment in aligned]
        except (TypeError, ValueError):
            return False
        if any(end <= start for start, end in zip(starts, ends)):
            return False
        # Equal starts are the classic repeated-chorus pile-up. Small line
        # overlaps are valid, but occurrence order must remain strict.
        return all(right > left for left, right in zip(starts, starts[1:]))

    try:
        if not _anchor_lyrics_enabled():
            # A stale browser may submit after an ops flag flip. Receiving an
            # official reference and silently treating it as absent would
            # recreate the incident even though the UI is now hidden.
            return (
                _declined(result, "feature_disabled")
                if isinstance(result, dict) and anchor_text and psegs
                else result
            )
        if not isinstance(result, dict) or not anchor_text:
            return result
        if not psegs:
            return result

        import ctc_align as _ctc
        import vocal_sep as _vs

        try:
            _stem = await asyncio.wait_for(
                asyncio.to_thread(
                    _vs.separate_vocals, audio_path, cache_only=True,
                ),
                timeout=120,
            )
        except Exception as stem_exc:
            logger.warning(
                "[ANCHOR] cached stem unavailable error_type=%s job=%s",
                type(stem_exc).__name__, job_id,
            )
            _stem = None
        if not _stem and os.environ.get(
            "CTC_ALIGN_COMPUTE_STEM", "1"
        ).strip().lower() in ("1", "true", "yes", "on"):
            logger.info("[ANCHOR] no cached stem — computing it (job=%s)", job_id)
            try:
                _stem = await asyncio.wait_for(
                    asyncio.to_thread(_vs.separate_vocals, audio_path),
                    timeout=_vs.thread_budget_s(),
                )
            except Exception as stem_exc:
                logger.warning(
                    "[ANCHOR] stem compute unavailable error_type=%s job=%s",
                    type(stem_exc).__name__, job_id,
                )
                _stem = None

        align_src = _stem or audio_path
        if not _stem:
            logger.info("[ANCHOR] no stem — aligning on the MIX (job=%s)", job_id)
        if len(psegs) >= 3:
            try:
                retimed = await asyncio.wait_for(
                    asyncio.to_thread(
                        _ctc.retime_segments,
                        align_src,
                        psegs,
                        job_id,
                        audio_path,
                    ),
                    timeout=420,
                )
                decline_reason = str(_ctc.last_decline_reason or "unknown")
            except Exception as ctc_exc:
                logger.warning(
                    "[ANCHOR] CTC failed error_type=%s job=%s; trying "
                    "Whisper-DP fallback",
                    type(ctc_exc).__name__, job_id,
                )
                retimed = None
                decline_reason = f"ctc_{type(ctc_exc).__name__}"
        else:
            # The local CTC contract needs >=3 lines, but a one-line official
            # lyric is still authoritative and Whisper-DP can align it.
            retimed = None
            decline_reason = "too_few_lines_for_ctc"
        if retimed is not None:
            result = _apply(
                result, retimed, timing_source="ctc_timing_only",
            )
        else:
            logger.info(
                "[ANCHOR] CTC declined (reason=%s) job=%s; trying hosted "
                "forced alignment",
                decline_reason, job_id,
            )
            # This aligner has a different occurrence model from local CTC
            # and is already part of the production stack. It is especially
            # useful for known lyrics over mastered live mixes where Demucs
            # removes audience vocals. Accept only a complete, strictly
            # monotonic result so repeated choruses can never pile up.
            try:
                from forced_align import forced_align_lyrics
                retimed = await asyncio.wait_for(
                    asyncio.to_thread(
                        forced_align_lyrics, audio_path, anchor_text,
                    ),
                    timeout=540,
                )
            except Exception as forced_exc:
                logger.warning(
                    "[ANCHOR] hosted forced align failed error_type=%s job=%s",
                    type(forced_exc).__name__, job_id,
                )
                retimed = None
            if _safe_alignment(retimed):
                result = _apply(
                    result,
                    retimed,
                    timing_source="forced_align",
                    decline_reason=decline_reason,
                )
                return result
            if retimed:
                logger.warning(
                    "[ANCHOR] rejected unsafe hosted alignment lines=%d job=%s",
                    len(retimed), job_id,
                )

            logger.info(
                "[ANCHOR] hosted alignment declined job=%s; trying "
                "Whisper-DP fallback",
                job_id,
            )
            try:
                from lyrics_whisper_align import whisper_word_align
                resolved_language = resolve_transcription_language(
                    "", reference_text=anchor_text,
                )
                # Demucs may erase a distant/crowd vocal from a mastered live
                # recording. Try the preferred stem first, then the untouched
                # mix as an independent acoustic view before declining.
                fallback_sources = list(dict.fromkeys((align_src, audio_path)))
                retimed = None
                for fallback_source in fallback_sources:
                    retimed = await asyncio.wait_for(
                        asyncio.to_thread(
                            whisper_word_align,
                            fallback_source,
                            [segment["text"] for segment in psegs],
                            language=resolved_language,
                            job_id=job_id,
                        ),
                        timeout=240,
                    )
                    if _safe_alignment(retimed):
                        break
                    logger.info(
                        "[ANCHOR] Whisper-DP declined source=%s job=%s",
                        "stem" if fallback_source == _stem else "mix",
                        job_id,
                    )
            except Exception as fallback_exc:
                logger.warning(
                    "[ANCHOR] Whisper fallback failed error_type=%s job=%s",
                    type(fallback_exc).__name__, job_id,
                )
                retimed = None
            if not _safe_alignment(retimed):
                logger.error(
                    "[ANCHOR] fail-closed: official lyrics received but both "
                    "aligners declined ctc_reason=%s job=%s",
                    decline_reason, job_id,
                )
                result = _declined(result, decline_reason)
            else:
                result = _apply(
                    result,
                    retimed,
                    timing_source="whisper_align",
                    decline_reason=decline_reason,
                )
    except Exception as exc:
        logger.warning(
            "[ANCHOR] fail-closed wrapper error_type=%s (job=%s)",
            type(exc).__name__, job_id,
        )
        if isinstance(result, dict) and anchor_text and psegs:
            result = _declined(
                result, "wrapper_error", error_type=type(exc).__name__,
            )
    finally:
        if _stem:
            try:
                os.unlink(_stem)
            except OSError:
                pass
    return result


async def _postprocess_live_whisperx(
    segments: list[dict], *, audio_path: str, canonical: str = "",
    artist: str = "", song: str = "", language: str | None = None,
    job_id: str = "",
) -> list[dict]:
    """Apply the opt-in audio-first postpasses shared by every live exit.

    Both pipeline helpers are self-declining, but checking their existing
    flags here avoids imports, thread scheduling, audio reads, and model calls
    when a feature is disabled.  Catalogue lyrics are passed only to gap
    recovery as its existing hallucination guard; they never determine line
    order or timing in this helper.
    """
    _truthy = ("1", "true", "yes", "on")
    _segment_enabled = (
        os.environ.get("LLM_SEGMENT_ENABLED", "").strip().lower() in _truthy
    )
    _gap_enabled = (
        os.environ.get("GAP_RECOVERY_ENABLED", "").strip().lower() in _truthy
    )
    # GAP_RESCUE is the shared worker-owned recovery pass (VAD + independent
    # Whisper witness + resolved language).  When it is enabled, do not also
    # run the older Gemini recovery here: the second owner adds cost and can
    # duplicate or overwrite the first one's lines.  GAP_RECOVERY remains the
    # fallback for deployments that have GAP_RESCUE disabled.
    _worker_gap_owner = (
        os.environ.get("GAP_RESCUE_ENABLED", "").strip().lower() in _truthy
    )
    _gap_enabled = _gap_enabled and not _worker_gap_owner
    _lexical_requested = (
        os.environ.get("LIVE_LEXICAL_CONSENSUS_ENABLED", "0")
        .strip().lower() in _truthy
    )
    _lexical_enabled = bool(
        _lexical_requested
        and os.environ.get("LIVE_INDEPENDENT_VERIFY_ENABLED", "0")
        .strip().lower() in _truthy
    )
    # These legacy helpers rewrite content/structure.  They may only mutate
    # after the signed v5 benchmark gate and within the enforce cohort.
    if not _quality_mutation_authorized(job_id):
        _segment_enabled = False
        _gap_enabled = False
    if _lexical_requested and not _lexical_enabled:
        logger.warning(
            "[WC] live lexical consensus disabled: independent verifier "
            "must be enabled too",
        )
    if not (_segment_enabled or _gap_enabled or _lexical_enabled):
        return segments

    from pipeline import _llm_segment_words, _recover_gap_lyrics

    processed = segments
    if _segment_enabled:
        try:
            candidate = await asyncio.to_thread(
                _llm_segment_words, processed, audio_path=audio_path,
                artist=artist, song=song, language=language,
            )
            if isinstance(candidate, list) and candidate:
                processed = candidate
        except Exception as exc:
            logger.warning(
                "[WC] live LLM segmentation declined unexpectedly: %r", exc,
            )
    if _gap_enabled:
        try:
            candidate = await asyncio.to_thread(
                _recover_gap_lyrics, processed,
                audio_path=audio_path, canonical=canonical,
                prompt_reference=False,
                artist=artist, song=song, language=language,
            )
            if isinstance(candidate, list) and candidate:
                processed = candidate
        except Exception as exc:
            logger.warning(
                "[WC] live gap recovery declined unexpectedly: %r", exc,
            )
    # The audio still owns every row and timestamp.  Catalogue text can only
    # repair a small number of 1:1 spelling tokens; it cannot add, delete,
    # split or reorder anything in a live performance.
    if _lexical_enabled and canonical:
        try:
            from live_lexical_consensus import propose_segments
            candidate, _stats = propose_segments(processed, canonical)
            if candidate:
                processed = candidate
        except Exception as exc:
            logger.warning(
                "[WC] live lexical consensus declined unexpectedly: %r", exc,
            )
    return processed


def _can_infer_primary_language_from_reference(
    requested_language: str | None, *, live: bool = False,
    title: str = "", filename: str = "",
) -> bool:
    """Whether catalogue text may choose the primary ASR language.

    Studio uploads keep the existing reliability hint.  A live performance
    can legitimately use another language (or mix languages), so Auto must be
    decided by the audio provider instead of a catalogue entry for a different
    recording.  Explicit per-song operator choices are handled separately and
    continue to win.
    """
    return bool(
        not (requested_language or "").strip()
        and not (live or _looks_live(title, filename))
    )


async def _run_transcription_for_job(
    request, current_user, job_id: str, audio_path: str,
    *, language: str = "", artist: str = "", title: str = "",
    filename: str = "", live: bool = False,
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

    # Bind inline/legacy requests too (the RQ entrypoint already does this).
    # Context variables are copied into asyncio.to_thread, so every Replicate
    # consumer below can attribute its prediction without threading job_id
    # through a dozen fallback signatures.
    from observability import set_job_log_context
    set_job_log_context(job_id)

    if not filename:
        filename = os.path.basename(audio_path)

    tmp_dir = tempfile.mkdtemp()
    tmp_path = audio_path
    _vocal_stem = None   # demucs output path (lazy), cleaned up in finally

    # Filled after the audio-first ASR exists.  `_emit_segments` reads this
    # state at the single output chokepoint so every downstream branch gets
    # the same reference-health observability without duplicating payload
    # plumbing across the cascade.
    _reference_attestation_state = {"report": None}

    try:
        # Language is a per-job property. Tenant, role and geography never
        # participate in this decision; a workspace may contain any mix of
        # Spanish, English and other supported languages.
        lang = normalize_language(language)
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
                            content_reference_used: bool | None = None,
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
            # Freeze the selected provider/reference rows at the cascade
            # chokepoint *before* dedup, beat snap, lead-in or later CTC
            # passes can alter text/timing. Unselected variants remain in
            # their provider-specific cache/N-best artifacts.
            from line_evidence import annotate_provider_evidence
            reference_used = (
                bool(str(reference_lyrics or "").strip())
                if content_reference_used is None
                else bool(content_reference_used)
            )
            frozen_segments = annotate_provider_evidence(
                segments,
                source=source,
                content_source=(
                    "catalog_reference" if reference_used
                    else source
                ),
                timing_source=source,
                reference_text=reference_lyrics if reference_used else None,
                reference_id=(
                    f"{artist}:{title}" if reference_used else None
                ),
            )
            deduped = _dedup_collisions(frozen_segments)
            if deduped and segments and len(deduped) != len(segments):
                logger.info("[EMIT] deduped collisions: %d → %d segments (job=%s)",
                            len(segments), len(deduped), job_id)
            polished = _snap(_normalize_words(deduped))
            anomalies = timing_anomalies(polished)
            if anomalies["regressions"] or anomalies["duplicate_starts"]:
                logger.warning(
                    "[TIMING-CONSISTENCY] source=%s regressions=%s "
                    "duplicate_starts=%s overlaps=%s job=%s",
                    source, anomalies["regressions"],
                    anomalies["duplicate_starts"], anomalies["overlaps"], job_id,
                )
            normalized_polished = normalize_segments_timing(polished)
            if normalized_polished != polished:
                logger.warning(
                    "[TIMING-NORMALIZE] repaired non-monotonic starts in "
                    "%s segments job=%s", len(polished), job_id,
                )
            polished = normalized_polished
            out = {"job_id": job_id, "segments": polished,
                   "reference_lyrics": reference_lyrics}

            # Language is evaluated at the same single output chokepoint as
            # timing. A bilingual song is valid: preserve multi-label
            # evidence instead of forcing one global language.
            reference_languages = detect_text_languages(reference_lyrics)
            try:
                language_evidence = _wx_segs if _wx_segs else polished
            except NameError:
                language_evidence = polished
            detected_languages = detect_text_languages(language_evidence)
            requested_language = normalize_language(language)
            reference_language = (
                next(iter(reference_languages))
                if len(reference_languages) == 1 else None
            )
            detected_language = (
                next(iter(detected_languages))
                if len(detected_languages) == 1 else None
            )
            mixed_language = (
                len(reference_languages) > 1 or len(detected_languages) > 1
            )
            expected_language = lang or requested_language or reference_language
            language_conflict = bool(
                expected_language
                and detected_languages
                and expected_language not in detected_languages
                and not mixed_language
            )
            language_uncertain = bool(
                not requested_language
                and not reference_languages
                and len(detected_languages) <= 1
            )
            if language_conflict:
                logger.error(
                    "[LANGUAGE] conflict job=%s expected=%s detected=%s; "
                    "blocking approval",
                    job_id, expected_language, detected_language,
                )
            out.update({
                "requested_language": requested_language,
                "detected_language": detected_language,
                "detected_languages": sorted(detected_languages),
                "reference_language": reference_language,
                "reference_languages": sorted(reference_languages),
                "mixed_language": mixed_language,
                "language_conflict": language_conflict,
                "language_uncertain": language_uncertain,
            })

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
            if isinstance(_reference_attestation_state.get("report"), dict):
                out["reference_attestation"] = dict(
                    _reference_attestation_state["report"]
                )

            # Cobertura contra el AUDIO en el punto de salida de la cascada.
            # Toda métrica previa se mide contra la letra de REFERENCIA y por
            # eso SUBE cuando la referencia viene recortada (un job reportaba
            # 100 % teniendo el 39 % del canto sin letra). Ésta sólo puede
            # bajar si se pierde canto. Se mide en el único exit point, así
            # queda registrada para TODAS las ramas — no dentro de una sola,
            # que fue el error de diagnóstico original.
            #
            # Sólo observabilidad: no gatea ni altera el resultado. Las
            # palabras crudas viajan en `_asr_words` para que el worker pueda
            # re-medir tras los post-pases (CTC / adlib / formatter) y
            # atribuir la pérdida a la etapa que la produjo.
            try:
                _asr_words = [w for s in (_wx_segs or [])
                              for w in (s.get("words") or [])
                              if isinstance(w, dict)]
            except NameError:
                _asr_words = []
            if _asr_words:
                try:
                    from audio_coverage import summarize as _cov_summary
                    _c = _cov_summary(polished, _asr_words)
                    out["audio_coverage"] = _c["audio_coverage"]
                    out["_asr_words"] = _asr_words
                    _log = (logger.warning if _c["audio_coverage"] < 0.8
                            else logger.info)
                    _log("[COVERAGE] cascada source=%s cobertura_audio=%.0f%% "
                         "zonas_sin_letra=%d (%.1fs, peor %.1fs) job=%s",
                         source, _c["audio_coverage"] * 100,
                         _c["uncovered_spans"], _c["uncovered_seconds"],
                         _c["worst_span_s"], job_id)
                except Exception as e:  # nunca romper la transcripción
                    logger.warning("[COVERAGE] no se pudo medir: %r", e)
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
            _initial_asr_lyrics_hint, _plain_lyrics_aligner_enabled,
            _anchored_recovery_is_safe,
            _fetch_lrclib_by_audio_evidence,
            _strip_leading_reference_credits,
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

        # Catalogue text may begin with a detached editorial credit rather
        # than a sung lyric. Remove only explicit, blank-line-delimited credit
        # blocks before *any* alignment path sees them. A false leading line
        # shifts ordinal word bucketing for the entire song (La Foto de Tu
        # Cuerpo: 31 clean reconciled lines became 49 mixed fragments).
        if lrc and (lrc.get("plain") or "").strip():
            _clean_plain, _removed_credits = _strip_leading_reference_credits(
                lrc.get("plain") or "",
            )
            if _removed_credits:
                lrc["plain"] = _clean_plain
                logger.warning(
                    "[LYRICS] removed %d detached leading credit line(s): %r",
                    len(_removed_credits),
                    _removed_credits,
                )

        # The upload wizard defaults to Auto.  Resolve that choice from the
        # canonical lyrics before the primary ASR runs, so English references
        # are transcribed as English while Spanish references retain the
        # explicit hint that historically made Whisper more reliable.
        if lrc:
            _reference_for_language = (
                (lrc.get("plain") or "").strip()
                or (lrc.get("synced") or "").strip()
            )
            _reference_languages = detect_text_languages(_reference_for_language)
            # Whisper's language parameter is global. A Spanish verse plus
            # English chorus must stay provider-auto; forcing either language
            # would damage the other half of the song.
            if len(_reference_languages) > 1:
                if lang:
                    logger.info(
                        "[LANGUAGE] mixed reference; ignoring single-language "
                        "ASR hint %s for job=%s",
                        lang, job_id,
                    )
                lang = None
            _detected_lang = (
                resolve_transcription_language(
                    None, reference_text=_reference_for_language,
                )
                if len(_reference_languages) == 1
                and _can_infer_primary_language_from_reference(
                    lang, live=live, title=title, filename=filename,
                ) else None
            )
            if _detected_lang:
                lang = _detected_lang
                logger.info(
                    "[LANGUAGE] auto-resolved %s from reference before primary ASR job=%s",
                    lang,
                    job_id,
                )
            elif len(_reference_languages) > 1:
                logger.info(
                    "[LANGUAGE] mixed reference %s; keeping provider-auto job=%s",
                    sorted(_reference_languages),
                    job_id,
                )

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
                # Divergent version detection (2026-06-04, LIVE_NO_HINT_ENABLED,
                # default off). When the upload and the lrclib record describe
                # DIFFERENT versions, the lrclib STUDIO text poisons whisperX's
                # initial_prompt — the model parrots the prompt in studio order and
                # scrambles the performance (lab: Coti "Nada" live → offset +75s,
                # first verse at 1:29 instead of 0:39) — AND the downstream
                # reconcile/scaffold drift against the wrong structure.
                # Lab (7 songs, 3 Rotor ground-truths): clean NO-hint whisperX matches
                # Rotor's own timing (median 0.03-0.8 s); Rotor itself transcribes blind
                # the same way. So for divergent audio: drop the hint + emit the clean
                # transcription raw, skipping the canonical cascade. Reversible; falsy
                # when we can't measure (missing lrclib duration) so default behavior
                # is untouched.
                #
                # SYMMETRIC since 2026-08-05. This used to test the SIGNED difference
                # (audio - lrclib > 60), so it only caught the "extended live" side and
                # was blind to the opposite, equally broken case: a reference LONGER
                # than the upload (radio edit / snippet / short live cut). Los Pericos
                # "Runaway (En Vivo)" — 110s upload vs a 205s lrclib studio record,
                # diff -95s — sailed past this check into the canonical cascade, and
                # forced_align clamped every studio line past the 110s mark onto the
                # final timestamp: ~17 lines piled at 1:50 in the editor, including an
                # outro this cut never sings. See timing_confidence.divergent_duration.
                _lrc_dur = (lrc or {}).get("duration") if isinstance(lrc, dict) else None
                from timing_confidence import divergent_duration as _divergent_dur
                _live_no_hint = bool(
                    os.environ.get("LIVE_NO_HINT_ENABLED", "0").strip().lower()
                    in ("1", "true", "yes", "on")
                    and _divergent_dur(_audio_dur_for_lrc, _lrc_dur)
                )
                if _live_no_hint:
                    _dur_diff = float(_audio_dur_for_lrc) - float(_lrc_dur)
                    logger.info(
                        "[WC] divergent version (audio %.0fs vs lrclib %.0fs, "
                        "diff %+.0fs — reference is %s) — clean whisperX, no hint, "
                        "raw emit",
                        float(_audio_dur_for_lrc), float(_lrc_dur), _dur_diff,
                        "shorter (extended/live)" if _dur_diff > 0
                        else "longer (edit/snippet)",
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
                # A live recording is a different performance even when its
                # duration happens to match the catalogue/studio reference.
                # The old policy only dropped the prompt for duration-divergent
                # versions, so same-length lives were still vulnerable to the
                # reference being copied into the ASR order. Keep the catalogue
                # for later text correction, but let Whisper hear the upload
                # without a prompt first. This is the safe audio-first policy
                # for live-labelled uploads and is independently kill-switchable.
                _live_audio_truth = bool(
                    (live or _looks_live(title, filename))
                    and os.environ.get("LIVE_AUDIO_AS_TRUTH_ENABLED", "1")
                    .strip().lower() in ("1", "true", "yes", "on")
                )
                _drop_hint = _live_no_hint or _live_audio_truth or _no_hint_always
                if _no_hint_always and not _live_no_hint:
                    logger.info("[WC] WHISPERX_NO_HINT_ALWAYS — clean whisperX, reconcile restores canonical text")
                elif _live_audio_truth and not _live_no_hint:
                    logger.info(
                        "[WC] live audio-as-truth — clean whisperX, "
                        "catalogue text remains available for reconciliation",
                    )
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

                # Catalogue text is a candidate, never truth by declaration.
                # Compare it with clean audio-first WhisperX before it can own
                # vocabulary or whole-song structure.  Default mode is OFF;
                # `observe` exports metrics only, while `enforce` fails closed
                # to raw WhisperX when the candidate lacks ASR support.
                _reference_gate_mode = os.environ.get(
                    "REFERENCE_ATTESTATION_MODE", "off"
                ).strip().lower()
                if _wx_segs and _canonical and _reference_gate_mode in {
                    "observe", "enforce",
                }:
                    from reference_attestation import (
                        assess_reference_attestation,
                        reference_gate_action,
                    )
                    from timing_sources import WHISPERX as _WC_WX
                    _reference_is_live = bool(
                        live or _looks_live(title, filename)
                    )
                    _reference_report = assess_reference_attestation(
                        _canonical,
                        _wx_segs,
                        reference_source="catalog_unverified",
                        audio_duration_s=_audio_dur_for_lrc,
                        is_live=_reference_is_live,
                    )
                    _reference_attestation_state["report"] = _reference_report
                    logger.info(
                        "[REFERENCE-ATTEST] mode=%s status=%s score=%.3f "
                        "vocabulary=%s global=%s job=%s",
                        _reference_gate_mode,
                        _reference_report["text_status"],
                        _reference_report["metrics"]["attestation_score"],
                        _reference_report["allow_vocabulary_reconciliation"],
                        _reference_report["allow_global_forced_alignment"],
                        job_id,
                    )
                    _reference_action = reference_gate_action(
                        _reference_report,
                        mode=_reference_gate_mode,
                        is_live=_reference_is_live,
                    )
                    if _reference_action == "audio_first":
                        logger.warning(
                            "[REFERENCE-ATTEST] catalogue candidate lacks "
                            "safe text/structure attestation; "
                            "emitting audio-first WhisperX without reference "
                            "reconciliation job=%s",
                            job_id,
                        )
                        return _emit_segments(
                            _wx_segs,
                            _WC_WX,
                            reference_lyrics="",
                            extra={
                                "reference_candidate_rejected": True,
                                "reference_gate_action": _reference_action,
                                "reference_source": lyrics_source,
                            },
                        )

                if _wx_segs:
                    from timing_sources import (
                        WHISPERX_RECONCILED as _WC_WX_REC,
                        WHISPERX as _WC_WX,
                    )
                    # Every live policy exits through the same audio-first
                    # postprocess.  LLM segmentation re-groups the live's OWN
                    # timed words; gap recovery can fill bounded acoustic holes.
                    # Both are independently flagged and self-declining.  The
                    # catalogue cascade below remains unreachable, so studio
                    # structure can never replace the performance's order/timing.
                    if _live_no_hint or _live_audio_truth:
                        if _live_audio_truth:
                            _live_policy = (
                                "divergent live" if _live_no_hint
                                else "live audio-as-truth"
                            )
                        else:
                            _live_policy = "divergent live"
                        logger.info(
                            "[WC] %s — emitting clean whisperX after "
                            "audio-first postprocess, "
                            "no catalogue reconciliation (%d segs)",
                            _live_policy, len(_wx_segs),
                        )
                        # In live auto-mode the performance decides the
                        # language.  Catalogue text may describe a studio cut
                        # or even another-language version.  Explicit choices
                        # still win because they arrive in the original
                        # `language` argument.
                        _live_language = resolve_transcription_language(
                            language if (language or "").strip() else None,
                            result={"segments": _wx_segs},
                        )
                        _wx_segs = await _postprocess_live_whisperx(
                            _wx_segs, audio_path=_aa, canonical=_canonical,
                            artist=artist, song=title,
                            language=_live_language,
                            job_id=job_id,
                        )
                        return _emit_segments(
                            _wx_segs, _WC_WX, reference_lyrics=_canonical,
                            content_reference_used=False,
                            extra={
                                "live_audio_truth": True,
                                "resolved_language": _live_language,
                            },
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
                            # The reconciler has already chosen the catalogue's
                            # human line structure. Its per-word array is an
                            # ordinal timing aid, not guaranteed 1:1 lexical
                            # ownership, so splitting those lines at apparent
                            # word gaps can create single-word and even reversed
                            # fragments. Keep line boundaries; still run end
                            # tightening and overlap clamping.
                            _reconciled = _prc(
                                _reconciled,
                                split_long_lines=False,
                            )
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
                            if (
                                _is_divergent
                                and _quality_mutation_authorized(job_id)
                            ) else _wx_segs
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
                                    language=(
                                        resolve_transcription_language(
                                            lang,
                                            reference_text=_canonical,
                                        )
                                        or lang
                                    ),
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
                                    # Audit 2026-08-13 (F4): durations agreeing
                                    # is metadata-vs-metadata, not proof the
                                    # synced timeline actually lines up with
                                    # what's sung — synced_offset_decision used
                                    # to trust it blindly and suppress the
                                    # amber review flag on that basis alone.
                                    # Spend one Whisper call (~3s) to confirm
                                    # real audio at the claimed zero-offset
                                    # position matches the first synced line,
                                    # but only when durations actually agree
                                    # (3.0s mirrors synced_offset_decision's
                                    # own dur_tol default — the Whisper call is
                                    # only worth paying for in that regime).
                                    _durations_agree = (
                                        _audio_dur_for_lrc is not None
                                        and _lrc_dur_val is not None
                                        and abs(_audio_dur_for_lrc - _lrc_dur_val) <= 3.0
                                    )
                                    _verify_score = (
                                        await asyncio.to_thread(
                                            _verify_lrclib_alignment, tmp_path,
                                            _pairs[0][1], _pairs[0][0],
                                        )
                                        if _durations_agree else None
                                    )
                                    _offset, _trust = _lca.synced_offset_decision(
                                        _audio_dur_for_lrc, _lrc_dur_val,
                                        _first_wx_t, _pairs[0][0],
                                        verify_fn=(
                                            (lambda score=_verify_score: score)
                                            if _durations_agree else None
                                        ),
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
                logger.info(
                    "[LYRICS] lrclib plain hit (%s chars) — ASR-first "
                    "(reference reserved for post-alignment), skipping Gemini",
                    len(plain),
                )

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
                    _candidate_offset = float(user_dur - lrc_dur)
                    # Audit 2026-08-13 (F3): the trim decision above is pure
                    # metadata arithmetic (durations subtracted) — nothing
                    # confirms the user's audio actually HAS an intro at
                    # that point, vs. e.g. a longer outro/extended mix
                    # throwing off the same subtraction. A wrong trim here
                    # silently cuts real sung lyrics out of what Whisper
                    # ever sees. Spend one cheap Whisper call (~3s) to
                    # confirm the audio at the claimed boundary actually
                    # matches lrclib's opening line before committing to
                    # the slice; skip the trim (transcribe the full audio)
                    # rather than risk shipping lyrics missing their start.
                    _expected_opening = (plain.strip().splitlines() or [""])[0][:200]
                    _alignment_score = (
                        await asyncio.to_thread(
                            _verify_lrclib_alignment, tmp_path,
                            _expected_opening, _candidate_offset,
                        )
                        if _expected_opening else None
                    )
                    if _alignment_score is None or _alignment_score < 0.4:
                        logger.warning(
                            "[LYRICS] intro-trim rejected: audio at %.1fs doesn't "
                            "match lrclib's opening line (score=%s) — skipping "
                            "trim, transcribing full audio instead (user=%.1fs, "
                            "lrclib=%.1fs)",
                            _candidate_offset, _alignment_score, user_dur, lrc_dur,
                        )
                    else:
                        intro_offset = _candidate_offset
                        candidate = os.path.join(tmp_dir, "body_only.mp3")
                        sliced = await asyncio.to_thread(
                            _slice_audio_window, tmp_path, candidate,
                            intro_offset, user_dur - intro_offset,
                        )
                        if sliced:
                            transcribe_path = candidate
                            trimmed_path = candidate
                            logger.info("[LYRICS] trimmed %.1fs intro before Whisper (user=%.1fs, lrclib=%.1fs, alignment_score=%.2f)", intro_offset, user_dur, lrc_dur, _alignment_score)
                        else:
                            intro_offset = 0.0  # slice failed — fall through

                # Hybrid intro Whisper. The intro region we sliced off may
                # contain a spoken dialogue / narration that previews the
                # song's lyrics (verified case: "El Plan de la Mariposa —
                # El Riesgo" Video Oficial has 73 s of voice-over reciting
                # the first verse before the song starts). Run Whisper on
                # the intro chunk with the same ASR-first policy. A recited
                # line must not prime Whisper into repeating the known song
                # text over the rest of the dialogue. The segments returned
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
                aligner_enabled = _plain_lyrics_aligner_enabled()
                initial_hint = _initial_asr_lyrics_hint(plain)
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
                            None,
                            lambda: transcribe(
                                intro_path, lang,
                                lyrics_hint=initial_hint,
                                return_words=True,
                            ),
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
                            transcribe_path, lang,
                            lyrics_hint=initial_hint,
                            return_words=True,
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
                #   - the env kill-switch is off
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
                    recovery_safe, recovery_reason = _anchored_recovery_is_safe(
                        plain, anchors, recovered,
                    )
                    if recovered and recovery_safe:
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
                    logger.error(
                        "[LYRICS] synthetic recovery rejected (%s; ASR=%s; "
                        "anchors=%s) — preserving audio-timed output for review",
                        recovery_reason, reason, len(anchors),
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

        # Post-ASR alignment is default-on with a kill-switch. Word timestamps
        # are requested regardless so raw lines can end on their last word.
        aligner_enabled = _plain_lyrics_aligner_enabled()

        # ── Fetch Gemini in parallel, keep first-pass ASR audio-first ─────────
        # The reference remains valuable after recognition for spelling and
        # human line grouping. Feeding it into Whisper first caused reference
        # parroting in the measured corpus and on the ROTOR regression track.
        # `_initial_asr_lyrics_hint` retains an explicit rollback mode.
        #
        # In audio-first mode Whisper starts immediately while Gemini continues
        # in parallel. Only explicit short/full rollback modes wait for Gemini
        # before ASR, because those modes intentionally need a prompt.
        _gemini_pre = ""
        _prompt_mode = os.environ.get(
            "WHISPER_REFERENCE_PROMPT_MODE", "off",
        ).strip().lower()
        if _prompt_mode not in ("", "off", "0", "false", "no"):
            try:
                _gemini_pre = (
                    await asyncio.wait_for(
                        asyncio.shield(gemini_task), timeout=10.0,
                    )
                    or ""
                )
                if _gemini_pre:
                    logger.info(
                        "[LYRICS] Gemini returned %d chars before Whisper — "
                        "reference-prompt rollback mode=%s",
                        len(_gemini_pre), _prompt_mode,
                    )
            except asyncio.TimeoutError:
                logger.info(
                    "[LYRICS] Gemini prompt not ready in 10s — Whisper runs "
                    "audio-first",
                )
            except Exception as _e_gem:
                logger.info(
                    "[LYRICS] Gemini pre-fetch error (%s) — Whisper runs "
                    "audio-first", _e_gem,
                )
        else:
            logger.info(
                "[LYRICS] audio-first mode — Whisper and reference lookup "
                "running concurrently",
            )

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
                lyrics_hint=_initial_asr_lyrics_hint(_gemini_pre),
                return_words=True,
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

        # Metadata-recovery path: filename artist credits are frequently
        # truncated ("Rodrig"), while the audio-first ASR text is already a
        # strong fingerprint. Search LRCLIB by title only and accept a record
        # exclusively when title + duration + heard words agree. This is
        # deliberately after ASR; doing it before listening recreated the old
        # "right duration, wrong song" incident.
        if not reference and song_hint:
            try:
                _evidence_dur = await asyncio.to_thread(
                    _audio_duration, tmp_path,
                )
                _evidence_lrc = await asyncio.to_thread(
                    _fetch_lrclib_by_audio_evidence,
                    artist_hint, song_hint, segments, _evidence_dur,
                )
                if _evidence_lrc and _evidence_lrc.get("plain"):
                    reference = (_evidence_lrc.get("plain") or "").strip()
                    logger.info(
                        "[LYRICS] recovered reference from audio evidence "
                        "(%s chars, artist=%r, title=%r, score=%s)",
                        len(reference),
                        _evidence_lrc.get("_matched_artist"),
                        _evidence_lrc.get("_matched_title"),
                        _evidence_lrc.get("_audio_evidence_score"),
                    )
            except Exception as e:
                logger.warning(
                    "[LYRICS] audio-evidence reference recovery failed: %s", e,
                )

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
    base_revision: str = Form("", max_length=20),
    editor_metrics_json: str = Form("", max_length=2000),
    editor_version_id: str = Form("", max_length=36),
    editor_revision: str = Form("", max_length=20),
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
    # Batch-only canonical visual contract. Empty keeps the legacy individual
    # form fields unchanged; non-empty is validated and takes precedence.
    render_profile: str = Form("", max_length=4000),
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
    # Art track ("official audio"): master audio + cover image → video with the
    # cover centered over a blurred fill + subtle motion, NO lyrics. The cover
    # comes in via background_file (image). Skips transcription + AI background.
    art_track: bool = Form(False),
    # Línea legal opcional en pantalla para art tracks, ej.
    # "℗ 2026 Universal Music Chile". Vacía = no se dibuja.
    label_line: str = Form("", max_length=120),
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
    try:
        _render_profile = normalize_render_profile(render_profile)
    except RenderProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _render_profile:
        # A batch profile is the signed-off visual contract.  Keep the legacy
        # form fields for old clients, but let the canonical object win when
        # it is present so retries cannot drift from the manifest.
        _profile_fields = pipeline_fields(_render_profile)
        style = _profile_fields["style"]
        font = _profile_fields["font"]
        genre = _profile_fields["genre"]
        concept = _profile_fields["concept"]
        movement_style = _profile_fields["movement_style"]
        effect = _profile_fields["effect"]
        text_case = _profile_fields["text_case"]
        font_scale = str(_profile_fields["font_scale"])
        line_transition = _profile_fields["line_transition"]
        if _render_profile.get("background_id") is not None:
            background_id = _render_profile["background_id"]
    reuse = bool(job_id)
    try:
        segments = json.loads(segments_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="segments_json must be valid JSON") from exc
    if not isinstance(segments, list):
        raise HTTPException(status_code=400, detail="segments_json must be an array")
    segments = normalize_editor_segments(segments)
    requested_language = normalize_language(language)
    observed_languages = detect_text_languages(segments)
    if (
        requested_language
        and len(observed_languages) == 1
        and requested_language not in observed_languages
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Transcription language conflict: requested "
                f"{requested_language}, detected {next(iter(observed_languages))}. "
                "Choose the song language and transcribe again."
            ),
        )
    language = requested_language
    try:
        requested_revision = int(base_revision) if str(base_revision).strip() else None
        if requested_revision is not None and requested_revision < 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="base_revision must be a non-negative integer") from exc

    editor_document = None
    selected_editor_version = None
    approved_version = None

    if reuse:
        # Reuse path: verify the job belongs to caller and pull the audio
        # path / R2 key from the row. Two valid entry states:
        #   - transcribed_pending: editor flow (segments came from
        #     /transcribe-uploaded; segments_json carries the user-edited
        #     timings).
        #   - awaiting_upload: direct-generate flow (no editor;
        #     segments_json is "[]" so the worker runs Whisper itself
        #     against the audio that already landed in R2).
        job_row = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            .first()
        )
        _tenant_match = bool(job_row and job_row.tenant_id == current_user["tenant_id"])
        # Cross-tenant para admins de plataforma: mismo contrato que
        # _job_scope / GET /editor / source-audio-url (pedido CEO
        # 2026-06-11) — el rol admin necesita poder regenerar el video de
        # cualquier cliente para resolver incidentes de soporte. Antes de
        # este fix /generate era el único endpoint del flujo de edición
        # sin este bypass: un admin podía abrir /editor y ver el audio de
        # un job ajeno (200 OK) pero al generar chocaba con un 404
        # "job_not_found" porque este chequeo comparaba tenant_id a secas
        # (bug real, staging 2026-08-19: found=True tenant_match=False).
        _is_admin_cross_tenant = bool(job_row and not _tenant_match
                                       and current_user.get("role") == "admin")
        if not job_row or (not _tenant_match and not _is_admin_cross_tenant):
            # Do not expose whether a foreign job exists, but leave enough
            # forensic signal to distinguish a reaped temporary job from a
            # tenant/session mismatch in production logs.
            logger.warning(
                "[GENERATE] job_not_found job_id=%s actor_user_id=%s found=%s tenant_match=%s",
                job_id,
                current_user.get("id"),
                bool(job_row),
                _tenant_match,
            )
            # Stable machine-readable code so the frontend doesn't couple to the
            # HTTP status. `job_not_found` = reaped / cross-tenant (non-admin) /
            # never existed → the client surfaces a "session expired, re-upload"
            # CTA instead of freezing the single-song hero (audit 2026-07-27).
            return JSONResponse(
                status_code=404,
                content={"code": "job_not_found", "detail": "Job not found."},
            )
        if _is_admin_cross_tenant:
            _audit_cross_tenant_access(db, current_user, job_row, "generate")
        # State whitelist for /generate. `transcribed_pending` is what the
        # transcription worker writes on success (post-2026-05-25 fix);
        # `transcribed` is accepted defensively for jobs that were written
        # by the older worker variant that drifted from the convention,
        # and `awaiting_upload` covers the direct-generate path (no editor).
        # See transcription_worker.py:137 for the writer side.
        if job_row.status not in ("transcribed_pending", "transcribed", "awaiting_upload"):
            # Stable code (mirrors the `job_not_found` path above) so the client
            # can react without string/status matching.
            return JSONResponse(
                status_code=409,
                content={
                    "code": "job_not_generatable",
                    "detail": f"Job is in state {job_row.status!r}, cannot generate.",
                },
            )
        # An explicit, owned job_id is authoritative after a duplicate-upload
        # response race.  Restore its visibility now; the successful generate
        # transaction below persists this together with the queued state.
        from jobs import touch_user_activity
        touch_user_activity(db, job_row)
        if editor_version_id or str(editor_revision).strip():
            if not current_user.get("features", {}).get("editor_v2"):
                # This is a deployment/configuration mismatch, not a missing
                # job. Returning 404 made the client tell operators that their
                # session had expired and invited them to discard corrections.
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "editor_not_enabled",
                        "detail": "The durable editor is not enabled for this environment.",
                    },
                )
            parsed_editor_revision = None
            if str(editor_revision).strip():
                try:
                    parsed_editor_revision = int(editor_revision)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=422, detail="editor_revision must be an integer") from None
            try:
                editor_document, selected_editor_version = approve_document(
                    db, job_row, current_user["id"],
                    editor_revision=parsed_editor_revision,
                    editor_version_id=editor_version_id or None,
                )
            except LookupError:
                raise HTTPException(status_code=409, detail="editor_version_not_found") from None
            except MachineSnapshotMissing as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "machine_snapshot_missing", "detail": str(exc)},
                ) from None
            except RuntimeError:
                # job_row.tenant_id, not current_user["tenant_id"]: an admin
                # regenerating another tenant's job must resolve the
                # document under the JOB's tenant, not their own (or this
                # 404s via get_or_create_document's tenant-scoped lookup).
                current_document = get_or_create_document(
                    db, job_id, job_row.tenant_id, job_row.segments_json or [],
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": "editor_revision_conflict",
                        "server_revision": current_document.revision,
                        "server_segments": current_document.current_segments,
                    },
                ) from None
            segments = selected_editor_version.segments
            requested_revision = selected_editor_version.revision
        current_revision = int(getattr(job_row, "segments_revision", 0) or 0)
        if requested_revision is None and current_revision > 0:
            return JSONResponse(
                status_code=428,
                content={"code": "client_upgrade_required", "current_revision": current_revision},
            )
        if requested_revision is not None and requested_revision != current_revision:
            from ops_metrics import increment
            increment("segments_revision_conflict")
            return JSONResponse(
                status_code=409,
                content={
                    "code": "stale_revision",
                    "current_revision": current_revision,
                    "updated_at": (
                        job_row.last_user_activity_at.isoformat()
                        if job_row.last_user_activity_at else None
                    ),
                },
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

    # Art track ("official audio"): master audio + cover image → cover
    # composited with subtle motion, no lyrics. Validate the cover and coerce
    # incompatible options BEFORE quota/AI gates so it costs 1 credit and is
    # not treated as an AI-background job.
    if art_track:
        # Feature gate (default OFF salvo admin / tenant en allowlist). Corta
        # acá aunque el front no muestre la opción — un tenant sin acceso que
        # pegue a la API con art_track=true no debe poder generar.
        if not has_art_track_access(current_user):
            raise HTTPException(
                status_code=403,
                detail="Art Track no está habilitado para tu cuenta.",
            )
        if background_id:
            raise HTTPException(
                status_code=400,
                detail="Art tracks use an uploaded cover, not a library background.",
            )
        if not (background_file and background_file.filename):
            raise HTTPException(
                status_code=400, detail="Art track requires a cover image.",
            )
        if not background_file.filename.lower().endswith((".jpg", ".jpeg", ".png")):
            raise HTTPException(
                status_code=400,
                detail="Art track cover must be an image (.jpg/.jpeg/.png).",
            )
        # No lyrics, no Escenas, no Veo image-to-video animation for art tracks.
        enable_scenes = False
        animate_image = ""

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
    # source, which IS AI generation, so the auth gate must apply. Art tracks
    # invoke NO AI (deterministic ffmpeg composite), so they never need it.
    _needs_ai_auth = (not art_track) and ((not background_id) or (background_mode == "variation"))
    if _needs_ai_auth and current_user.get("role") != "admin":
        user_model = db.query(User).filter(User.id == current_user["id"]).first()
        if user_model and not user_model.ai_authorized:
            raise HTTPException(status_code=403, detail="AI tool usage not authorized. Contact admin for approval.")

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
        job_row = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            # SessionLocal has autoflush disabled. Refresh after waiting for
            # the row lock so a concurrent generate/save cannot leave this
            # request making decisions from the earlier ownership lookup.
            .populate_existing()
            .with_for_update()
            .first()
        )
        # Same admin cross-tenant bypass as the ownership check above — this
        # is the post-lock re-verification, not a second, independent
        # authorization rule. An admin who passed the first check must not
        # get bounced here just because the row still belongs to another
        # tenant (it never gets reassigned — see job_row.tenant_id below).
        _tenant_ok = bool(
            job_row is not None
            and (job_row.tenant_id == current_user["tenant_id"]
                 or current_user.get("role") == "admin")
        )
        if (job_row is None
                or not _tenant_ok
                or job_row.status not in (
                    "transcribed_pending", "transcribed", "awaiting_upload",
                )):
            return JSONResponse(
                status_code=409,
                content={"code": "job_not_generatable", "detail": "Job changed before generation."},
            )
        current_revision = int(getattr(job_row, "segments_revision", 0) or 0)
        if requested_revision is None and current_revision > 0:
            return JSONResponse(
                status_code=428,
                content={"code": "client_upgrade_required", "current_revision": current_revision},
            )
        if requested_revision is not None and requested_revision != current_revision:
            from ops_metrics import increment
            increment("segments_revision_conflict")
            return JSONResponse(
                status_code=409,
                content={"code": "stale_revision", "current_revision": current_revision},
            )
        # Normally the preceding autosave already persisted the exact payload.
        # A CAS-matching direct client may still combine save+generate; in that
        # case the server performs the segment write and owns the increment.
        if requested_revision is None and current_revision == 0:
            # Legacy clients did not send a revision, but their submitted
            # segments are still the approval snapshot and must be persisted
            # before enqueueing the worker.
            job_row.segments_json = segments
        if requested_revision is not None and job_row.segments_json != segments:
            job_row.segments_json = segments
            job_row.segments_revision = current_revision + 1
            current_revision += 1
        from transcription_quality import can_render as _quality_can_render
        _quality_ok, _quality_reason = _quality_can_render(
            getattr(job_row, "transcription_quality", None),
            revision=current_revision,
            segments=job_row.segments_json or segments,
            job_id=job_id,
            tenant_id=str(job_row.tenant_id or ""),
        )
        if not _quality_ok:
            # Quality analysis is advisory. A stale/pending model verdict must
            # never prevent an operator from rendering the lyrics they just
            # edited. Preserve the signal for observability, but do not turn
            # it into a 409 or a second approval flow.
            logger.warning(
                "[TRANSCRIPTION-QUALITY] allowing render with advisory result",
                extra={
                    "job_id": job_id,
                    "tenant_id": str(job_row.tenant_id or ""),
                    "revision": current_revision,
                    "reason": _quality_reason,
                },
            )
            try:
                from ops_metrics import increment
                increment("transcription_quality_render_advisory")
            except Exception:
                pass
        if editor_metrics_json:
            try:
                _editor_metrics = json.loads(editor_metrics_json)
                if not isinstance(_editor_metrics, dict):
                    raise ValueError("metrics must be an object")
                _allowed_metrics = {
                    "duration_ms", "active_edit_ms", "line_count", "text_changes",
                    "timing_changes", "lines_added", "lines_removed",
                    "lines_reordered", "quality_acknowledged", "session_id",
                }
                if any(
                    key not in _allowed_metrics
                    or not _valid_product_event_property(key, value)
                    for key, value in _editor_metrics.items()
                ):
                    raise ValueError("invalid metric property")
                _editor_metrics["revision"] = current_revision
                _event_quality = job_row.transcription_quality or {}
                _editor_metrics["pipeline_release"] = str(
                    _event_quality.get("pipeline_release") or "unknown"
                )[:64]
                _editor_metrics["pipeline_config_fingerprint"] = str(
                    _event_quality.get("pipeline_config_fingerprint") or "unknown"
                )[:32]
                _editor_metrics["timing_source"] = str(
                    _event_quality.get("timing_source") or "unknown"
                )[:64]
                _editor_metrics["quality_policy_version"] = str(
                    _event_quality.get("policy_version") or "unknown"
                )[:64]
                _editor_metrics["quality_reason_codes"] = ",".join(
                    str(reason.get("code"))
                    for reason in (_event_quality.get("reasons") or [])
                    if isinstance(reason, dict) and reason.get("code")
                )[:500]
                from database import ProductEvent
                db.add(ProductEvent(
                    tenant_id=current_user["tenant_id"],
                    user_id=current_user["id"], job_id=job_id,
                    name="editor_approved", properties=_editor_metrics,
                ))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(
                    status_code=422, detail=f"invalid editor metrics: {exc}"
                ) from None
        job_row.artist = artist
        job_row.song_title = song_title or None
        job_row.style = style
        job_row.delivery_profile = delivery_profile
        job_row.umg_spec = umg_spec
        # The runnable transition is committed atomically with the outbox after
        # all request-side validation/background work has succeeded.
        job_row.status = "transcribed_pending"
        job_row.current_step = "editing"
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
        # approve_document/get_or_create_document may refresh this ORM row
        # from the database while SessionLocal has autoflush disabled.  Apply
        # the revival again at the final locked transition so it is guaranteed
        # to persist with queued status.
        from jobs import touch_user_activity
        touch_user_activity(db, job_row)

        # The durable editor bridge locks and refreshes this same Job row.
        # Persist the pending transition inside the current transaction first;
        # otherwise populate_existing() would restore the pre-generate state
        # when SessionLocal(autoflush=False) is in use.
        db.flush()

        # The generate/approve action freezes the exact persisted editor
        # snapshot used by the worker as an immutable audit version. Legacy
        # clients are bridged lazily so they receive the same guarantee.
        try:
            if editor_document is None:
                # job_row.tenant_id, not current_user["tenant_id"] — see the
                # RuntimeError branch above for why (admin cross-tenant).
                editor_document = get_or_create_document(
                    db, job_id, job_row.tenant_id, job_row.segments_json or [],
                )
            require_machine_snapshot(job_row, editor_document)
            if editor_document.revision < current_revision:
                sync_legacy_snapshot(
                    db, editor_document, current_user["id"],
                    job_row.segments_json or [], current_revision,
                )
            approved_version = db.query(EditorVersion).filter(
                EditorVersion.job_id == job_id,
                EditorVersion.tenant_id == job_row.tenant_id,
                EditorVersion.revision == editor_document.revision,
            ).first()
            if approved_version and approved_version.segments == (job_row.segments_json or []):
                approved_version.is_approved = True
                approved_version.reason = "approve"
        except MachineSnapshotMissing as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "machine_snapshot_missing", "detail": str(exc)},
            ) from None
        except ValueError:
            # Keep legacy generate compatibility for malformed-but-JSON
            # payloads; the durable editor layer must never turn that existing
            # path into a 500 while the pipeline emits its normal validation.
            logger.warning("[editor] skipped approval snapshot for invalid segments job=%s", job_id)
        db.commit()
        if approved_version is not None:
            try:
                from queue_jobs import enqueue_correction_learning
                enqueue_correction_learning(
                    job_id, approved_version.id,
                    active_edit_ms=(
                        _editor_metrics.get("active_edit_ms")
                        if "_editor_metrics" in locals() else None
                    ),
                    session_id=(
                        _editor_metrics.get("session_id")
                        if "_editor_metrics" in locals() else None
                    ),
                )
            except Exception as exc:
                # Learning is deliberately non-blocking: a queue outage must
                # never turn a valid user approval into a failed generation.
                logger.warning(
                    "[QUALITY-LEARNING] approval capture enqueue failed job=%s: %s",
                    job_id, exc,
                )
    else:
        job_id = create_job(
            db,
            artist=artist, style=style, filename=existing_filename,
            user_id=current_user["id"], tenant_id=tenant_id,
            delivery_profile=delivery_profile, umg_spec=umg_spec,
            initial_status="transcribed_pending",
            song_title=song_title,
        )

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Persist the art_track marker into render_params now (belt-and-suspenders
    # with the worker-side merge) so /retry re-renders as an art track even if
    # the worker dies before Step 1.
    if art_track:
        try:
            from jobs import merge_render_params
            _params = {"art_track": True}
            if (label_line or "").strip():
                _params["label_line"] = label_line.strip()
            merge_render_params(job_id, _params)
        except Exception as _e:
            logger.warning("[ART] could not persist art_track render_param: %s", _e)

    if _render_profile:
        try:
            from jobs import merge_render_params
            merge_render_params(job_id, {"render_profile": _render_profile})
        except Exception as _e:
            logger.warning("[BATCH] could not persist render_profile: %s", _e)

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

    # Hide the orphan draft the wizard sometimes leaves behind: if a
    # sibling transcribed_pending/awaiting_upload row for the same audio was
    # just created (re-upload-on-generate bug), soft-archive it so the operator
    # doesn't see a phantom "2nd job". Time-windowed so it never touches an
    # intentional re-upload of the same song later.  The helper never deletes
    # the row: an out-of-order browser response may still reference it.
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

    # P1 2026-07-17: si el frontend no mandó bg_cache_key (race del debounce
    # de 10s de useBackgroundPreview — el operador aprobó dentro de la
    # ventana), lo recomputamos server-side con la MISMA función compartida
    # que valida el worker (bg_preview.job_bg_cache_key). Solo aplica al
    # fondo único AI: custom/library (bg_path) y escenas tienen su propio
    # flujo, y /variant JAMÁS hereda este fast path (quiere un fondo
    # distinto con params iguales — por eso el recompute vive acá y no en
    # run_pipeline, que /variant y /retry comparten). El worker re-valida
    # el key de todos modos: un mismatch = generación fresh, nunca un
    # fondo equivocado.
    _bg_cache_key_norm = (bg_cache_key or "").strip() or None
    _effective_scenes = bool(enable_scenes) and has_scenes_access(current_user)
    # Audit adversarial 2026-07-17: excluir variation EXPLÍCITAMENTE. Una
    # variation de librería devuelve bg_path=None (con variation_source_path
    # seteado), así que sin este guard el recompute corría y le asignaba un
    # key a un job de variation — justo lo que /variant NO debe heredar (un
    # _animate_user_image downstream lo salvaba, pero el guard debe hacer lo
    # que el comentario promete, no depender de otra rama). Y el join de la
    # letra va DENTRO del try: segments malformados (json.loads de un cliente
    # roto) no deben tirar 500 y bloquear el enqueue — se cae a fresh.
    _is_variation = bool(variation_source_path or variation_source_r2_key)
    if (
        _bg_cache_key_norm is None and bg_path is None
        and not _effective_scenes and not _is_variation
    ):
        try:
            from bg_preview import job_bg_cache_key
            _bg_cache_key_norm = job_bg_cache_key(
                artist=artist, song_title=song_title, style=style,
                movement_style=movement_style, effect=effect,
                custom_colors=(custom_colors.strip() or ""), genre=genre,
                concept=concept,
                background_hint=(background_hint.strip() or None),
                bg_verbatim=bg_verbatim, match_lyrics=match_lyrics,
            )
        except Exception as _recompute_err:
            logger.warning(
                "[BG] recompute server-side falló job=%s: %s — fondo fresh",
                job_id, _recompute_err,
            )
            _bg_cache_key_norm = None
        if _bg_cache_key_norm:
            logger.info(
                "[BG] bg_cache_key recomputado server-side job=%s key=%s",
                job_id, _bg_cache_key_norm,
            )

    publication_job = (
        db.query(Job).filter(Job.job_id == job_id).with_for_update().one()
    )
    publication_job.status = initial_status
    publication_job.current_step = "queued"
    publication_job.progress = 0
    publication_job.last_progress_at = datetime.now(timezone.utc)
    _commit_pipeline_publication(
        db, publication_job, "generate",
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
        # ~60-180s + $0.80-3.20 de cuota Veo. P1 2026-07-17: si el frontend
        # no lo mandó, viene recomputado server-side (bloque de arriba).
        bg_cache_key=_bg_cache_key_norm,
        # Escenas (multi-escena): opt-in del operador AND elegibilidad real.
        # Si el flag llega pero el usuario no tiene acceso, se ignora (fondo
        # único) — el gate de feature vive en el backend, no en el form.
        enable_scenes=_effective_scenes,
        title_template=title_template if title_template in ("auto", "centered", "lower_third", "badge") else "auto",
        title_size=_clamp_title_size(title_size),
        title_artist_font=(title_artist_font.strip() or ""),
        title_song_font=(title_song_font.strip() or ""),
        # UI v1.1: pass-through. Empty string preserves auto-wrap.
        title_song_break=(title_song_break or ""),
        # Art track: master audio + cover → cover composited (blur + centered +
        # subtle motion), no lyrics. Validated above (cover required, image
        # only). The pipeline skips transcription + AI background.
        art_track=art_track,
        label_line=(label_line or "").strip() if art_track else "",
        render_profile=_render_profile,
    )

    return {
        "job_id": job_id,
        "status": initial_status,
        "approved_editor_version_id": (
            selected_editor_version.id if selected_editor_version is not None else None
        ),
    }


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
    ddb: Session = Depends(get_deliveries_db),
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
        # Expuesto para que la UI distinga un error crudo de un
        # "background_attention:*" (fondo degradado) y muestre la tarjeta
        # accionable en vez de un error rojo. Ver BG_ATTENTION_CATEGORY_PREFIX.
        "error_category": job.get("error_category"),
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
        # Retroactive ProRes keeps delivery_profile="youtube" as historical
        # provenance. The persisted spec is therefore required to restore
        # the ProRes/send-to-UMG UI correctly after a reload.
        "umg_spec": job.get("umg_spec"),
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
        "segments_revision": int(job.get("segments_revision") or 0),
        # Final-render preflight is consumed by JobDetail from this polling
        # endpoint.  Returning it here is essential: /jobs is only the list
        # bootstrap, while a refresh and every render/edit completion hydrate
        # the editor from /status/{job_id}.
        "delivery_qc": job.get("delivery_qc"),
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
        # El flag vive en la DB de deliveries (`ddb`, posible externa de prod).
        # Gateado por approved_at: el botón "Enviar a UMG" solo aparece en jobs
        # aprobados, así que para el caso común (job no aprobado, polleado sin
        # parar por JobDetail) devolvemos False sin pegarle a la DB externa —
        # evita latencia/egress/checkout de conexión de prod en cada poll.
        "is_in_umg_portal": bool(
            job.get("approved_at")
            and ddb.query(Delivery.id)
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
    with scoped_db() as db_tick:
        try:
            fresh_user = get_current_user_from_token_param(token, db_tick)
        except HTTPException as exc:
            reason = "session_unavailable" if exc.status_code == 503 else "token_invalid"
            return None, True, reason
        if (fresh_user.get("id") != initial_user_id
                or fresh_user.get("tenant_id") != initial_tenant_id):
            return None, True, "tenant_changed"
        job = get_job(db_tick, job_id, **scope)
    return job, False, ""


@app.get("/events/{job_id}")
async def job_events(
    job_id: str,
    request: Request,
    token: str | None = Query(None, description="Temporary legacy query auth"),
):
    """Server-Sent Events stream for a single job. Emits one event whenever
    the job's status, step, or progress changes and a keepalive comment on
    unchanged ticks, then closes on any terminal state. New clients
    authenticate with an Authorization Bearer header.
    Query authentication is a temporary, server-controlled compatibility path.

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
    auth_header = request.headers.get("authorization", "")
    bearer_token = ""
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip()
    if bearer_token:
        access_token = bearer_token
    elif token and os.environ.get(
        "SSE_LEGACY_QUERY_AUTH_ENABLED", "0"
    ).strip().lower() in ("1", "true", "yes", "on"):
        access_token = token
        logger.warning("[SECURITY] legacy SSE query authentication used")
        from ops_metrics import increment
        await asyncio.to_thread(increment, "sse_query_access_token")
    else:
        raise HTTPException(status_code=401, detail="Bearer authentication required.")

    with scoped_db() as db:
        try:
            current_user = get_current_user_from_token_param(access_token, db)
        except HTTPException as exc:
            if exc.status_code == 503:
                raise
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
    from job_states import TERMINAL_STATUSES
    TERMINAL = TERMINAL_STATUSES
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
                _sse_tick, access_token, job_id, scope, _initial_user_id, _initial_tenant_id,
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
                    "error_category": job.get("error_category"),
                    "created_at": job.get("created_at"),
                    "completed_at": job.get("completed_at"),
                    "eta_s": _eta_s,
                    "step_text_es": _step_text,
                }
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                # Keep the stream observable even while a long render remains
                # on the same step/progress value. The frontend watchdog is
                # 6–8 s; ticks are every 2 s, so a healthy idle stream never
                # looks frozen. SSE comments are ignored by event handlers.
                yield ": keepalive\n\n"
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


@app.get("/batch/jobs/{job_id}")
def batch_job_status(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Scoped detail endpoint for the resumable batch runner.

    It intentionally lives under ``/batch`` so it cannot shadow the legacy
    ``/jobs/{job_id}/...`` media routes.  No delivery or portal mutation is
    performed; the response is read-only and includes render_params/files so
    the runner can build its scoreboard.
    """
    row = get_job(db, job_id, **_job_scope(current_user))
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return row


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
    ok, reason = delete_job(db, job_id, tenant_id, deleted_by_user_id=current_user.get("id"))
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
    return bulk_delete_jobs(db, ids, tenant_id, deleted_by_user_id=current_user.get("id"))


FILE_MAP = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
    # canvas, canvas_v2, canvas_v3 — la fuente de verdad es pipeline.
    **{ft: f"{ft}.mp4" for ft in CANVAS_FILE_TYPES},
}

def _require_canvas_access(file_type: str, user) -> None:
    """403 si alguien que no es admin pide el Canvas.

    Defensa en profundidad, tercera capa: el front ya esconde el botón
    (`features.canvas`) y el worker ni siquiera produce el archivo para un job
    que no es de un admin (`_job_owner_is_admin`). Esto ataja el caso que las
    otras dos no cubren — un token viejo, una URL compartida, o un job que SÍ
    es de admin cuyo archivo alguien intenta bajar con otra cuenta.
    """
    if file_type in CANVAS_FILE_TYPES and not has_canvas_access(user):
        raise HTTPException(status_code=403, detail="Canvas no disponible.")


MEDIA_TYPES = {
    "video": "video/mp4",
    "short": "video/mp4",
    "thumbnail": "image/jpeg",
    "umg_master": "video/quicktime",
    "umg_short": "video/quicktime",
    **{ft: "video/mp4" for ft in CANVAS_FILE_TYPES},
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

    Devuelve 404 cuando el entregable NO EXISTE. Suena obvio y no lo era:
    antes esto sólo validaba que el file_type estuviera en FILE_MAP, así
    que siempre entregaba un token, la URL resultante era truthy y TODOS
    los guards `url && ...` del frontend eran decorativos — el tab "Short"
    montaba un <video> que 404eaba, "Descargar Short" era clickeable, y
    BatchProgress usa `<a download>.click()`, que no puede observar el
    status HTTP y contaba 0 fallos. Con jobs que pueden terminar sin short
    (ver pipeline._accessory_failed) eso pasó de rareza a caso real.
    """
    if file_type not in FILE_MAP and file_type != "all":
        raise HTTPException(status_code=400, detail="Invalid file type.")
    _require_canvas_access(file_type, current_user)
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    # El media-token es la puerta a VER el media: si un admin cruza de
    # tenant, queda en el audit trail (contrato de la apertura cross-tenant).
    # Va ANTES del 404 de abajo a propósito: lo que el audit registra es la
    # INTENCIÓN de mirar el job de otro tenant, y eso no cambia porque el
    # archivo puntual no exista.
    _audit_cross_tenant_access(db, current_user, job, kind=f"media-token:{file_type}")
    # Sólo short y thumbnail. `video` queda fuera aposta: un job sin master
    # no se puede ni mirar, y 404ear el master de una fila vieja con la
    # columna en NULL sería una regresión peor que el problema que esto
    # resuelve. Los ProRes (umg_*) son derivados LAZY —no existen hasta que
    # alguien los pide— y "all" ya filtra por los archivos presentes.
    # Ojo: get_job devuelve un DICT con los entregables anidados en "files",
    # no el modelo ORM (ver su contrato en jobs.py).
    if file_type in ("short", "thumbnail") + CANVAS_FILE_TYPES:
        if not (job.get("files") or {}).get(f"{file_type}_url"):
            raise HTTPException(
                status_code=404,
                detail=f"This job has no {file_type}.",
            )
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
        _require_canvas_access(file_type, current_user)
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
    # ProRes keys need a lifecycle-aware HEAD check in
    # check_prores_readiness before redirecting. Other immutable artifacts
    # retain the direct fast path.
    if (s3_key and storage.is_enabled()
            and file_type not in ("umg_master", "umg_short")):
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

    # Lazy ProRes path: never run ffmpeg or blocking R2/lock readiness I/O on
    # the event loop. check_prores_readiness may HEAD R2 and short-wait up to
    # 15 s for an in-flight transcode, so the whole check runs in a worker
    # thread before this handler decides between redirect, prewarm and 202.
    if file_type in ("umg_master", "umg_short"):
        readiness = await asyncio.to_thread(
            check_prores_readiness, job_id, file_type, job, tenant_id,
        )
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
                from queue_jobs import enqueue_prores_prewarm, SubmissionsPausedError
                enqueue_prores_prewarm(job_id, file_type, force=True)
            except SubmissionsPausedError as exc:
                from ops_control import get_submissions_state
                state = get_submissions_state()
                return JSONResponse(
                    status_code=503,
                    content={
                        "code": "submissions_paused",
                        "detail": str(exc),
                    },
                    headers={"Retry-After": str(state.get("retry_after", 60))},
                )
            except Exception as e:  # pragma: no cover
                logger.warning("[PRORES] enqueue prewarm from /download failed: %s", e)
                raise HTTPException(status_code=503, detail="ProRes queue unavailable")
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
        _require_canvas_access(file_type, current_user)
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
        url = storage.generate_signed_url(
            s3_key,
            expiry_seconds=MEDIA_TOKEN_EXPIRE_SECONDS,
        )
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


class DeliveryQCIssueDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(acknowledged|rejected|resolved_manual)$")
    reason: str = Field(default="", max_length=300)


class DeliveryQCExternalResultRequest(BaseModel):
    finding_count: int = Field(ge=0, le=10000)
    report_id: str = Field(default="", max_length=160)
    source: str = Field(default="umg", pattern="^[a-zA-Z0-9_-]{1,32}$")


def _merge_content_validation_choice(
    render_params: dict | None,
    *,
    bypass: bool = False,
    force: bool = False,
) -> dict:
    """Persist one unambiguous content-policy choice, defaulting safe.

    The three write paths (edit, retry and variant) must not preserve a stale
    opposite flag from a prior operation.  Force wins if a malformed/legacy
    client sends both values; when neither is present we fail closed.
    """
    merged = dict(render_params or {})
    merged.pop("bypass_content_validation", None)
    merged.pop("force_content_validation", None)
    if force:
        merged["force_content_validation"] = True
    elif bypass:
        merged["bypass_content_validation"] = True
    else:
        merged["force_content_validation"] = True
    return merged


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
    # Optional one-click Delivery QC repairs. IDs are server-issued and bound
    # to the fresh report; arbitrary browser patches are never accepted.
    delivery_qc_action_ids: list[str] = Field(default_factory=list, max_length=64)
    base_revision: int | None = Field(default=None, ge=0)
    editor_revision: int | None = Field(default=None, ge=0)
    editor_version_id: str | None = Field(default=None, max_length=36)
    force_conflict_overwrite: bool = False
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
    # Explicit non-Universal opt-in used together with a prompt that asks for
    # people. Universal accounts remain validation-mandatory in pipeline.py;
    # this request field can never relax that server-side rule.
    bypass_content_validation: bool = Field(default=False)
    # Explicit safe choice. It also wins if a legacy/malformed client sends
    # both flags. Sending neither defaults to this same fail-closed behavior.
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
    # Scene axes editable in the wizard for a background regen. None = keep
    # persisted (only sent when the operator CHANGED them — same None-means-
    # keep contract as movement_style/background_mode). The pipeline already
    # reads all three from merged render_params (pipeline.py ~15459); mirrors
    # the /generate limits. genre/concept steer the AI scene vocabulary;
    # match_lyrics = "Inspirado en la letra" (True) vs "Auto/Mi prompt" (False).
    genre: str | None = Field(default=None, max_length=64)
    concept: str | None = Field(default=None, max_length=2000)
    match_lyrics: bool | None = Field(default=None)
    # "Usar mi prompt tal cual": when True (and background_hint is set),
    # the hint goes straight to Veo without Gemini's rewrite. Only
    # meaningful for edit_type=="background". None = keep persisted: the
    # unified wizard only sends this field when the toggle CHANGED, so a
    # `bool` default of False silently clobbered a persisted True on any
    # background edit that didn't touch it (BUG-5). None-aware now.
    bg_verbatim: bool | None = Field(default=None)
    # Library asset for edit_type=="background_library": swap the video's
    # background for a curated BackgroundAsset instead of regenerating with
    # AI. The escape hatch from the non-converging Veo loop (incidente Gaby
    # 2026-07-08, job eaff5c7baf50: 3 regens sin control y sin salida a
    # biblioteca). Required for that edit_type; ignored otherwise.
    background_id: int | None = Field(default=None)
    # Fondo subido por el operador para edit_type=="custom": restaura en el
    # wizard de EDICIÓN la opción "Subir el mío" que #970 ocultó (el backend
    # /edit no tenía este edit_type → no-op silencioso). El body de /edit es
    # JSON y no puede transportar bytes, así que el browser sube el archivo a
    # R2 vía POST /edit/{job}/custom-background (multipart) y manda acá la key
    # devuelta. El worker la baja y la usa tal cual (human-provided) o —si
    # animate_image y es imagen fija— la anima con Veo image-to-video (mismo
    # seam que create-time). Requerido para ese edit_type; ignorado si no.
    custom_background_r2_key: str | None = Field(default=None, max_length=512)
    # "Animar con AI" sobre la imagen subida (solo imágenes fijas). Espeja el
    # flag animate_image de /generate. Provenance: la imagen animada por Veo
    # se clasifica como AI-derived (aplica validación Universal), igual que en
    # create-time (_background_source_is_ai animate_image=True → True).
    animate_image: bool = Field(default=False)
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
    file_type: str = Field(..., max_length=20)  # "umg_master" | "umg_short" | "video" | "short" | "canvas"


@app.post("/approve/{job_id}")
async def approve_job(
    job_id: str,
    body: ApproveJobRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a job after human review, changing status from pending_review to done."""
    from database import Job as JobModel, AuditLog, EditorVersion, ProductEvent
    from datetime import datetime, timezone

    job_query = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        job_query = job_query.filter(
            JobModel.tenant_id == current_user["tenant_id"],
        )
    job = job_query.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Quota is charged to the job owner's account, not to a platform admin who
    # may be approving cross-tenant. Take the account lock before the job row
    # lock so all quota-consuming paths share one deterministic lock order.
    billing_user = db.query(User).filter(User.id == job.user_id).first()
    if billing_user is None:
        raise HTTPException(status_code=409, detail="Job owner no longer exists")
    billing_identity = billing_user.to_dict()
    _lock_quota_scope(db, billing_identity)

    job = job_query.with_for_update().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "pending_review":
        raise HTTPException(status_code=400, detail="Job is not pending review")

    from delivery_qc_runtime import approval_gate, effective_delivery_qc_mode
    _delivery_gate = approval_gate(job.delivery_qc, effective_delivery_qc_mode())
    if _delivery_gate.get("blocked"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_qc_blocked",
                "message": "El preflight de entrega tiene hallazgos pendientes.",
                "delivery_qc": _delivery_gate,
            },
        )

    scene_plan = job.scene_plan if isinstance(job.scene_plan, dict) else {}
    approval_credits = (
        scenes_credit_cost() if scene_plan.get("scenes") is not None else 1
    )
    _enforce_plan_quota(
        db,
        billing_identity,
        credits_needed=approval_credits,
        lock_scope=False,
        send_alert=False,
    )

    is_cross_tenant_admin = (
        current_user.get("role") == "admin"
        and job.tenant_id != current_user.get("tenant_id")
    )
    _audit_cross_tenant_access(
        db, current_user, job, "approve_job", commit=False,
    )

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
                "archived_failed_attempts": _archived_n,
                "tenant_id": job.tenant_id,
                "owner_user_id": job.user_id,
                "actor_tenant_id": current_user.get("tenant_id"),
                "cross_tenant_admin": is_cross_tenant_admin},
    ))
    learning_version = db.query(EditorVersion).filter(
        EditorVersion.job_id == job_id,
        EditorVersion.revision == int(job.segments_revision or 0),
        EditorVersion.is_approved.is_(True),
    ).first()
    learning_event = db.query(ProductEvent).filter(
        ProductEvent.job_id == job_id,
        ProductEvent.name == "editor_approved",
    ).order_by(ProductEvent.created_at.desc()).first()
    _qc_summary = dict((job.delivery_qc or {}).get("summary") or {})
    db.add(ProductEvent(
        tenant_id=str(job.tenant_id), user_id=current_user["id"], job_id=job_id,
        name="delivery_qc_approved",
        properties={
            "mode": str((job.delivery_qc or {}).get("mode") or "off"),
            "decision": str((job.delivery_qc or {}).get("decision") or "missing"),
            "open_count": int(_qc_summary.get("open_count") or 0),
            "fail_count": int(_qc_summary.get("fail_count") or 0),
            "warn_count": int(_qc_summary.get("warn_count") or 0),
        },
    ))
    db.commit()

    if learning_version is not None:
        try:
            from queue_jobs import enqueue_correction_learning
            props = dict(learning_event.properties or {}) if learning_event else {}
            enqueue_correction_learning(
                job_id, learning_version.id,
                active_edit_ms=props.get("active_edit_ms"),
                session_id=props.get("session_id"),
            )
        except Exception as exc:
            logger.warning(
                "[QUALITY-LEARNING] final approval capture enqueue failed job=%s: %s",
                job_id, exc,
            )

    # 2026-05-30 perf: drop the cached /usage entry for this operator so
    # the sidebar badge reflects the +1 immediately, not after the 30 s
    # TTL. Failure is silent — if Redis is down the next /usage just
    # bypasses the cache and reads the live counter.
    try:
        from cache import invalidate, usage_key
        usage_owners = {
            (job.tenant_id, job.user_id),
            (current_user["tenant_id"], current_user["id"]),
        }
        for tenant_id, user_id in usage_owners:
            invalidate(usage_key(tenant_id, user_id))
    except Exception:
        pass

    return {"ok": True, "status": "done", "job_id": job_id}


@app.post("/jobs/{job_id}/delivery-qc/issues/{issue_id}/decision")
async def decide_delivery_qc_issue(
    job_id: str,
    issue_id: str,
    body: DeliveryQCIssueDecisionRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist one reviewer decision; raw lyric text never enters telemetry."""
    query = db.query(Job).filter(Job.job_id == job_id)
    if current_user.get("role") != "admin":
        query = query.filter(Job.tenant_id == current_user["tenant_id"])
    job = query.with_for_update().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    report = dict(job.delivery_qc or {})
    if report.get("status") != "COMPLETE":
        raise HTTPException(status_code=409, detail="delivery_qc_report_stale")
    found = None
    issues = []
    status_map = {
        "acknowledged": "ACKNOWLEDGED",
        "rejected": "REJECTED",
        "resolved_manual": "RESOLVED_MANUAL",
    }
    for raw in report.get("issues") or []:
        row = dict(raw)
        if str(row.get("issue_id")) == issue_id:
            found = row
            row["status"] = status_map[body.decision]
            row["operator_decision"] = {
                "decision": body.decision, "reason": body.reason,
                "user_id": current_user["id"],
                "decided_at": datetime.now(timezone.utc).isoformat(),
            }
        issues.append(row)
    if found is None:
        raise HTTPException(status_code=404, detail="delivery_qc_issue_not_found")
    report["issues"] = issues
    open_rows = [row for row in issues if row.get("status") == "OPEN"]
    report["summary"] = {
        **dict(report.get("summary") or {}),
        "open_count": len(open_rows),
        "fail_count": sum(row.get("severity") == "FAIL" for row in open_rows),
        "warn_count": sum(row.get("severity") == "WARN" for row in open_rows),
    }
    from delivery_qc_runtime import approval_gate, effective_delivery_qc_mode
    report["approval"] = approval_gate(report, effective_delivery_qc_mode())
    job.delivery_qc = report
    db.add(ProductEvent(
        tenant_id=str(job.tenant_id), user_id=current_user["id"], job_id=job_id,
        name="delivery_qc_issue_decision",
        properties={
            "issue_id": issue_id, "decision": body.decision,
            "code": str(found.get("code") or "unknown"),
            "category": str(found.get("category") or "unknown"),
            "severity": str(found.get("severity") or "unknown"),
        },
    ))
    db.add(AuditLog(
        user_id=current_user["id"], action="delivery_qc.issue_decision",
        detail={"job_id": job_id, "issue_id": issue_id, "decision": body.decision},
    ))
    db.commit()
    return {"ok": True, "delivery_qc": report}


@app.post("/jobs/{job_id}/delivery-qc/external-result")
async def record_delivery_qc_external_result(
    job_id: str,
    body: DeliveryQCExternalResultRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record label QC outcome so 'zero external findings' is measurable."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    report = dict(job.delivery_qc or {})
    history = list(report.get("external_results") or [])
    result = {
        "source": body.source, "report_id": body.report_id,
        "finding_count": body.finding_count,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "recorded_by": current_user["id"],
    }
    history.append(result)
    report["external_results"] = history[-20:]
    job.delivery_qc = report
    db.add(ProductEvent(
        tenant_id=str(job.tenant_id), user_id=current_user["id"], job_id=job_id,
        name="delivery_qc_external_result",
        properties={"source": body.source, "finding_count": body.finding_count},
    ))
    db.commit()
    return {"ok": True, "external_result": result}


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

    job_query = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        job_query = job_query.filter(
            JobModel.tenant_id == current_user["tenant_id"],
        )
    job = job_query.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "pending_review":
        raise HTTPException(status_code=400, detail="Job is not pending review")

    is_cross_tenant_admin = (
        current_user.get("role") == "admin"
        and job.tenant_id != current_user.get("tenant_id")
    )
    _audit_cross_tenant_access(
        db, current_user, job, "reject_job", commit=False,
    )

    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.approved_at = datetime.now(timezone.utc)
    job.review_notes = body.notes or None

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.reject",
        detail={"job_id": job_id, "notes": body.notes,
                "tenant_id": job.tenant_id,
                "owner_user_id": job.user_id,
                "actor_tenant_id": current_user.get("tenant_id"),
                "cross_tenant_admin": is_cross_tenant_admin},
    ))
    db.commit()

    try:
        from cache import invalidate, usage_key
        usage_owners = {
            (job.tenant_id, job.user_id),
            (current_user["tenant_id"], current_user["id"]),
        }
        for tenant_id, user_id in usage_owners:
            invalidate(usage_key(tenant_id, user_id))
    except Exception:
        pass

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
    # Cross-tenant para admins de plataforma: mismo contrato que _job_scope
    # (pedido CEO 2026-06-11) — el rol admin abre el video de cualquier
    # cliente para resolver incidentes. Sin bypass, el editor de un tenant
    # ajeno abría MUDO (audio no disponible) aunque el admin ya podía ver el
    # job. Acceso auditado (compliance UMG).
    _audio_q = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        _audio_q = _audio_q.filter(JobModel.tenant_id == current_user["tenant_id"])
    job = _audio_q.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _audit_cross_tenant_access(db, current_user, job, "source_audio")

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
        extension = os.path.splitext(job.input_r2_key)[1].lower()
        content_type = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
        }.get(extension)
        url = storage.generate_signed_url(
            job.input_r2_key,
            expiry_seconds=3600,
            response_content_type=content_type,
        )
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
            url = storage.generate_signed_url(
                r2_key,
                expiry_seconds=3600,
                response_content_type="video/mp4",
            )
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

    safe_basename = _safe_basename(audio.filename)

    _enforce_disk_capacity()

    # Stream to a temp file with size + magic-bytes validation.
    import tempfile
    fd, temp_path = tempfile.mkstemp(prefix=f"restore_{job_id}_", suffix=".bin")
    os.close(fd)
    try:
        size_bytes = await _stream_upload_to_disk(audio, temp_path)
        _validate_audio_file_on_disk(audio.filename, temp_path)

        from quality_cache import sha256_file
        audio_sha256 = sha256_file(temp_path)
        # Content-addressed storage makes replacement recoverable and prevents
        # a quality worker from observing different bytes under the same key.
        target_key = storage.content_addressed_input_key(
            str(job.tenant_id or ""), job_id, audio_sha256, safe_basename,
        )

        uploaded = storage.upload_file(temp_path, target_key)
        if not uploaded:
            raise HTTPException(status_code=503, detail="R2 unavailable.")
        uploaded_etag = storage.object_etag(target_key) or audio_sha256
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    job = (
        db.query(JobModel).filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .populate_existing().with_for_update().one()
    )
    previous_key = job.input_r2_key
    previous_hash = job.input_audio_sha256
    previous_audio_revision = int(job.audio_revision or 0)
    job.input_r2_key = target_key
    job.input_audio_sha256 = audio_sha256
    job.input_audio_etag = uploaded_etag
    job.audio_revision = previous_audio_revision + 1
    job.active_quality_attempt_id = None

    segment_rows = list(job.segments_json or [])
    starts = [float(row.get("start") or 0) for row in segment_rows if isinstance(row, dict)]
    ends = [float(row.get("end") or 0) for row in segment_rows if isinstance(row, dict)]
    quality = dict(job.transcription_quality or {})
    quality.update({
        "policy_version": "lyrics-quality-v6",
        "decision": "review_required",
        "render_blocked": True,
        "analysis_status": "superseded",
        "analysis_pending": False,
        "audio_sha256": audio_sha256,
        "audio_revision": job.audio_revision,
        "unsafe_windows": ([{
            "id": f"audio-replaced-{job.audio_revision}",
            "start": min(starts) if starts else 0.0,
            "end": max(ends) if ends else 0.001,
            "reasons": ["source_audio_replaced"],
        }] if segment_rows else []),
        "reasons": [{
            "code": "source_audio_replaced", "severity": "critical",
            "value": job.audio_revision,
        }],
    })
    job.transcription_quality = quality
    from database import EditorDocument
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).with_for_update().first()
    if document is not None:
        document.quality_proposal = None

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
            "previous_key": previous_key,
            "previous_audio_sha256": previous_hash,
            "audio_sha256": audio_sha256,
            "previous_audio_revision": previous_audio_revision,
            "audio_revision": job.audio_revision,
        },
    ))
    quality_outbox_id = _create_editor_quality_outbox(
        db, job, revision=int(job.segments_revision or 0),
        segments=segment_rows, quality=quality, reason="source_audio_restored",
    )
    db.commit()

    try:
        storage.delete_object(f"waveform/{job_id}.json")
    except Exception:
        pass

    _dispatch_editor_quality_outbox(quality_outbox_id)

    return {
        "job_id": job_id,
        "key": target_key,
        "size_mb": round(size_bytes / 1024 / 1024, 2),
        "restored": True,
        "audio_sha256": audio_sha256,
        "audio_revision": job.audio_revision,
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

    job_query = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        job_query = job_query.filter(
            JobModel.tenant_id == current_user["tenant_id"],
        )
    job = job_query.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _audit_cross_tenant_access(db, current_user, job, "waveform")
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


class EditorPatchRequest(BaseModel):
    base_revision: int
    segments: list[dict]
    checkpoint: str = "autosave"


def _editor_changed_windows(previous: list[dict], current: list[dict]) -> list[dict]:
    """Bound quality re-analysis to lines whose content or timing changed."""
    def key(item, index):
        return str(item.get("_id") or f"idx_{index}")

    before = {key(item, i): item for i, item in enumerate(previous) if isinstance(item, dict)}
    after = {key(item, i): item for i, item in enumerate(current) if isinstance(item, dict)}
    windows = []
    for item_key in set(before) | set(after):
        old, new = before.get(item_key), after.get(item_key)
        if old == new:
            continue
        candidates = [item for item in (old, new) if isinstance(item, dict)]
        starts = [float(item.get("start") or 0) for item in candidates]
        ends = [float(item.get("end") or 0) for item in candidates]
        if ends and max(ends) > min(starts):
            windows.append({
                "start": min(starts), "end": max(ends),
                "reason": "operator_edited_segment",
            })
    return windows


def _invalidate_quality_after_editor_save(
    job, *, revision: int, segments: list[dict],
    previous_segments: list[dict] | None = None,
) -> dict:
    """Bind editor changes to a fresh, fail-closed quality snapshot."""
    from transcription_quality import evaluate, supersede_pending_analysis

    current = job.transcription_quality
    if not isinstance(current, dict):
        current = evaluate(segments, None)
        current["timing_source"] = job.timing_source or "unknown"
    else:
        current = dict(current)
    current["unsafe_windows"] = [
        *(current.get("unsafe_windows") or []),
        *_editor_changed_windows(previous_segments or [], segments),
    ]
    if isinstance(getattr(job, "delivery_qc", None), dict):
        from delivery_qc_runtime import mark_delivery_qc_stale
        job.delivery_qc = mark_delivery_qc_stale(
            job.delivery_qc, revision=revision, reason="editor_segments_changed",
        )
    return supersede_pending_analysis(
        current, revision=revision, segments=segments,
    ) or current


def _create_editor_quality_outbox(
    db: Session, job: Job, *, revision: int, segments: list[dict],
    quality: dict | None, reason: str,
) -> str | None:
    """Commit reanalysis intent atomically with the editor/audio mutation."""
    from transactional_outbox import create_quality_outbox_event
    event = create_quality_outbox_event(
        db, job=job, revision=revision, segments=segments,
        quality=quality, reason=reason,
    )
    return event.id if event is not None else None


def _dispatch_editor_quality_outbox(event_id: str | None) -> None:
    if not event_id:
        return
    try:
        from transactional_outbox import dispatch_outbox_event
        result = dispatch_outbox_event(event_id)
        if result.get("status") not in {"dispatched", "skipped"}:
            logger.warning(
                "[QUALITY-OUTBOX] publication pending event=%s status=%s",
                event_id, result.get("status"),
            )
            from queue_jobs import ensure_job_outbox_reconciler_scheduled
            ensure_job_outbox_reconciler_scheduled()
    except Exception as exc:
        logger.warning(
            "[QUALITY-OUTBOX] immediate dispatch failed event=%s error=%s",
            event_id, type(exc).__name__,
        )


class EditorRestoreRequest(BaseModel):
    version_id: str
    base_revision: int


class EditorQualityProposalApplyRequest(BaseModel):
    base_revision: int = Field(ge=0)
    window_ids: list[str] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=16, max_length=160)


class EditorQualityProposalDismissRequest(BaseModel):
    base_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=160)
    # Categorical only: AuditLog must never become a side channel for lyrics.
    reason: str = Field(
        default="operator_dismissed",
        pattern=(
            r"^(operator_dismissed|incorrect_content|incorrect_timing|"
            r"not_helpful|already_fixed)$"
        ),
    )


class EditorOperatorSuggestionRejectRequest(BaseModel):
    base_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=160)
    reason: str = Field(
        default="operator_rejected",
        pattern=(
            r"^(operator_rejected|incorrect_content|incorrect_timing|"
            r"not_helpful|already_fixed|uncertain)$"
        ),
    )


class EditorQualityObservationRequest(BaseModel):
    base_revision: int = Field(ge=0)
    window_id: str = Field(min_length=1, max_length=128)
    verdict: str = Field(pattern=r"^(correct|incorrect|uncertain)$")
    idempotency_key: str = Field(min_length=16, max_length=160)


class EditorConflictRequest(BaseModel):
    strategy: str
    server_revision: int = Field(ge=0)
    segments: list[dict] | None = None


class EditorActivityHeartbeatRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=100)
    activity_seq: int = Field(ge=0, le=10_000_000)


class ProductEventItem(BaseModel):
    name: str
    job_id: str | None = None
    occurred_at: str | None = None
    properties: dict = Field(default_factory=dict)


class ProductEventsRequest(BaseModel):
    events: list[ProductEventItem]


def _editor_document_or_404(db: Session, job_id: str, current_user: dict):
    # Keep rollback effective: production tenants outside the canary cannot
    # mutate the durable editor by calling the API directly.
    if not current_user.get("features", {}).get("editor_v2"):
        raise HTTPException(status_code=404, detail="Job not found.")
    # Platform admins already have audited cross-tenant access to the review
    # and /edit flows (see `_job_scope` and POST /edit).  Editor 2.0 used a
    # stricter tenant-only lookup here, so an admin could open a historical
    # client's lyrics through the legacy status endpoint but GET /editor
    # returned 404.  The frontend then waited forever for durable hydration
    # and kept "Aprobar" disabled.  Resolve the same Job the surrounding
    # review flow authorises, while keeping regular users tenant-isolated.
    if current_user.get("role") == "admin":
        job = db.query(Job).filter(Job.job_id == job_id).first()
    else:
        job = get_job_for_tenant(db, job_id, current_user["tenant_id"])
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    try:
        document = get_or_create_document(
            db, job_id, job.tenant_id, job.segments_json or [],
        )
    except (LookupError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid editor segments.") from None
    # Opening/using an explicit editor ID wins over a soft-dedup race.  This
    # also refreshes the reaper activity anchor for long editing sessions.
    from jobs import touch_user_activity
    touch_user_activity(db, job)
    return job, document


def _editor_conflict_payload(db: Session, document: EditorDocument) -> dict:
    payload = serialize_document(db, document)
    return {
        "detail": "editor_revision_conflict",
        "server_revision": payload["revision"],
        "server_segments": payload["segments"],
        "updated_by": payload["updated_by"],
        "updated_at": payload["updated_at"],
    }


@app.get("/editor/{job_id}")
async def get_editor_document(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    _audit_cross_tenant_access(db, current_user, job, "editor_read", commit=False)
    revoke_quality_proposal_if_disabled(document)
    # Serialization can erase an expired proposal. Build the response before
    # commit so that tenant-scoped raw text is durably removed by this GET.
    payload = serialize_document(db, document)
    editor_quality = getattr(job, "transcription_quality", None)
    if not isinstance(editor_quality, dict) and job.segments_json:
        # Expand compatibility for legacy jobs without mutating on GET. The
        # editor receives an explicit fail-closed verdict and its approval
        # endpoint persists the revision/hash-scoped acknowledgement.
        from transcription_quality import evaluate as evaluate_transcription_quality
        editor_quality = evaluate_transcription_quality(job.segments_json, None)
        editor_quality["evaluated_revision"] = int(job.segments_revision or 0)
        editor_quality["timing_source"] = job.timing_source or "unknown"
    if isinstance(editor_quality, dict):
        from transcription_quality import effective_policy_mode
        editor_quality = dict(editor_quality)
        editor_quality["mode"] = effective_policy_mode(
            job_id=job_id, tenant_id=str(job.tenant_id or ""),
        )
    payload.update({
        "artist": job.artist,
        "song_title": job.song_title,
        "filename": job.filename,
        "job_status": job.status,
        "transcription_quality": editor_quality,
    })
    db.commit()  # lazy migration/reconciliation/expiry is an intentional side effect
    return payload


@app.patch("/editor/{job_id}")
async def patch_editor_document(
    job_id: str,
    body: EditorPatchRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    quality_outbox_id = None
    previous_editor_segments = [
        dict(item) for item in (document.current_segments or [])
    ]
    operator_proposal_before = dict(document.quality_proposal or {})
    try:
        _audit_cross_tenant_access(db, current_user, job, "editor_save", commit=False)
        document, version, applied = save_document(
            db, job, document, current_user["id"], body.base_revision,
            body.segments, body.checkpoint,
        )
        if applied:
            manual_suggestion_decisions = (
                rebase_operator_suggestions_after_manual_edit(
                    document, operator_proposal_before,
                    previous_editor_segments,
                )
            )
            from correction_learning import invalidate_job_observations
            invalidate_job_observations(db, job_id, "later_editor_revision")
            job.transcription_quality = _invalidate_quality_after_editor_save(
                job, revision=document.revision,
                segments=list(document.current_segments or []),
                previous_segments=previous_editor_segments,
            )
            quality_outbox_id = _create_editor_quality_outbox(
                db, job, revision=document.revision,
                segments=list(document.current_segments or []),
                quality=job.transcription_quality, reason="editor_save",
            )
            for evidence in manual_suggestion_decisions:
                db.add(ProductEvent(
                    tenant_id=str(job.tenant_id),
                    user_id=current_user["id"], job_id=job_id,
                    name="editor_operator_suggestion_decision",
                    properties={
                        key: value for key, value in evidence.items()
                        if key != "decided_at" and value is not None
                    } | {
                        "pipeline_release": str(
                            (job.transcription_quality or {}).get(
                                "pipeline_release"
                            ) or "unknown"
                        ),
                    },
                ))
        db.commit()
    except RuntimeError:
        db.rollback()
        _, document = _editor_document_or_404(db, job_id, current_user)
        raise HTTPException(
            status_code=409,
            detail=_editor_conflict_payload(db, document),
        ) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _dispatch_editor_quality_outbox(quality_outbox_id)
    return {
        "job_id": job_id,
        "revision": document.revision,
        "version_id": version.id if version else None,
        "saved_at": document.updated_at.isoformat(),
        "applied": applied,
    }


@app.post("/editor/{job_id}/quality-proposals/{proposal_id}/apply")
async def apply_editor_quality_proposal(
    job_id: str,
    proposal_id: str,
    body: EditorQualityProposalApplyRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    quality_outbox_id = None
    previous = [dict(item) for item in (document.current_segments or [])]
    proposal_before = dict(document.quality_proposal or {})
    windows_before = {
        str(item.get("id")): dict(item)
        for item in (proposal_before.get("windows") or [])
        if isinstance(item, dict)
    }
    try:
        document, version, applied = apply_quality_proposal(
            db, job, document, current_user["id"], proposal_id=proposal_id,
            base_revision=body.base_revision, window_ids=body.window_ids,
            idempotency_key=body.idempotency_key,
        )
        if applied:
            from correction_learning import invalidate_job_observations
            invalidate_job_observations(db, job_id, "quality_proposal_applied")
            job.transcription_quality = _invalidate_quality_after_editor_save(
                job, revision=document.revision,
                segments=list(document.current_segments or []),
                previous_segments=previous,
            )
            quality_outbox_id = _create_editor_quality_outbox(
                db, job, revision=document.revision,
                segments=list(document.current_segments or []),
                quality=job.transcription_quality,
                reason="quality_proposal_applied",
            )
            if proposal_before.get("operator_suggestion_only") is True:
                for window_id in body.window_ids:
                    suggestion = windows_before.get(str(window_id)) or {}
                    suggestion_type = str(
                        suggestion.get("suggestion_type") or "unknown"
                    )
                    current_end = suggestion.get("current_end")
                    proposed_end = suggestion.get("proposed_end")
                    proposed_delta_ms = None
                    if suggestion_type == "timing":
                        try:
                            proposed_delta_ms = round(1000 * (
                                float(proposed_end) - float(current_end)
                            ))
                        except (TypeError, ValueError):
                            proposed_delta_ms = None
                    db.add(ProductEvent(
                        tenant_id=str(job.tenant_id),
                        user_id=current_user["id"], job_id=job_id,
                        name="editor_operator_suggestion_decision",
                        properties={
                            "decision": "accepted",
                            "suggestion_type": suggestion_type,
                            "confidence": str(
                                suggestion.get("confidence") or "unknown"
                            ),
                            "impact_ms": int(
                                suggestion.get("impact_ms") or 0
                            ),
                            "proposed_delta_ms": proposed_delta_ms,
                            "pipeline_release": str(
                                (job.transcription_quality or {}).get(
                                    "pipeline_release"
                                ) or "unknown"
                            ),
                        },
                    ))
        db.add(AuditLog(
            user_id=current_user["id"], action="editor.quality_proposal_apply",
            detail={
                "job_id": job_id, "proposal_id": proposal_id,
                "window_ids": list(body.window_ids), "applied": applied,
                "revision": int(document.revision or 0),
            },
        ))
        db.commit()
    except QualityProposalsDisabled as exc:
        # apply_quality_proposal revoked the raw pending payload under lock;
        # preserve that deletion even though the requested action is disabled.
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="quality_proposal_not_found") from None
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _dispatch_editor_quality_outbox(quality_outbox_id)
    return {
        "job_id": job_id, "proposal_id": proposal_id,
        "revision": int(document.revision or 0),
        "version_id": version.id if version else None,
        "applied": applied, "idempotent": not applied,
    }


@app.post(
    "/editor/{job_id}/quality-proposals/{proposal_id}/windows/{window_id}/reject"
)
async def reject_editor_operator_suggestion(
    job_id: str,
    proposal_id: str,
    window_id: str,
    body: EditorOperatorSuggestionRejectRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    try:
        evidence, rejected = reject_operator_suggestion(
            db, document, proposal_id=proposal_id, window_id=window_id,
            base_revision=body.base_revision, reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
        if rejected:
            db.add(ProductEvent(
                tenant_id=str(job.tenant_id), user_id=current_user["id"],
                job_id=job_id, name="editor_operator_suggestion_decision",
                properties={
                    key: value for key, value in evidence.items()
                    if key not in {"idempotency_hash", "decided_at"}
                } | {"pipeline_release": str(
                    (job.transcription_quality or {}).get(
                        "pipeline_release"
                    ) or "unknown"
                )},
            ))
            db.add(AuditLog(
                user_id=current_user["id"],
                action="editor.operator_suggestion_reject",
                detail={
                    "job_id": job_id, "proposal_id": proposal_id,
                    "window_id_hash": evidence.get("window_id"),
                    "suggestion_type": evidence.get("suggestion_type"),
                    "reason": body.reason,
                },
            ))
        db.commit()
    except QualityProposalsDisabled as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {
        "job_id": job_id, "proposal_id": proposal_id,
        "window_id": window_id, "rejected": rejected,
        "idempotent": not rejected,
    }


@app.post("/editor/{job_id}/quality-proposals/{proposal_id}/dismiss")
async def dismiss_editor_quality_proposal(
    job_id: str,
    proposal_id: str,
    body: EditorQualityProposalDismissRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, document = _editor_document_or_404(db, job_id, current_user)
    try:
        dismissed = dismiss_quality_proposal(
            db, document, proposal_id=proposal_id,
            base_revision=body.base_revision, idempotency_key=body.idempotency_key,
        )
        db.add(AuditLog(
            user_id=current_user["id"], action="editor.quality_proposal_dismiss",
            detail={
                "job_id": job_id, "proposal_id": proposal_id,
                "reason": body.reason, "dismissed": dismissed,
            },
        ))
        db.commit()
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="quality_proposal_not_found") from None
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {
        "job_id": job_id, "proposal_id": proposal_id,
        "dismissed": dismissed, "idempotent": not dismissed,
    }


@app.post("/editor/{job_id}/quality-proposals/{proposal_id}/observe")
async def observe_editor_quality_proposal(
    job_id: str,
    proposal_id: str,
    body: EditorQualityObservationRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    try:
        evidence, recorded = record_quality_observation(
            db, job, document,
            proposal_id=proposal_id,
            window_id=body.window_id,
            base_revision=body.base_revision,
            verdict=body.verdict,
            idempotency_key=body.idempotency_key,
        )
        if recorded:
            db.add(ProductEvent(
                tenant_id=str(job.tenant_id), user_id=current_user["id"],
                job_id=job_id, name="quality_consensus_observation",
                properties=evidence,
            ))
            db.add(AuditLog(
                user_id=current_user["id"],
                action="editor.quality_consensus_observe",
                detail={
                    "job_id": job_id,
                    "proposal_id": proposal_id,
                    "window_id_hash": evidence.get("window_id"),
                    "verdict": body.verdict,
                },
            ))
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {
        "job_id": job_id,
        "proposal_id": proposal_id,
        "window_id": body.window_id,
        "verdict": body.verdict,
        "recorded": recorded,
        "idempotent": not recorded,
    }


@app.post("/editor/{job_id}/lock")
@app.post("/editor/{job_id}/lock/heartbeat")
async def editor_lock(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, document = _editor_document_or_404(db, job_id, current_user)
    result = acquire_lock(db, document, current_user["id"])
    db.commit()
    return result


@app.delete("/editor/{job_id}/lock")
async def editor_unlock(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, document = _editor_document_or_404(db, job_id, current_user)
    if not release_lock(db, document, current_user["id"]):
        db.rollback()
        raise HTTPException(status_code=409, detail="editor_lock_owned_by_other_user")
    db.commit()
    return {"released": True}


@app.post("/editor/{job_id}/activity/heartbeat")
@limiter.limit("12/minute")
async def editor_activity_heartbeat(
    request: Request,
    job_id: str,
    body: EditorActivityHeartbeatRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record a server-timestamped active-editor sample for operational QA.

    Elapsed time is derived later from bounded gaps between these rows. The
    endpoint deliberately ignores browser clocks and client-reported minutes.
    """
    from evidence_attestation import lyric_snapshot_hash

    job, document = _editor_document_or_404(db, job_id, current_user)
    lock_expires = document.lock_expires_at
    if lock_expires is not None and lock_expires.tzinfo is None:
        lock_expires = lock_expires.replace(tzinfo=timezone.utc)
    if (
        document.lock_user_id != current_user["id"]
        or lock_expires is None
        or lock_expires <= datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=409, detail="editor_active_lock_required",
        )
    quality = job.transcription_quality or {}
    prior_heartbeats = db.query(ProductEvent).filter(
        ProductEvent.name == "editor_activity_heartbeat",
        ProductEvent.job_id == job_id,
        ProductEvent.user_id == current_user["id"],
    ).order_by(ProductEvent.id.desc()).all()
    previous = next((
        row for row in prior_heartbeats
        if (row.properties or {}).get("session_id") == body.session_id
    ), None)
    expected_seq = int((previous.properties or {}).get("activity_seq") or 0) + 1 \
        if previous is not None else 1
    if body.activity_seq != expected_seq:
        raise HTTPException(
            status_code=409, detail="editor_activity_sequence_conflict",
        )
    event = ProductEvent(
        tenant_id=current_user["tenant_id"], user_id=current_user["id"],
        job_id=job_id, name="editor_activity_heartbeat",
        occurred_at=datetime.now(timezone.utc),
        properties={
            "session_id": body.session_id,
            "activity_seq": body.activity_seq,
            "revision": int(document.revision or 0),
            "snapshot_sha256": lyric_snapshot_hash(document.current_segments or []),
            "pipeline_release": str(
                quality.get("pipeline_release") or "unknown"
            )[:64],
            "pipeline_config_fingerprint": str(
                quality.get("pipeline_config_fingerprint") or "unknown"
            )[:32],
        },
    )
    db.add(event)
    db.flush()
    event_id = event.id
    db.commit()
    return {
        "event_id": event_id,
        "revision": int(document.revision or 0),
        "snapshot_sha256": event.properties["snapshot_sha256"],
    }


@app.get("/editor/{job_id}/versions")
async def get_editor_versions(
    job_id: str,
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, document = _editor_document_or_404(db, job_id, current_user)
    return {"versions": list_versions(db, document, limit=limit, offset=offset)}


@app.get("/editor/{job_id}/versions/{version_id}")
async def get_editor_version(
    job_id: str,
    version_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _, document = _editor_document_or_404(db, job_id, current_user)
    version = get_version(db, document, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="editor_version_not_found")
    return {
        "id": version.id,
        "revision": version.revision,
        "reason": version.reason,
        "is_approved": bool(version.is_approved),
        "segments": version.segments,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


@app.post("/editor/{job_id}/restore")
async def restore_editor_version(
    job_id: str,
    body: EditorRestoreRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    previous = [dict(item) for item in (document.current_segments or [])]
    quality_outbox_id = None
    try:
        _audit_cross_tenant_access(db, current_user, job, "editor_restore", commit=False)
        document, version = restore_version(
            db, job, document, current_user["id"], body.version_id, body.base_revision,
        )
        from correction_learning import invalidate_job_observations
        invalidate_job_observations(db, job_id, "editor_version_restored")
        job.transcription_quality = _invalidate_quality_after_editor_save(
            job, revision=document.revision,
            segments=list(document.current_segments or []),
            previous_segments=previous,
        )
        quality_outbox_id = _create_editor_quality_outbox(
            db, job, revision=document.revision,
            segments=list(document.current_segments or []),
            quality=job.transcription_quality, reason="editor_version_restored",
        )
        db.commit()
    except LookupError:
        raise HTTPException(status_code=404, detail="editor_version_not_found") from None
    except RuntimeError:
        db.rollback()
        _, document = _editor_document_or_404(db, job_id, current_user)
        raise HTTPException(
            status_code=409,
            detail=_editor_conflict_payload(db, document),
        ) from None
    _dispatch_editor_quality_outbox(quality_outbox_id)
    return {
        "job_id": job_id,
        "revision": document.revision,
        "version_id": version.id,
        "segments": document.current_segments,
    }


@app.post("/editor/{job_id}/conflicts/resolve")
async def resolve_editor_conflict(
    job_id: str,
    body: EditorConflictRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job, document = _editor_document_or_404(db, job_id, current_user)
    previous = [dict(item) for item in (document.current_segments or [])]
    quality_outbox_id = None
    try:
        _audit_cross_tenant_access(db, current_user, job, "editor_conflict_resolve", commit=False)
        document, version, applied = resolve_conflict(
            db, job, document, current_user["id"], body.server_revision,
            body.strategy, body.segments,
        )
        if applied:
            from correction_learning import invalidate_job_observations
            invalidate_job_observations(db, job_id, "editor_conflict_resolved")
            job.transcription_quality = _invalidate_quality_after_editor_save(
                job, revision=document.revision,
                segments=list(document.current_segments or []),
                previous_segments=previous,
            )
            quality_outbox_id = _create_editor_quality_outbox(
                db, job, revision=document.revision,
                segments=list(document.current_segments or []),
                quality=job.transcription_quality,
                reason="editor_conflict_resolved",
            )
        db.commit()
    except RuntimeError:
        db.rollback()
        _, document = _editor_document_or_404(db, job_id, current_user)
        raise HTTPException(
            status_code=409, detail=_editor_conflict_payload(db, document),
        ) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _dispatch_editor_quality_outbox(quality_outbox_id)
    return {
        **serialize_document(db, document),
        "version_id": version.id if version else None,
        "applied": applied,
    }


_PRODUCT_EVENT_NAMES = {
    "editor_opened", "editor_view_changed", "editor_seek",
    "editor_selection_created", "editor_group_moved", "editor_timing_changed",
    "editor_undo", "editor_autosave_success", "editor_autosave_failed",
    "editor_conflict", "editor_version_restored", "editor_approved",
    "editor_help_opened", "editor_operator_suggestions_shown",
    "editor_operator_suggestion_decision", "editor_audio_playback_failed",
}

# Ventana de /admin/product-metrics. Sin esto la única acotación era
# `LIMIT 10000` sobre TODA la tabla, así que el período medido dependía del
# volumen de telemetría y era imposible comparar dos lecturas entre sí.
PRODUCT_METRICS_WINDOW_DAYS = int(
    os.environ.get("PRODUCT_METRICS_WINDOW_DAYS", "28")
)

_PRODUCT_EVENT_PROPERTIES = {
    "editor_opened": {"line_count", "view", "source"},
    "editor_view_changed": {"from", "to"},
    "editor_seek": {"position_ms", "source"},
    "editor_selection_created": {"count", "method", "duration_ms"},
    "editor_group_moved": {"count", "delta_ms", "duration_ms"},
    "editor_timing_changed": {"count", "operation", "delta_ms", "duration_ms"},
    "editor_undo": {"operation", "count"},
    "editor_autosave_success": {"duration_ms", "checkpoint", "retry_count"},
    "editor_autosave_failed": {"duration_ms", "checkpoint", "reason", "status", "retry_count"},
    # `checkpoint`/`reason` los emite handleDurableStatus al entrar en conflicto
    # (el emisor histórico, que reportaba resolution, se removió con el
    # ConflictDialog en #1123). Sin estas dos claves el loop de /analytics/events
    # rechaza el evento ENTERO al primer key desconocido y el cliente se come el
    # error con un catch vacío: el contador quedaba clavado en 0 y alguien lo
    # iba a leer como "no hay conflictos".
    "editor_conflict": {
        "server_revision", "local_revision", "resolution",
        "checkpoint", "reason",
    },
    "editor_version_restored": {"from_revision", "to_revision"},
    "editor_approved": {
        "revision", "duration_ms", "line_count", "text_changes",
        "timing_changes", "lines_added", "lines_removed",
        "lines_reordered", "active_edit_ms", "quality_acknowledged",
    },
    "editor_help_opened": {"context"},
    "editor_operator_suggestions_shown": {
        "proposal_id", "total", "timing_count", "text_count",
        "vocalization_count",
    },
    "editor_operator_suggestion_decision": {
        "decision", "suggestion_type", "confidence", "impact_ms",
        "proposed_delta_ms", "chosen_delta_ms",
        "distance_to_proposal_ms", "reason", "window_id",
        "pipeline_release",
    },
    "editor_audio_playback_failed": {
        "position_ms", "media_error_code", "automatic_recovery_available",
    },
}
_PRODUCT_EVENT_COMMON_PROPERTIES = {"session_id"}
from product_telemetry import valid_property as _valid_product_event_property


@app.post("/analytics/events")
@limiter.limit("120/minute")
async def record_product_events(
    request: Request,
    body: ProductEventsRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(body.events) > 50:
        raise HTTPException(status_code=422, detail="A maximum of 50 events is accepted per batch.")
    accepted = 0
    rejected = 0
    for item in body.events:
        if item.name not in _PRODUCT_EVENT_NAMES:
            rejected += 1
            continue
        event_job = (
            get_job_for_tenant(db, item.job_id, current_user["tenant_id"])
            if item.job_id else None
        )
        if item.job_id and not event_job:
            rejected += 1
            continue
        allowed = _PRODUCT_EVENT_PROPERTIES[item.name] | _PRODUCT_EVENT_COMMON_PROPERTIES
        properties = {}
        invalid_properties = False
        for key, value in (item.properties or {}).items():
            if key not in allowed or not _valid_product_event_property(key, value):
                invalid_properties = True
                break
            properties[key] = value
        if invalid_properties:
            rejected += 1
            continue
        if len(json.dumps(properties, ensure_ascii=False)) > 2000:
            rejected += 1
            continue
        if event_job is not None:
            event_quality = event_job.transcription_quality or {}
            properties["pipeline_release"] = str(
                event_quality.get("pipeline_release") or "unknown"
            )[:64]
            properties["pipeline_config_fingerprint"] = str(
                event_quality.get("pipeline_config_fingerprint") or "unknown"
            )[:32]
            properties["timing_source"] = str(
                event_quality.get("timing_source") or "unknown"
            )[:64]
            properties["quality_policy_version"] = str(
                event_quality.get("policy_version") or "unknown"
            )[:64]
            properties["quality_reason_codes"] = ",".join(
                str(reason.get("code"))
                for reason in (event_quality.get("reasons") or [])
                if isinstance(reason, dict) and reason.get("code")
            )[:500]
        occurred_at = None
        if item.occurred_at:
            try:
                occurred_at = datetime.fromisoformat(item.occurred_at.replace("Z", "+00:00"))
            except ValueError:
                occurred_at = None
        db.add(ProductEvent(
            tenant_id=current_user["tenant_id"], user_id=current_user["id"],
            job_id=item.job_id, name=item.name, occurred_at=occurred_at,
            properties=properties,
        ))
        accepted += 1
    db.commit()
    return {"accepted": accepted, "rejected": rejected}


@app.get("/admin/product-metrics")
async def product_metrics(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    # Ventana temporal explícita + filtro por nombre. Antes esto tomaba las
    # 10.000 filas más recientes SIN filtrar: `editor_activity_heartbeat` se
    # emite cada 15 s por editor abierto (4/min) y `transcription_quality_
    # shadow_decision` lo escribe el worker, así que ambos —que no son eventos
    # del editor y ni siquiera están en el allowlist— consumían el cupo y
    # contaminaban `sample_size`, `counts` y el cálculo de sesiones. El
    # resultado era una ventana temporal desconocida y variable.
    # `ProductEvent.created_at` es DateTime(timezone=True): comparar contra un
    # datetime naive deja que Postgres lo interprete en la timezone de la sesión
    # y la ventana se corre en silencio. Aware desde el arranque.
    _window_start = datetime.now(timezone.utc) - timedelta(days=PRODUCT_METRICS_WINDOW_DAYS)
    query = (
        db.query(ProductEvent)
        .filter(ProductEvent.created_at >= _window_start)
        .filter(ProductEvent.name.in_(_PRODUCT_EVENT_NAMES))
    )
    if not current_user.get("is_super_admin"):
        query = query.filter(ProductEvent.tenant_id == current_user["tenant_id"])
    rows = query.order_by(ProductEvent.created_at.desc()).limit(10000).all()
    approval_query = (
        db.query(ProductEvent)
        .filter(ProductEvent.name == "editor_approved")
        .filter(ProductEvent.created_at >= _window_start)
    )
    if not current_user.get("is_super_admin"):
        approval_query = approval_query.filter(
            ProductEvent.tenant_id == current_user["tenant_id"]
        )
    approval_rows = approval_query.order_by(ProductEvent.created_at.desc()).limit(10000).all()
    rows_by_id = {row.id: row for row in rows}
    rows_by_id.update({row.id: row for row in approval_rows})
    rows = sorted(rows_by_id.values(), key=lambda row: row.created_at, reverse=True)
    event_job_ids = {row.job_id for row in rows if row.job_id}
    job_quality_context = {
        row.job_id: {
            "timing_source": row.timing_source or "unknown",
            "quality": row.transcription_quality or {},
        }
        for row in (
            db.query(Job.job_id, Job.timing_source, Job.transcription_quality)
            .filter(Job.job_id.in_(event_job_ids))
            .all()
            if event_job_ids else []
        )
    }
    counts: dict[str, int] = {}
    group_move_durations = []
    approval_durations = []
    correction_totals = {
        "text_changes": 0, "timing_changes": 0,
        "lines_added": 0, "lines_removed": 0,
        "lines_reordered": 0,
    }
    route_work: dict[str, dict] = {}
    release_work: dict[str, list[float]] = {}
    suggestion_metrics = {
        kind: {"shown": 0, "accepted": 0, "rejected": 0, "manual": 0}
        for kind in ("timing", "text", "vocalization")
    }
    seen_approvals: set[tuple] = set()
    sessions: dict[tuple, dict] = {}
    view_usage = {"basic": 0, "advanced": 0}
    for row in rows:
        properties = row.properties or {}
        if row.name == "editor_approved":
            approval_key = (row.job_id, properties.get("revision"))
            if approval_key in seen_approvals:
                continue
            seen_approvals.add(approval_key)
        counts[row.name] = counts.get(row.name, 0) + 1
        timestamp = row.occurred_at or row.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        session_id = properties.get("session_id")
        # Older clients have no session id; bucket by UTC date so two visits
        # months apart never become one enormous synthetic session.
        key = (row.user_id, row.job_id, session_id or timestamp.date().isoformat())
        session = sessions.setdefault(key, {"opened": None, "first_edit": None, "first": timestamp, "last": timestamp})
        session["first"] = min(session["first"], timestamp)
        session["last"] = max(session["last"], timestamp)
        if row.name == "editor_opened":
            session["opened"] = timestamp if session["opened"] is None else min(session["opened"], timestamp)
            view = properties.get("view")
            if view in view_usage:
                view_usage[view] += 1
        elif row.name == "editor_view_changed":
            view = properties.get("to")
            if view in view_usage:
                view_usage[view] += 1
        if row.name in {
            "editor_timing_changed", "editor_group_moved",
            "editor_selection_created", "editor_autosave_success",
        }:
            session["first_edit"] = timestamp if session["first_edit"] is None else min(session["first_edit"], timestamp)
        if row.name == "editor_group_moved" and isinstance(properties.get("duration_ms"), (int, float)):
            group_move_durations.append(float(properties["duration_ms"]))
        if row.name == "editor_approved":
            # Operational SLA is active editing time only. Legacy wall-clock
            # remains available in raw events but can never make the target
            # pass or contaminate release percentiles.
            active_duration = properties.get("active_edit_ms")
            if isinstance(active_duration, (int, float)):
                approval_durations.append(float(active_duration))
                release = str(properties.get("pipeline_release") or "unknown")
                release_work.setdefault(release, []).append(float(active_duration))
            for correction_name in correction_totals:
                value = properties.get(correction_name)
                if isinstance(value, (int, float)):
                    correction_totals[correction_name] += int(value)
            context = job_quality_context.get(row.job_id) or {}
            route = str(
                properties.get("timing_source")
                or context.get("timing_source") or "unknown"
            )
            route_row = route_work.setdefault(route, {
                "songs": 0, "durations": [], "text_changes": 0,
                "timing_changes": 0, "quality_reasons": {},
            })
            route_row["songs"] += 1
            if isinstance(active_duration, (int, float)):
                route_row["durations"].append(float(active_duration))
            for correction_name in ("text_changes", "timing_changes"):
                value = properties.get(correction_name)
                if isinstance(value, (int, float)):
                    route_row[correction_name] += int(value)
            immutable_reason_codes = str(properties.get("quality_reason_codes") or "")
            reason_codes = [code for code in immutable_reason_codes.split(",") if code]
            if not reason_codes:
                reason_codes = [
                    str(reason["code"])
                    for reason in ((context.get("quality") or {}).get("reasons") or [])
                    if isinstance(reason, dict) and reason.get("code")
                ]
            for code in reason_codes:
                route_row["quality_reasons"][code] = (
                    route_row["quality_reasons"].get(code, 0) + 1
                )
        elif row.name == "editor_operator_suggestions_shown":
            for kind in suggestion_metrics:
                count = properties.get(f"{kind}_count")
                if isinstance(count, (int, float)):
                    suggestion_metrics[kind]["shown"] += max(0, int(count))
        elif row.name == "editor_operator_suggestion_decision":
            kind = str(properties.get("suggestion_type") or "")
            decision = str(properties.get("decision") or "")
            if kind in suggestion_metrics:
                if decision in {"accepted", "rejected"}:
                    suggestion_metrics[kind][decision] += 1
                elif decision == "manual_override":
                    suggestion_metrics[kind]["manual"] += 1
    session_durations = [
        (session["last"] - session["first"]).total_seconds() * 1000
        for session in sessions.values() if session["last"] >= session["first"]
    ]
    first_edits = [
        (session["first_edit"] - session["opened"]).total_seconds() * 1000
        for session in sessions.values()
        if session["opened"] is not None and session["first_edit"] is not None
        and session["first_edit"] >= session["opened"]
    ]
    # `counts` sale de `rows`, que está truncado por el LIMIT — y el numerador
    # tenía además su propia query suplementaria, así que aprobaciones viejas
    # entraban y aperturas viejas no: el ratio se inflaba de forma sistemática
    # (medido en staging: 0,80 informado vs 0,55 real, y >1,0 alcanzable).
    # Numerador y denominador se cuentan en SQL sobre la MISMA ventana, sin
    # límite, así que la tasa deja de depender del volumen de telemetría.
    def _count_events(name: str) -> int:
        q = (
            db.query(func.count(ProductEvent.id))
            .filter(ProductEvent.name == name)
            .filter(ProductEvent.created_at >= _window_start)
        )
        if not current_user.get("is_super_admin"):
            q = q.filter(ProductEvent.tenant_id == current_user["tenant_id"])
        return int(q.scalar() or 0)

    opened = _count_events("editor_opened")
    approvals = _count_events("editor_approved")
    # Señal explícita de que las métricas derivadas de `rows` (sesiones,
    # view_usage, percentiles) están calculadas sobre una ventana recortada.
    events_truncated = len(rows) >= 10000
    def _percentile(values, quantile):
        if not values:
            return None
        ordered = sorted(values)
        if quantile == 0.50:
            import statistics
            return statistics.median(ordered)
        import math
        index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
        return ordered[index]
    review_p50 = _percentile(approval_durations, 0.50)
    review_p90 = _percentile(approval_durations, 0.90)
    route_metrics = {
        route: {
            "songs": values["songs"],
            "review_p50_ms": _percentile(values["durations"], 0.50),
            "review_p90_ms": _percentile(values["durations"], 0.90),
            "text_changes": values["text_changes"],
            "timing_changes": values["timing_changes"],
            "quality_reasons": values["quality_reasons"],
        }
        for route, values in route_work.items()
    }
    release_metrics = {
        release: {
            "songs": len(durations),
            "review_p50_ms": _percentile(durations, 0.50),
            "review_p90_ms": _percentile(durations, 0.90),
            "target_met": (
                len(durations) >= 30
                and _percentile(durations, 0.50) < 5 * 60 * 1000
                and _percentile(durations, 0.90) < 10 * 60 * 1000
            ),
        }
        for release, durations in release_work.items()
    }
    suggestion_summary = {}
    for kind, values in suggestion_metrics.items():
        decided = values["accepted"] + values["rejected"]
        suggestion_summary[kind] = {
            **values,
            "decided": decided,
            "acceptance_rate": (
                values["accepted"] / decided if decided else None
            ),
            "sanity_gate_met": (
                decided >= 10 and values["accepted"] / decided >= 0.70
            ) if decided else False,
        }
    return {
        "events": counts,
        "sample_size": len(rows),
        "view_usage": view_usage,
        "approval_rate": approvals / opened if opened else None,
        # True => `rows` tocó el LIMIT: sample_size/view_usage/sesiones y
        # los percentiles cubren menos que la ventana declarada.
        "events_truncated": events_truncated,
        "window_days": PRODUCT_METRICS_WINDOW_DAYS,
        "conflicts": counts.get("editor_conflict", 0),
        "autosave_failures": counts.get("editor_autosave_failed", 0),
        "undo_count": counts.get("editor_undo", 0),
        "avg_group_move_duration_ms": (
            sum(group_move_durations) / len(group_move_durations)
            if group_move_durations else None
        ),
        "avg_session_duration_ms": (
            sum(session_durations) / len(session_durations) if session_durations else None
        ),
        "avg_time_to_first_edit_ms": sum(first_edits) / len(first_edits) if first_edits else None,
        "operator_review": {
            "sample_size": len(approval_durations),
            "p50_ms": review_p50,
            "p90_ms": review_p90,
            "target_p50_ms": 5 * 60 * 1000,
            "target_p90_ms": 10 * 60 * 1000,
            "target_met": (
                len(approval_durations) >= 30
                and len(release_work) == 1
                and review_p50 is not None and review_p90 is not None
                and review_p50 < 5 * 60 * 1000
                and review_p90 < 10 * 60 * 1000
            ),
            "corrections": correction_totals,
            "by_timing_source": route_metrics,
            "by_pipeline_release": release_metrics,
            "suggestions": suggestion_summary,
        },
    }


class SaveSegmentsRequest(BaseModel):
    # Persisted to Job.segments_json (JSONB). Same shape /generate and
    # /edit accept. 5 MB upper bound mirrors /generate's segments_json
    # form-field cap — a long video can legitimately ship a few hundred
    # KB of segments.
    segments: list[dict] = Field(..., max_length=10000)
    # Server-owned OCC: the client sends the revision it hydrated.
    # None is accepted only while the job is still legacy revision zero.
    base_revision: int | None = Field(default=None, ge=0)


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
    from jobs import touch_user_activity

    job = (
        db.query(Job)
        .filter(Job.job_id == job_id)
        .with_for_update()
        .first()
    )
    # Bypass cross-tenant para admins de plataforma (mismo contrato que
    # _job_scope): un super admin corrige y GUARDA el job de cualquier
    # usuario cuando tiene problemas. Sin esto, abrir el editor de un job
    # ajeno dejaba el autoguardado en 404 permanente ("No pudimos guardar")
    # y las ediciones no persistían. Para no-admins el editor se comparte
    # entre miembros del mismo workspace; el control optimista por revisión
    # detecta cualquier guardado sobre una versión vieja.
    _is_platform_admin = bool(current_user.get("is_super_admin"))
    if (not job
            or (not _is_platform_admin
                and job.tenant_id != current_user["tenant_id"])):
        raise HTTPException(status_code=404, detail="Job not found.")
    _audit_cross_tenant_access(db, current_user, job, "save_segments", commit=False)

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
        # Outcome metric (issue #934, autosave poco confiable): el 409 por
        # status es el bloqueo silencioso recurrente del autosave — dejarlo
        # consultable por tenant sin depender del Sentry del browser.
        logger.warning(
            "[save-segments] rejected outcome=409-status job=%s tenant=%s status=%s",
            job_id, job.tenant_id, job.status,
        )
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

    # Reconcile the durable document before evaluating OCC so both legacy and
    # Editor 2.0 clients advance one shared monotonic revision.
    try:
        editor_document = get_or_create_document(
            db, job_id, job.tenant_id, job.segments_json or [],
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    current_revision = int(getattr(job, "segments_revision", 0) or 0)
    if body.base_revision is None and current_revision > 0:
        return JSONResponse(
            status_code=428,
            content={
                "code": "client_upgrade_required",
                "current_revision": current_revision,
            },
        )
    if body.base_revision is not None and body.base_revision != current_revision:
        # Network retries after a committed response are idempotent when the
        # canonical payload already matches what is stored.
        if job.segments_json == segs:
            return {
                "ok": True,
                "job_id": job_id,
                "applied": False,
                "revision": current_revision,
                "count": len(segs),
            }
        logger.warning(
            "[save-segments] conflict job=%s tenant=%s base=%s current=%s",
            job_id, job.tenant_id, body.base_revision, current_revision,
        )
        from ops_metrics import increment
        increment("segments_revision_conflict")
        return JSONResponse(
            status_code=409,
            content={
                "code": "stale_revision",
                "current_revision": current_revision,
                "updated_at": (
                    job.last_user_activity_at.isoformat()
                    if job.last_user_activity_at else None
                ),
            },
        )

    previous_segments_for_quality = [
        dict(item) for item in (job.segments_json or []) if isinstance(item, dict)
    ]
    # Audit log of what changed between prev and new — only when non-empty.
    # Motivation: operator (Tomas, 2026-05-19) reported "lines change places"
    # in autosync, and we had ZERO way to reconstruct what happened (only
    # the final sorted segments_json was persisted). This block writes a
    # compact diff per save so future complaints are diagnosable.
    # Capped to keep payload small (20 changed entries max with `truncated`
    # flag if exceeded).
    try:
        from database import AuditLog
        from correction_learning import hmac_identifier

        def _protected_text_ref(value: str) -> str | None:
            try:
                return hmac_identifier("audit_lyric", value)
            except RuntimeError:
                # Privacy is fail-closed: lengths/categories remain useful,
                # but an unkeyed or raw lexical reference is never persisted.
                return None

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
                    "text_changed": ps_text != ns_text,
                    "prev_text_length": len(ps_text),
                    "new_text_length": len(ns_text),
                    "prev_text_hmac": _protected_text_ref(ps_text),
                    "new_text_hmac": _protected_text_ref(ns_text),
                })
            if prev_idx != new_idx:
                reorder.append({"id": k, "from_idx": prev_idx, "to_idx": new_idx})

        if changed or reorder:
            correction_summary = {
                "changed_lines": len(changed),
                "text_changes": sum(
                    1 for item in changed
                    if item.get("text_changed")
                ),
                "timing_changes": sum(
                    1 for item in changed
                    if item.get("prev_start") != item.get("new_start")
                    or item.get("prev_end") != item.get("new_end")
                ),
                "reorders": len(reorder),
            }
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
                    "correction_summary": correction_summary,
                    "truncated": truncated,
                },
            ))
    except Exception as e:
        # Audit logging is best-effort — never break the save flow.
        logger.warning("[save-segments] audit log failed: %s", e)

    job.segments_json = segs
    job.segments_revision = current_revision + 1
    job.transcription_quality = _invalidate_quality_after_editor_save(
        job, revision=job.segments_revision, segments=segs,
        previous_segments=previous_segments_for_quality,
    )
    touch_user_activity(db, job)
    quality_outbox_id = None
    try:
        sync_legacy_snapshot(
            db, editor_document, current_user["id"], job.segments_json or [],
            int(getattr(job, "segments_revision", 0) or 0),
        )
        from correction_learning import invalidate_job_observations
        invalidate_job_observations(db, job_id, "later_editor_revision")
        quality_outbox_id = _create_editor_quality_outbox(
            db, job, revision=job.segments_revision, segments=segs,
            quality=job.transcription_quality, reason="legacy_editor_save",
        )
        db.commit()
    except (LookupError, ValueError, RuntimeError) as exc:
        db.rollback()
        logger.warning("[save-segments] editor bridge rejected job=%s: %s", job_id, exc)
        return JSONResponse(
            status_code=409,
            content={"code": "editor_state_conflict", "detail": str(exc)},
        )

    _dispatch_editor_quality_outbox(quality_outbox_id)

    # Outcome metric (issue #934): éxito consultable por tenant — junto con
    # el warning del 409 de arriba permite medir la tasa real de fallas del
    # autosave sin depender de los console.warn del browser.
    logger.info(
        "[save-segments] ok job=%s tenant=%s count=%d",
        job_id, job.tenant_id, len(segs),
    )

    return {
        "ok": True,
        "job_id": job_id,
        "saved_at": job.last_user_activity_at.isoformat() if job.last_user_activity_at else None,
        "count": len(segs),
        "applied": True,
        "revision": int(getattr(job, "segments_revision", 0) or 0),
    }


class TranscriptionQualityAckRequest(BaseModel):
    base_revision: int = Field(..., ge=0)
    confirmed_window_ids: list[str] = Field(default_factory=list, max_length=64)


@app.post("/jobs/{job_id}/transcription-quality/acknowledge")
@limiter.limit("12/minute")
async def acknowledge_transcription_quality(
    request: Request,
    job_id: str,
    body: TranscriptionQualityAckRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist an explicit, revision+content-scoped operator decision."""
    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
    is_platform_admin = bool(current_user.get("is_super_admin"))
    if (not job or (not is_platform_admin
                    and job.tenant_id != current_user["tenant_id"])):
        raise HTTPException(status_code=404, detail="Job not found.")
    current_revision = int(job.segments_revision or 0)
    from transcription_quality import effective_policy_mode
    if effective_policy_mode(
        job_id=job_id, tenant_id=str(job.tenant_id or ""),
    ) != "enforce":
        raise HTTPException(
            status_code=409,
            detail={"code": "transcription_quality_not_enforced"},
        )
    if body.base_revision != current_revision:
        return JSONResponse(
            status_code=409,
            content={"code": "stale_revision", "current_revision": current_revision},
        )
    from transcription_quality import (
        POLICY_VERSION, evaluate as evaluate_transcription_quality,
        segments_hash,
    )
    previous_quality = (
        dict(job.transcription_quality)
        if isinstance(job.transcription_quality, dict) else {}
    )
    if previous_quality.get("policy_version") != POLICY_VERSION:
        # Legacy/stale jobs have no trustworthy machine evidence under the
        # current policy. Create a fail-closed verdict, then let this explicit
        # operator action acknowledge only the exact current revision+hash.
        quality = evaluate_transcription_quality(job.segments_json or [], None)
        quality["evaluated_revision"] = current_revision
        quality["timing_source"] = str(
            previous_quality.get("timing_source")
            or job.timing_source or "unknown"
        )[:64]
    else:
        quality = previous_quality
    from transcription_quality import runtime_identity
    current_identity = runtime_identity()
    if (
        quality.get("pipeline_release") != current_identity["pipeline_release"]
        or
        quality.get("pipeline_config_fingerprint")
        != current_identity["pipeline_config_fingerprint"]
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "transcription_quality_stale_config"},
        )
    confirmed_ids = list(body.confirmed_window_ids or [])
    if any(not value or len(value) > 64 for value in confirmed_ids):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_confirmed_window_ids"},
        )
    if quality.get("policy_version") == POLICY_VERSION:
        from transcription_quality import confirmed_all_windows
        if not confirmed_all_windows(
            quality, confirmed_ids,
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unsafe_windows_not_confirmed",
                    "expected": len(quality.get("unsafe_windows") or []),
                },
            )
    from transcription_quality import (
        manual_override_allowed, quality_fingerprint,
    )
    if not manual_override_allowed(quality):
        raise HTTPException(
            status_code=409,
            detail={"code": "quality_failure_not_overridable"},
        )
    current_hash = segments_hash(job.segments_json or [])
    quality["segments_hash"] = current_hash
    quality["evaluated_revision"] = current_revision
    fingerprint = quality_fingerprint(
        quality, revision=current_revision, content_hash=current_hash,
    )
    quality["quality_fingerprint"] = fingerprint
    previous_ack = quality.get("acknowledgement") or {}
    if (
        previous_ack.get("quality_fingerprint") == fingerprint
        and int(previous_ack.get("revision", -1)) == current_revision
        and set(previous_ack.get("confirmed_window_ids") or []) == set(confirmed_ids)
    ):
        return {
            "ok": True, "revision": current_revision,
            "segments_hash": current_hash, "idempotent": True,
        }
    quality["acknowledgement"] = {
        "revision": current_revision,
        "segments_hash": current_hash,
        "policy_version": quality.get("policy_version"),
        "confirmed_window_ids": confirmed_ids,
        "quality_fingerprint": fingerprint,
        "user_id": current_user["id"],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    job.transcription_quality = quality
    db.add(AuditLog(
        user_id=current_user["id"], action="lyrics.quality_acknowledged",
        detail={
            "job_id": job_id, "revision": current_revision,
            "segments_hash": current_hash,
            "policy_version": quality.get("policy_version"),
            "score": quality.get("score"),
            "reason_codes": [
                item.get("code") for item in (quality.get("reasons") or [])
                if isinstance(item, dict)
            ],
        },
    ))
    db.commit()
    return {"ok": True, "revision": current_revision, "segments_hash": current_hash}


# Mismos estados en los que el LyricsEditor está operativamente montado
# (ver _SAVE_SEGMENTS_ALLOWED en save_segments): si el operador puede
# corregir texto ahí, puede pedir el re-anclado ahí.
_REANCHOR_ALLOWED = (
    "transcribed_pending", "transcribed", "pending_review", "rejected", "editing", "done",
)


class ReanchorSegmentsRequest(BaseModel):
    base_revision: int | None = Field(default=None, ge=0)


@app.post("/jobs/{job_id}/reanchor")
@limiter.limit("6/minute")
async def reanchor_segments(
    request: Request,
    job_id: str,
    body: ReanchorSegmentsRequest = Body(default_factory=ReanchorSegmentsRequest),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Versión B, parte 2 (2026-07-15): re-anclar el timing DESPUÉS de que
    el operador corrigió el TEXTO en el editor.

    Toma los segments actuales del job (`segments_json`, ya persistidos por
    el autosave de /save-segments), usa su texto como letra ancla y corre la
    misma `_maybe_anchor_align` del flujo de subida (motor CTC, paridad
    Rotor). Reglas:

    - Auth y status gate idénticos a /save-segments (owner + tenant, editor
      montado).
    - Gate por ANCHOR_LYRICS_ENABLED (mismo flag que el anclado en upload).
    - Líneas con `locked: true` (timing arrastrado a mano por el operador)
      NO se pisan — el ancla incluye su texto para que la alineación sea
      monotónica, pero su timing persiste tal cual.
    - Decline del motor / flag off a mitad de camino → 200 {ok: false} y
      los segments quedan intactos (mismo contrato "nunca rompe" del helper).
    - En éxito persiste el timing re-anclado en segments_json y devuelve
      los segments mergeados para que el editor se refresque sin re-fetch.
    """
    from jobs import get_job_model, touch_user_activity

    job = get_job_model(db, job_id)
    is_platform_admin = current_user.get("role") == "admin"
    if (not job
            or (not is_platform_admin
                and (job.user_id != current_user["id"]
                     or job.tenant_id != current_user["tenant_id"]))):
        raise HTTPException(status_code=404, detail="Job not found.")
    _audit_cross_tenant_access(db, current_user, job, "reanchor")
    if not _anchor_lyrics_enabled():
        # Flag off → el server no tiene la Versión B habilitada. 409 (no
        # 404) para no confundir con "job inexistente"; el frontend ni
        # muestra el botón sin features.anchor_lyrics.
        raise HTTPException(
            status_code=409,
            detail="El re-anclado no está habilitado en este servidor.",
        )
    if job.status not in _REANCHOR_ALLOWED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"reanchor requires status in {_REANCHOR_ALLOWED} "
                f"(current: {job.status})"
            ),
        )

    initial_revision = int(getattr(job, "segments_revision", 0) or 0)
    if body.base_revision is None and initial_revision != 0:
        return JSONResponse(
            status_code=428,
            content={"code": "client_upgrade_required", "current_revision": initial_revision},
        )
    if body.base_revision is not None and body.base_revision != initial_revision:
        from ops_metrics import increment
        increment("segments_revision_conflict")
        return JSONResponse(
            status_code=409,
            content={
                "code": "stale_revision",
                "current_revision": initial_revision,
                "updated_at": (
                    job.last_user_activity_at.isoformat()
                    if job.last_user_activity_at else None
                ),
            },
        )

    prev_segs = [dict(s) for s in (job.segments_json or [])
                 if isinstance(s, dict)]
    anchor_lines = [(s.get("text") or "").strip() for s in prev_segs]
    n_lines = sum(1 for _t in anchor_lines if _t)
    if n_lines < 3:
        # Mismo umbral que _maybe_anchor_align — con <3 líneas el motor
        # declina siempre; 422 acá da un mensaje accionable en vez de un
        # {ok: false} opaco.
        raise HTTPException(
            status_code=422,
            detail="Se necesitan al menos 3 líneas con texto para re-sincronizar.",
        )

    # SNAPSHOT + release (mismo patrón que /transcribe-uploaded, incidente
    # agus77 06/07): la descarga de R2 + el CTC pueden tardar minutos y no
    # podemos tener la sesión checked-out todo ese tiempo.
    _r2_key = job.input_r2_key
    _row_filename = job.filename or ""
    db.close()

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    audio_path = os.path.join(job_dir, _row_filename) if _row_filename else ""
    if not audio_path or (not os.path.exists(audio_path) and not _r2_key):
        raise HTTPException(
            status_code=409,
            detail="El audio original ya no está disponible para este job.",
        )
    if not os.path.exists(audio_path):
        import asyncio as _asyncio
        _loop = _asyncio.get_event_loop()
        for _attempt in range(3):
            # boto3 es síncrono — executor para no bloquear el event loop
            # (mismo patrón que /transcribe-uploaded).
            _ok = await _loop.run_in_executor(
                None, storage.download_object, _r2_key, audio_path,
            )
            if _ok:
                break
            if _attempt < 2:
                await _asyncio.sleep(0.5 * (2 ** _attempt))
        else:
            raise HTTPException(
                status_code=502,
                detail="No pudimos leer el audio original. Reintentá en unos segundos.",
            )

    anchor_text = "\n".join(_t for _t in anchor_lines if _t)
    out = await _maybe_anchor_align(
        {"segments": prev_segs}, audio_path, job_id, anchor_text,
    )
    anchored = out.get("segments") if isinstance(out, dict) else None
    if (not isinstance(out, dict)
            or out.get("timing_source") != "anchor_ctc"
            or not isinstance(anchored, list)
            or len(anchored) != n_lines):
        # Decline seguro (flag/engine/mismatch de líneas) — los segments
        # del operador quedan intactos, igual que la Versión A en upload.
        logger.info("[REANCHOR] declined job=%s (n_lines=%d)", job_id, n_lines)
        return {
            "ok": False,
            "reason": "declined",
            "job_id": job_id,
            "count": len(prev_segs),
            "review_count": 0,
            "locked_kept": 0,
            "revision": initial_revision,
        }

    # Merge: los segs re-anclados corresponden 1:1 (en orden) a los segs
    # previos con texto no vacío. Se preservan las keys extra del original
    # (_id, pos/scale/rot, estilo) y el timing de las líneas `locked`.
    merged = []
    review_count = 0
    locked_kept = 0
    _ai = 0
    for seg, _text in zip(prev_segs, anchor_lines):
        if not _text:
            merged.append(seg)
            continue
        new_seg = anchored[_ai]
        _ai += 1
        if seg.get("locked"):
            locked_kept += 1
            merged.append(seg)
            continue
        m = dict(seg)
        m["start"] = new_seg.get("start", seg.get("start"))
        m["end"] = new_seg.get("end", seg.get("end"))
        if new_seg.get("words") is not None:
            m["words"] = new_seg["words"]
        if new_seg.get("review"):
            m["review"] = True
            review_count += 1
        else:
            m.pop("review", None)
        merged.append(m)
    # Mismo contrato de orden monotónico que /save-segments.
    merged = sorted(merged, key=lambda s: float(s.get("start", 0) or 0))

    # Persistir con sesión corta (la del request se soltó antes del I/O).
    from database import SessionLocal as _SL
    _db2 = _SL()
    persisted_revision = initial_revision
    try:
        row = (
            _db2.query(Job)
            .filter(Job.job_id == job_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        current_revision = int(getattr(row, "segments_revision", 0) or 0)
        if body.base_revision is None and current_revision != 0:
            return JSONResponse(
                status_code=428,
                content={"code": "client_upgrade_required", "current_revision": current_revision},
            )
        if body.base_revision is not None and current_revision != body.base_revision:
            from ops_metrics import increment
            increment("segments_revision_conflict")
            return JSONResponse(
                status_code=409,
                content={
                    "code": "stale_revision",
                    "current_revision": current_revision,
                    "updated_at": (
                        row.last_user_activity_at.isoformat()
                        if row.last_user_activity_at else None
                    ),
                },
            )
        row.segments_json = merged
        row.segments_revision = (
            current_revision + 1 if body.base_revision is not None else current_revision
        )
        persisted_revision = int(row.segments_revision or 0)
        touch_user_activity(_db2, row)
        try:
            from database import AuditLog
            _db2.add(AuditLog(
                user_id=current_user["id"],
                action="lyrics.reanchor",
                detail={
                    "job_id": job_id,
                    "n_lines": len(merged),
                    "review_count": review_count,
                    "locked_kept": locked_kept,
                    "base_revision": current_revision,
                    "revision": int(row.segments_revision or 0),
                },
            ))
        except Exception as e:  # noqa: BLE001 — audit best-effort
            logger.warning("[REANCHOR] audit log failed: %s", e)
        # Audit 2026-08-13: bridge into editor_documents, same as
        # /save-segments (main.py sync_legacy_snapshot call) — this
        # endpoint used to write job.segments_json directly and commit
        # without it, which is the same divergence-then-stomp bug fixed on
        # /edit above: the next GET /editor would see job_revision >
        # document_revision and reconcile by overwriting whatever was in
        # the durable editor document with this re-anchored snapshot,
        # silently discarding any newer edit made in the editor meanwhile.
        #
        # CRITICAL ordering (regression found in prod 2026-08-13, same day
        # this bridge shipped): SessionLocal is created with autoflush=False
        # (database.py), and get_or_create_document re-queries the Job with
        # .populate_existing(), which OVERWRITES in-memory attributes from
        # the database row. Without an explicit flush first, the pending
        # `row.segments_json = merged` assignment above is silently
        # discarded before it ever reaches the DB — the endpoint then burns
        # 40-130s of CTC compute and persists nothing (observed live: rev
        # 156 and 157 byte-identical while the worker logged "[CTC] retimed
        # 50 lines"). Flush pins the new timings into the transaction so the
        # refresh below reads them back instead of clobbering them, and we
        # pass the locally-computed `merged`/`persisted_revision` rather
        # than re-reading through the refreshed ORM object.
        _db2.flush()
        try:
            _reanchor_document = get_or_create_document(
                _db2, job_id, row.tenant_id, merged,
            )
            sync_legacy_snapshot(
                _db2, _reanchor_document, current_user["id"],
                merged, persisted_revision,
            )
        except (LookupError, ValueError, RuntimeError) as exc:
            _db2.rollback()
            logger.warning("[REANCHOR] editor bridge rejected job=%s: %s", job_id, exc)
            raise HTTPException(
                status_code=409,
                detail={"code": "editor_state_conflict", "detail": str(exc)},
            ) from exc
        _db2.commit()
    finally:
        _db2.close()

    logger.info("[REANCHOR] ok job=%s lines=%d review=%d locked_kept=%d",
                job_id, len(merged), review_count, locked_kept)
    return {
        "ok": True,
        "job_id": job_id,
        "count": len(merged),
        "review_count": review_count,
        "locked_kept": locked_kept,
        "segments": merged,
        "revision": persisted_revision,
    }


@app.post("/edit/{job_id}/custom-background")
@limiter.limit("30/minute")
async def upload_edit_custom_background(
    request: Request,
    job_id: str,
    background_file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sube un fondo custom para un job en pending_review y devuelve su R2 key.

    Paso multipart previo al POST /edit/{job_id} con edit_type="custom".
    El body de /edit es JSON y no puede llevar bytes, así que el browser
    primero sube el archivo acá (mismo mecanismo que create-time:
    _save_custom_background valida extensión + magic-bytes + tamaño y lo
    guarda en inputs/{tenant}/{job}/bg_custom.*), recibe la key y la manda
    en custom_background_r2_key. NO toca bg_r2_key_cached (persist_cache=
    False): ese fondo recién pasa a ser el durable cuando el edit
    re-renderiza y valida.

    Restaura la opción "Subir el mío" que #970 ocultó en el wizard de
    edición porque el backend no la soportaba.
    """
    from database import Job as JobModel

    _q = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        _q = _q.filter(JobModel.tenant_id == current_user["tenant_id"])
    job = _q.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _audit_cross_tenant_access(db, current_user, job, "edit")
    if job.status != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=(
                "Custom background upload requires the job to be in "
                f"pending_review (current: {job.status})"
            ),
        )
    # Mismo guard que background/background_library: un fondo único pisaría
    # el timeline multi-escena cacheado (ver request_edit / incidente
    # 2026-07-01).
    _sp = job.scene_plan if isinstance(job.scene_plan, dict) else None
    if _sp and _sp.get("scenes"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Este video usa Escenas: regenerá la escena desde el "
                "filmstrip en vez de subir un fondo único."
            ),
        )
    if not storage.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Los fondos custom requieren object storage (R2).",
        )

    _job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(_job_dir, exist_ok=True)
    # tenant del JOB (no del caller): un admin de plataforma editando el
    # job de otro tenant debe dejar el archivo bajo el namespace del dueño,
    # para que la key valide contra inputs/{job.tenant_id}/{job}/ en /edit.
    _bg_path, _bg_r2_key = _save_custom_background(
        background_file, _job_dir, job_id, job.tenant_id, persist_cache=False,
    )
    if not _bg_r2_key:
        raise HTTPException(
            status_code=503,
            detail="No se pudo subir el fondo custom a storage.",
        )
    logger.info("[EDIT] custom bg subido job=%s key=%s por user=%s",
                job_id, _bg_r2_key, current_user["id"])
    return {
        "ok": True,
        "job_id": job_id,
        "bg_r2_key": _bg_r2_key,
        "filename": _safe_basename(background_file.filename or ""),
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

    # Fast-path para pestañas/clientes que reenvían el CTA mientras el primer
    # edit ya está corriendo. El SELECT MVCC no espera el row-lock corto de los
    # updates de progreso del worker, a diferencia del FOR UPDATE de abajo:
    # en el incidente 83f95d0e2679 dos duplicados tardaron 35 s y 120 s para
    # recién entonces devolver un 400 genérico. El gate se repite después del
    # lock para cubrir la carrera entre este probe y el commit del primer POST.
    _probe_q = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        _probe_q = _probe_q.filter(JobModel.tenant_id == current_user["tenant_id"])
    _probe = _probe_q.first()
    if not _probe:
        raise HTTPException(status_code=404, detail="Job not found")
    if _probe.status == "editing":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "edit_in_progress",
                "message": "An edit is already being rendered for this video.",
                "current_step": _probe.current_step,
                "progress": _probe.progress,
            },
        )

    # with_for_update() toma row-level lock en Postgres para serializar
    # el read-validate-write de edit_count. Sin esto, dos POST /edit del
    # mismo job en rápida sucesión leen el mismo edit_count, ambos pasan
    # el check < _MAX_EDITS, y ambos incrementan → user excede el límite
    # de 3 edits y la app cobra Veo extra (~$0.90 por background regen).
    # No-op en SQLite (igual que _lock_user_for_quota); lock real en
    # Postgres. Se libera con db.commit() al final del flow.
    # Cross-tenant para admins de plataforma: mismo contrato que _job_scope
    # (reads) — un admin re-renderiza el fix de cualquier cliente. Sin esto
    # el /edit de un tenant ajeno daba 404 aun para el super admin. Auditado
    # con commit=False para no soltar el row-lock de quota antes del commit
    # final del flow.
    _edit_q = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        _edit_q = _edit_q.filter(JobModel.tenant_id == current_user["tenant_id"])
    job = _edit_q.with_for_update().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _delivery_qc_actions: list[dict] = []
    if body.delivery_qc_action_ids:
        report = job.delivery_qc if isinstance(job.delivery_qc, dict) else {}
        if report.get("status") != "COMPLETE" or int(report.get("segments_revision") or -1) != int(job.segments_revision or 0):
            raise HTTPException(status_code=409, detail="delivery_qc_report_stale")
        indexed = {
            str(row.get("action_id")): row
            for row in ((report.get("repairs") or {}).get("actions") or [])
            if isinstance(row, dict)
        }
        missing = [value for value in body.delivery_qc_action_ids if value not in indexed]
        if missing:
            raise HTTPException(status_code=400, detail={"code": "delivery_qc_action_unknown", "action_ids": missing})
        _delivery_qc_actions = [indexed[value] for value in body.delivery_qc_action_ids]
        if any(row.get("status") != "APPLIED" for row in _delivery_qc_actions):
            raise HTTPException(status_code=400, detail="delivery_qc_action_not_safe")
        allowed_domain = {"lyrics": {"text", "timing"}, "metadata": {"metadata"}}.get(body.edit_type, set())
        if any(row.get("domain") not in allowed_domain for row in _delivery_qc_actions):
            raise HTTPException(status_code=400, detail="delivery_qc_action_wrong_edit_type")
    _audit_cross_tenant_access(db, current_user, job, "edit", commit=False)
    if job.status == "editing":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "edit_in_progress",
                "message": "An edit is already being rendered for this video.",
                "current_step": job.current_step,
                "progress": job.progress,
            },
        )
    valid_edit_types = ("typography", "background", "background_library", "lyrics", "metadata", "custom")
    if body.edit_type not in valid_edit_types:
        raise HTTPException(
            status_code=400,
            detail=f"edit_type must be one of {valid_edit_types}",
        )
    if body.edit_type == "background_library" and not body.background_id:
        raise HTTPException(
            status_code=400,
            detail="background_library edit requires 'background_id'.",
        )
    if body.edit_type == "custom" and not body.custom_background_r2_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "custom edit requires 'custom_background_r2_key' "
                "(subí el archivo con POST /edit/{job_id}/custom-background primero)."
            ),
        )
    # Escenas (incidente 2026-07-01, job 53b9513225b1): el edit "background"
    # es del mundo fondo-único — para un job multi-escena generaba UN clip
    # Veo de 8 s, PISABA bg_r2_key_cached (que era el timeline completo) y
    # re-renderizaba video+short loopeando esa única escena. El camino
    # correcto para estos jobs es la regeneración por escena del filmstrip
    # (edit_type="scene" vía /edit-scene, no consume cupo de edición).
    # background_library comparte el guard: un asset único también pisaría
    # el timeline multi-escena cacheado. custom (fondo subido) idem.
    if body.edit_type in ("background", "background_library", "custom"):
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
    # background_library tampoco consume slot (mismo mecanismo que metadata):
    # el cap de 3 existe para acotar gasto Veo (~$0.90/regen); el swap a un
    # asset curado cuesta $0 de IA y es justamente la SALIDA de emergencia
    # para quien ya quemó sus regens sin converger — si consumiera slot,
    # seguiría atrapado. AuditLog registra igual (edit_type en detail).
    _library_swap = body.edit_type == "background_library"
    # Fondo custom SIN animar = swap $0 (misma clase que la biblioteca):
    # no consume slot, es una salida de emergencia para quien ya quemó sus
    # regens. Custom CON "Animar con AI" sí pasa por Veo (~$0.50) → consume
    # slot como un background regen normal.
    _custom_as_is = body.edit_type == "custom" and not body.animate_image
    _no_slot = _metadata_only or _library_swap or _custom_as_is
    if (not _is_admin and not _no_slot
            and current_edit_count >= _MAX_EDITS):
        raise HTTPException(
            status_code=400,
            detail=f"Maximum edit limit ({_MAX_EDITS}) reached. Please approve or reject.",
        )

    # Legacy library jobs persisted only render_params.background_id. Recover
    # the durable R2 key after validating that the asset still exists and is
    # visible to this tenant; never trust the integer from JSON by itself.
    if body.edit_type in ("typography", "lyrics", "metadata") and not job.bg_r2_key_cached:
        _legacy_background_id = (job.render_params or {}).get("background_id")
        if _legacy_background_id is not None:
            _legacy_asset = (
                db.query(BackgroundAsset)
                .filter(
                    BackgroundAsset.id == _legacy_background_id,
                    BackgroundAsset.is_active == True,  # noqa: E712
                )
                .first()
            )
            if (
                _legacy_asset
                and _user_can_use_asset(_legacy_asset, current_user)
                and _background_asset_is_available(_legacy_asset)
                and (_legacy_asset.filename or "").startswith("library/")
            ):
                job.bg_r2_key_cached = _legacy_asset.filename

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

    # Editor 2.0 approvals resolve an exact durable snapshot under the Job
    # lock. Browser JSON is ignored whenever a revision/version selector is
    # present, and a remote save between autosave and approval fails closed.
    _approved_editor_version = None
    if body.editor_revision is not None or body.editor_version_id:
        if not current_user.get("features", {}).get("editor_v2"):
            raise HTTPException(status_code=404, detail="Job not found.")
        try:
            _editor_document, _approved_editor_version = approve_document(
                db, job, current_user["id"],
                editor_revision=body.editor_revision,
                editor_version_id=body.editor_version_id,
            )
        except LookupError:
            raise HTTPException(status_code=409, detail="editor_version_not_found") from None
        except RuntimeError:
            _current_document = get_or_create_document(
                db, job_id, job.tenant_id, job.segments_json or [],
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "editor_revision_conflict",
                    "server_revision": _current_document.revision,
                    "server_segments": _current_document.current_segments,
                },
            ) from None
        body.segments = _approved_editor_version.segments
        body.base_revision = _approved_editor_version.revision

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
        try:
            normalized_segments = normalize_segments(body.segments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        _pre_edit_revision = int(getattr(job, "segments_revision", 0) or 0)
        if body.base_revision is None and _pre_edit_revision > 0:
            return JSONResponse(
                status_code=428,
                content={
                    "code": "client_upgrade_required",
                    "current_revision": _pre_edit_revision,
                },
            )
        if _approved_editor_version is None:
            # Audit 2026-08-13: this used to write job.segments_json /
            # job.segments_revision directly, checked only against
            # job.segments_revision. That let editor_documents (the
            # LyricsEditor's durable source of truth) fall out of sync with
            # the job row — any writer here (including a background-only
            # edit that merely carries along whatever segments the wizard
            # had cached, per editSubmission.js bundling all pending
            # buckets into one POST) could advance job.segments_revision
            # without editor_documents ever knowing. The next GET /editor
            # then saw job_revision > document_revision and reconciled by
            # blindly overwriting editor_documents.current_segments with
            # the job's segments (editor.py get_or_create_document) —
            # silently stomping real edits with a stale wizard snapshot.
            # Confirmed root cause of a real incident (UMG Chile,
            # 2026-08-13): "edité la letra y luego me borró partes en un
            # segundo cambio de fondo".
            #
            # Fix: route through the same durable save_document() path
            # PATCH /editor and /save-segments already use. It re-fetches
            # job + document under a row lock and checks base_revision
            # against document.revision (the actual source of truth, not
            # a separately-tracked counter that can drift), so both rows
            # move together atomically — no more divergence, no more
            # stale-snapshot overwrite on the next reconcile.
            _edit_document = get_or_create_document(
                db, job_id, job.tenant_id, job.segments_json or [],
            )
            _pre_edit_document_segments = _edit_document.current_segments
            _pre_edit_document_revision = _edit_document.revision
            try:
                _edit_document, _edit_version, _edit_applied = save_document(
                    db, job, _edit_document, current_user["id"],
                    base_revision=(
                        body.base_revision if body.base_revision is not None
                        else _edit_document.revision
                    ),
                    segments=normalized_segments,
                    reason="manual",
                )
                if _edit_applied:
                    from correction_learning import invalidate_job_observations
                    invalidate_job_observations(
                        db, job_id, "later_editor_revision",
                    )
            except RuntimeError:
                from ops_metrics import increment
                increment("segments_revision_conflict")
                _conflict_document = get_or_create_document(
                    db, job_id, job.tenant_id, job.segments_json or [],
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "stale_revision",
                        "detail": "editor_revision_conflict",
                        "current_revision": _conflict_document.revision,
                        "server_revision": _conflict_document.revision,
                        "server_segments": _conflict_document.current_segments,
                        "updated_at": (
                            job.last_user_activity_at.isoformat()
                            if job.last_user_activity_at else None
                        ),
                    },
                )

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
    if body.edit_type == "background" and body.background_hint is not None:
        # Operator's free-form description of what they want the new
        # background to convey. Forwarded to Gemini's user_content as a
        # high-priority override block so it pisa los defaults that
        # produced the rejected background.
        #
        # Contrato None-aware (2026-07-24): None = mantener el hint
        # persistido; "" = CLEAR explícito (persiste "" en render_params);
        # valor = set + persist. Antes se salteaba el body vacío, así que
        # borrar el textarea no borraba el hint persistido y el prompt
        # viejo "revivía" en el siguiente regen — lo que hacía inútil el
        # cambio de modo de escena cableado en #979 (resolve_creative_mode
        # ignora match_lyrics si hay operator_prompt no vacío, y
        # run_edit_pipeline revive el persistido vía `background_hint or
        # _persisted_operator_prompt`). El "" persistido no revive: ambos
        # lados coercen con `or None`.
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
        # NORMALIZADO al persistir (misma nota que en pipeline al escribir
        # render_params): la invariante es que render_params.movement_style
        # SIEMPRE sea un código canónico o "". El pipeline normaliza igual al
        # renderizar, así que esto no cambia ningún render — deja de guardar
        # algo que después cada lector tiene que interpretar por su cuenta.
        _mv = _normalize_movement_style((body.movement_style or "").strip())
        edit_params["movement_style"] = _mv
        _rp_mv = dict(job.render_params or {})
        _rp_mv["movement_style"] = _mv
        job.render_params = _rp_mv
    # Scene axes (genre / concept / match_lyrics) — editable in the wizard for
    # a regen. None = keep persisted (only sent when CHANGED). Mirror the
    # movement_style block: forward through edit_params AND persist durably so
    # a later "Regenerar fondo" / reaped retry reproduces the operator's pick.
    # The pipeline reads all three from merged render_params (pipeline.py).
    if body.edit_type == "background" and body.genre is not None:
        _g = (body.genre or "").strip()
        edit_params["genre"] = _g
        _rp_g = dict(job.render_params or {})
        _rp_g["genre"] = _g
        job.render_params = _rp_g
    if body.edit_type == "background" and body.concept is not None:
        _c = (body.concept or "").strip()
        edit_params["concept"] = _c
        _rp_c = dict(job.render_params or {})
        _rp_c["concept"] = _c
        job.render_params = _rp_c
    if body.edit_type == "background" and body.match_lyrics is not None:
        _ml = bool(body.match_lyrics)
        edit_params["match_lyrics"] = _ml
        _rp_ml = dict(job.render_params or {})
        _rp_ml["match_lyrics"] = _ml
        job.render_params = _rp_ml
    if body.edit_type == "background" and body.bg_verbatim is not None:
        # "Usar mi prompt tal cual" — send background_hint straight to Veo.
        # None = keep persisted (BUG-5): the unified wizard only sends this
        # field when the toggle CHANGED, so writing unconditionally flipped a
        # persisted True→False on any background edit that didn't touch it.
        # Now symmetric with the background_mode / movement_style (None-means-
        # keep) blocks: only persist when the caller actually sent a value.
        _bv = bool(body.bg_verbatim)
        edit_params["bg_verbatim"] = _bv
        _rp_v = dict(job.render_params or {})
        _rp_v["bg_verbatim"] = _bv
        job.render_params = _rp_v
    if body.edit_type == "background":
        # Persist the CURRENT mutually-exclusive choice. The worker resolves
        # policy from durable render_params; leaving a stale bypass there made
        # a later safe edit silently behave as unrestricted.
        _rp_policy = _merge_content_validation_choice(
            job.render_params,
            bypass=body.bypass_content_validation,
            force=body.force_content_validation,
        )
        if _rp_policy.get("bypass_content_validation"):
            edit_params["bypass_content_validation"] = True
        else:
            edit_params["force_content_validation"] = True
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

    if _library_swap:
        # Resolver el asset ANTES de flipear el status: _resolve_library_
        # background trae gratis el tenant-gate (_user_can_use_asset, 404
        # no-revelador para assets ajenos) y el row de AssetUsage para el
        # audit. Cualquier 404/400 acá deja el job intacto en
        # pending_review. Solo modo as_is en v1 (variation re-entra en
        # costo Veo — diferido a propósito).
        _lib_job_dir = os.path.join(OUTPUTS_DIR, job_id)
        os.makedirs(_lib_job_dir, exist_ok=True)
        _lib_bg_path, _lib_bg_r2_key, _v1, _v2, _lib_asset_id = _resolve_library_background(
            body.background_id, "as_is", current_user, db, _lib_job_dir, job_id,
        )
        edit_params["library_bg"] = {
            "bg_path": _lib_bg_path,
            "bg_r2_key": _lib_bg_r2_key,
            "asset_id": _lib_asset_id,
        }

    if body.edit_type == "custom":
        # SEGURIDAD: la key llega en el body del cliente. Validar que
        # pertenece a ESTE job/tenant (prefijo inputs/{tenant}/{job}/) antes
        # de que el worker la baje — sin esto un cliente podría apuntar a un
        # objeto de otro tenant y hornearlo en su propio video. El endpoint
        # de subida guarda bajo inputs/{job.tenant_id}/{job}/bg_custom.*.
        from storage import _safe_filename as _r2_safe
        _expected_prefix = f"inputs/{_r2_safe(job.tenant_id)}/{_r2_safe(job_id)}/"
        if not (body.custom_background_r2_key or "").startswith(_expected_prefix):
            raise HTTPException(
                status_code=400,
                detail="custom_background_r2_key no pertenece a este job.",
            )
        edit_params["custom_bg"] = {
            "bg_r2_key": body.custom_background_r2_key,
            "animate_image": bool(body.animate_image),
        }

    # PR C 2026-05-26: metadata edits do NOT bump edit_count (see
    # rationale at the edit-cap gate above). All other types still
    # consume a slot.
    new_edit_count = (
        current_edit_count if _no_slot
        else current_edit_count + 1
    )

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

    _pre_edit_status = job.status
    _pre_edit_completed_at = job.completed_at
    _pre_edit_editing_started_at = job.editing_started_at
    # A rejected/error job can still represent a retained historical delivery
    # when it was reopened after shipping. Clear only failed-attempt timestamps;
    # an edit of a previously delivered song must not move the denominator to
    # the new edit month.
    if not job_was_delivered(
        job.status, job.completed_at, job.editing_started_at,
    ):
        job.completed_at = None

    # Flip to editing immediately so the UI can show progress.
    if isinstance(job.delivery_qc, dict):
        from delivery_qc_runtime import mark_delivery_qc_stale
        _qc = dict(job.delivery_qc)
        accepted = set(body.delivery_qc_action_ids)
        if accepted:
            repairs = dict(_qc.get("repairs") or {})
            repairs["actions"] = [
                {**row, "operator_status": "ACCEPTED_PENDING_RERENDER"}
                if str(row.get("action_id")) in accepted else row
                for row in repairs.get("actions") or []
            ]
            _qc["repairs"] = repairs
        job.delivery_qc = mark_delivery_qc_stale(
            _qc, revision=int(job.segments_revision or 0), reason="edit_render_pending",
        )
    job.status = "editing"
    job.edit_count = new_edit_count
    # Both typography and lyrics edits jump straight into the video
    # compositing step (cached bg reused). Only background edit goes
    # back through Veo, which is the `background` step. Un fondo custom
    # ANIMADO también corre Veo (image-to-video); tal cual va directo a
    # video (sin costo IA).
    _custom_animates = body.edit_type == "custom" and bool(body.animate_image)
    job.current_step = (
        "background" if (body.edit_type == "background" or _custom_animates)
        else "video"
    )
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
            "base_revision": body.base_revision,
            "segments_revision": int(getattr(job, "segments_revision", 0) or 0),
            "force_conflict_overwrite": body.force_conflict_overwrite,
        },
    ))
    # Commit the publication intent in the same transaction as the Job and
    # EditorDocument mutation. An ambiguous Redis timeout must not rewind an
    # editor revision and re-open the ABA window for an old callback.
    from transactional_outbox import create_outbox_event
    _edit_outbox = create_outbox_event(
        db,
        job_id=job_id,
        event_type="edit.enqueue",
        dedupe_key=(
            f"edit:{job_id}:{int(getattr(job, 'segments_revision', 0) or 0)}:"
            f"{new_edit_count}:{body.edit_type}"
        ),
        payload={
            "edit_type": body.edit_type,
            "edit_params": edit_params,
            "plan": current_user.get("plan", "100"),
            "tenant_id": current_user.get("tenant_id", ""),
        },
    )
    for _qc_action in _delivery_qc_actions:
        db.add(ProductEvent(
            tenant_id=str(job.tenant_id), user_id=current_user["id"], job_id=job_id,
            name="delivery_qc_action_decision",
            properties={
                "decision": "accepted",
                "action_id": str(_qc_action.get("action_id") or ""),
                "domain": str(_qc_action.get("domain") or "unknown"),
                "code": str(_qc_action.get("code") or "unknown"),
                "confidence": float(_qc_action.get("confidence") or 0),
            },
        ))
    # HOTFIX F1 2026-05-27 (audit): the pre-edit capture moved UP to
    # before the in-memory mutation (search "_pre_edit_artist =" above).
    # The old capture here was a no-op because it read AFTER the
    # job.artist assignment.
    db.commit()
    from transactional_outbox import dispatch_outbox_event
    # Pass the already imported publisher explicitly. Besides keeping this
    # boundary injectable in tests, it makes the first delivery attempt use
    # the exact same queue adapter as the API process. Reconciliation still
    # resolves the adapter from ``queue_jobs`` independently.
    _outbox_delivery = dispatch_outbox_event(
        _edit_outbox.id,
        edit_publisher=enqueue_edit,
    )
    _queue_pending = _outbox_delivery.get("status") != "dispatched"
    if _queue_pending:
        logger.warning(
            "[EDIT-OUTBOX] publication pending job=%s event=%s status=%s",
            job_id, _edit_outbox.id, _outbox_delivery.get("status"),
        )

    if _approved_editor_version is not None:
        try:
            from queue_jobs import enqueue_correction_learning
            enqueue_correction_learning(job_id, _approved_editor_version.id)
        except Exception as exc:
            logger.warning(
                "[QUALITY-LEARNING] edit approval capture enqueue failed job=%s: %s",
                job_id, exc,
            )

    return {
        "ok": True,
        "job_id": job_id,
        "edit_type": body.edit_type,
        "edit_count": new_edit_count,
        "edits_remaining": max(0, _MAX_EDITS - new_edit_count),
        "edit_limit_exempt": _is_admin,
        "segments_revision": int(getattr(job, "segments_revision", 0) or 0),
        "approved_editor_version_id": (
            _approved_editor_version.id if _approved_editor_version is not None else None
        ),
        "queue_pending": _queue_pending,
        "outbox_event_id": _edit_outbox.id,
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
    _pre_regen_status = job.status
    _pre_regen_completed_at = job.completed_at
    _pre_regen_editing_started_at = job.editing_started_at
    _pre_regen_progress = job.progress
    _pre_regen_current_step = job.current_step
    # A rejection timestamp is not necessarily a failed-attempt timestamp: a
    # rejected job may have been delivered before it was reopened. Keep that
    # historical completion and clear only jobs that were never delivered.
    if not job_was_delivered(
        job.status, job.completed_at, job.editing_started_at,
    ):
        job.completed_at = None
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
        job.status = _pre_regen_status
        job.completed_at = _pre_regen_completed_at
        # el re-roll de escena no tocó edit_count → nada que revertir acá
        job.editing_started_at = _pre_regen_editing_started_at
        job.progress = _pre_regen_progress
        job.current_step = _pre_regen_current_step
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

    job_query = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        job_query = job_query.filter(
            JobModel.tenant_id == current_user["tenant_id"]
        )
    job = job_query.first()
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
    _audit_cross_tenant_access(
        db, current_user, job, "enable_prores", commit=False,
    )
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
    # Explicit non-Universal people opt-in. The central pipeline still
    # requires a matching positive prompt and never permits a Universal
    # account to bypass validation.
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

    # Cross-tenant para admins de plataforma: mismo contrato que _job_scope
    # / POST /edit — un admin re-encola el fix de cualquier cliente (caso
    # UMG Chile: soporte necesita reintentar un job del cliente sin pedirle
    # que haga el click). Sin esto, un job de otro tenant daba 404 aun para
    # el super admin. Auditado vía _audit_cross_tenant_access más abajo.
    _retry_q = db.query(JobModel).filter(JobModel.job_id == job_id)
    if current_user.get("role") != "admin":
        _retry_q = _retry_q.filter(JobModel.tenant_id == current_user["tenant_id"])
    job = _retry_q.first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _audit_cross_tenant_access(db, current_user, job, "retry")
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

    # Persist one mutually-exclusive choice; missing fields fail closed and
    # stale choices from earlier attempts are removed.
    job.render_params = _merge_content_validation_choice(
        job.render_params,
        bypass=bool(body and body.bypass_content_validation),
        force=bool(body and body.force_content_validation),
    )

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
              "enable_scenes",
              # Art track: heredable — sin esto un retry de un art track se
              # re-renderiza como lyric video vacío y re-corre Whisper.
              # Persistido por pipeline/endpoint en render_params.
              "art_track",
              # Línea legal del art track (℗/© sello), persistida junto al
              # marker para que el retry la re-dibuje igual.
              "label_line"):
        if k in _retry_render_params and _retry_render_params[k] not in (None, ""):
            retry_pipeline_kwargs[k] = _retry_render_params[k]

    # Audit A5: re-gatear enable_scenes con el acceso ACTUAL del usuario, igual
    # que /generate y /upload. Un tenant al que se le sacó el acceso (o se cayó
    # de SCENES_ENABLED_TENANTS) no debe seguir generando multi-escena —y su
    # costo Veo extra— al reintentar un job viejo.
    if retry_pipeline_kwargs.get("enable_scenes"):
        retry_pipeline_kwargs["enable_scenes"] = has_scenes_access(current_user)

    # Art Track: mismo re-gate. Un art track NO se puede degradar a lyric
    # (se re-rendería vacío, sin letra), así que si el usuario perdió el
    # acceso a la feature cortamos el retry en vez de convertirlo.
    if retry_pipeline_kwargs.get("art_track") and not has_art_track_access(current_user):
        raise HTTPException(
            status_code=403,
            detail="Art Track no está habilitado para tu cuenta.",
        )

    _commit_pipeline_publication(
        db, job, "retry",
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


@app.post("/jobs/{job_id}/edit-art-track")
async def edit_art_track(
    job_id: str,
    background_file: UploadFile = File(None),
    effect: str = Form(""),
    song_title: str = Form(None),
    artist: str = Form(None),
    label_line: str = Form("", max_length=120),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Editar un Art Track ya generado sin rehacerlo desde cero: cambiar la
    portada (cover), el efecto de partículas, el título/artista y la línea
    legal ℗/©, y re-renderizar.

    Los art tracks son un composite ffmpeg determinístico y barato (~14-42s),
    así que la edición es un re-render completo vía `run_pipeline(art_track=True)`
    —el mismo camino que /retry— reusando el audio de R2 (input_r2_key) y la
    portada cacheada (bg_r2_key_cached) salvo que se suba una nueva. NO consume
    cuota ni crédito (igual que /retry): la edición es gratis.

    Multipart (no JSON) para poder recibir una portada nueva opcional. Todos
    los campos son opcionales; el panel del front manda el estado completo
    pre-cargado, así que el valor recibido es autoritativo (un `effect`/
    `label_line` vacío = limpiar). La portada solo se reemplaza si viene un
    archivo nuevo; si no, se reusa la actual.
    """
    from database import Job as JobModel, AuditLog
    from jobs import merge_render_params

    job = (
        db.query(JobModel)
        .filter(JobModel.job_id == job_id)
        .filter(JobModel.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Solo art tracks. Un job de lyric video no debe caer acá (se re-rendería
    # sin letra). El marker vive en render_params.art_track.
    if not bool((job.render_params or {}).get("art_track")):
        raise HTTPException(
            status_code=400,
            detail="This endpoint only edits Art Track jobs.",
        )

    # Re-gate de la feature con el acceso ACTUAL del usuario (igual que
    # /generate y /retry): un tenant al que se le sacó el acceso no sigue
    # editando art tracks.
    if not has_art_track_access(current_user):
        raise HTTPException(
            status_code=403,
            detail="Art Track no está habilitado para tu cuenta.",
        )

    # Estados editables = los mismos que habilitan el panel de edición en el
    # front (JobDetail.canEditLyrics): revisión pendiente / listo / rechazado.
    # No se edita mientras se procesa o si falló (para eso está /retry).
    if job.status not in ("pending_review", "done", "rejected"):
        raise HTTPException(
            status_code=400,
            detail=f"Art track cannot be edited from status '{job.status}'.",
        )

    # El re-render necesita el audio original en R2 (no se re-sube).
    if not job.input_r2_key:
        raise HTTPException(
            status_code=422,
            detail="Source audio no longer available — please re-generate the video.",
        )
    try:
        import storage as _storage
        if _storage.is_enabled() and not _storage.object_exists(job.input_r2_key):
            raise HTTPException(
                status_code=422,
                detail=(
                    "El audio original ya no está en storage. "
                    "Volvé a generar el video."
                ),
            )
    except HTTPException:
        raise
    except Exception as _exc:
        logger.warning(
            "[ART-EDIT] R2 pre-check failed for %s key=%r — proceeding: %s",
            job_id, job.input_r2_key, _exc,
        )

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Portada: reemplazar solo si viene una nueva. _save_custom_background
    # valida (imagen), sube a R2 y persiste bg_r2_key_cached. Si no viene
    # archivo, reusamos el cover cacheado.
    new_bg_r2_key = None
    if background_file and background_file.filename:
        if not background_file.filename.lower().endswith((".jpg", ".jpeg", ".png")):
            raise HTTPException(
                status_code=400,
                detail="Art track cover must be an image (.jpg/.jpeg/.png).",
            )
        _bg_path, new_bg_r2_key = _save_custom_background(
            background_file, job_dir, job_id, current_user["tenant_id"],
        )
    bg_r2_key = new_bg_r2_key or job.bg_r2_key_cached
    if not bg_r2_key:
        raise HTTPException(
            status_code=422,
            detail="No cover image on file — please upload a cover.",
        )

    # Persistir los ejes editables en render_params (autoritativo: vacío =
    # limpiar). Así el re-render y cualquier /retry futuro los re-dibujan.
    effect_val = (effect or "").strip()
    label_val = (label_line or "").strip()
    merge_render_params(job_id, {
        "art_track": True,
        "effect": effect_val,
        "label_line": label_val,
    })

    # Título/artista viven en columnas; se actualizan solo si vinieron.
    if song_title is not None:
        job.song_title = song_title.strip()
    if artist is not None:
        job.artist = artist.strip()

    # Reset del row a estado de re-render limpio (mismo patrón que /retry),
    # para que los entregables viejos no queden pegados si el nuevo render
    # produce menos archivos.
    _previous_status = job.status
    job.status = "editing"
    job.current_step = "render"
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
    job.last_progress_at = datetime.now(timezone.utc)

    # Cancelar prewarms de ProRes de la corrida anterior (mismo hazard que
    # /retry: publicarían un .mov viejo tras limpiar s3_keys).
    try:
        from queue_jobs import cancel_rq_job
        for _ft in ("umg_master", "umg_short"):
            cancel_rq_job(f"prewarm:{job_id}:{_ft}")
    except Exception as _exc:
        logger.warning("edit_art_track: prewarm cancel skipped for %s: %s", job_id, _exc)

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.edit_art_track",
        detail={
            "job_id": job_id,
            "previous_status": _previous_status,
            "cover_replaced": new_bg_r2_key is not None,
            "effect": effect_val,
        },
    ))
    _commit_pipeline_publication(
        db, job, "art_track_edit",
        mp3_path=None,
        artist=job.artist,
        style=job.style or "oscuro",
        plan=current_user.get("plan", "100"),
        tenant_id=current_user.get("tenant_id", ""),
        delivery_profile=job.delivery_profile or "youtube",
        input_r2_key=job.input_r2_key,
        song_title=job.song_title or "",
        umg_spec=job.umg_spec or {},
        segments_override=job.segments_json if job.segments_json else None,
        bg_r2_key=bg_r2_key,
        art_track=True,
        effect=effect_val,
        label_line=label_val,
    )

    return {
        "ok": True,
        "status": "editing",
        "job_id": job_id,
        "cover_replaced": new_bg_r2_key is not None,
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

    Contrato espejado con /edit (2026-07-24, "Crear variante abre el wizard
    completo"): TODOS los ejes que el wizard de edición deja tocar viajan
    también acá, con la misma semántica None-aware:
        None  → heredar del padre (no pisa render_params)
        valor → override
        ""    → clear explícito para los campos de texto (mismo contrato
                que /edit para background_hint; ver request_edit).
    Sin esto el wizard mostraba controles editables que el backend tiraba a
    la basura — exactamente lo que el PR #977 ("honestidad") prohíbe.
    """
    # Mismo formato y max_length que EditJobRequest.background_hint —
    # va al user_content de Gemini con header [OPERATOR OVERRIDE].
    # 2000 chars (bumped 2026-05-18, ver EditJobRequest para rationale).
    background_hint: str | None = Field(default=None, max_length=2000)
    # Same central policy as edit/retry: only a non-Universal account with an
    # explicit people prompt can use this opt-in; Universal remains strict.
    bypass_content_validation: bool = Field(default=False)
    force_content_validation: bool = Field(default=False)
    # Override del concept del padre. 2000 chars igual que /generate.
    # Alimenta _get_unique_prompt() junto con genre/style/lyrics.
    concept: str | None = Field(default=None, max_length=2000)
    # Override del style preset (gradient palette + visual register).
    style: str | None = Field(default=None, max_length=50)
    # ── Ejes de escena (espejo exacto de EditJobRequest) ─────────────────
    # genre/concept orientan el vocabulario de la escena IA; match_lyrics =
    # "Inspirado en la letra" (True) vs "Auto/Mi prompt" (False).
    genre: str | None = Field(default=None, max_length=64)
    match_lyrics: bool | None = Field(default=None)
    # "Usar mi prompt tal cual": el hint va directo a Veo sin reescritura
    # de Gemini. None = heredar (mismo BUG-5 que cerró /edit).
    bg_verbatim: bool | None = Field(default=None)
    # Registro de cámara/movimiento. También decide el MOTOR aguas abajo
    # ("foto-parallax" → Imagen, resto → Veo, pipeline.py) — por eso NO hay
    # un campo "motor" separado en este body.
    movement_style: str | None = Field(default=None, max_length=64)
    # ── Capa FX + animaciones de letra (espejo de EditJobRequest) ────────
    effect: str | None = Field(default=None, max_length=32)
    lyrics_animation: str | None = Field(default=None, max_length=16)
    line_transition: str | None = Field(default=None, max_length=16)
    # ── Tipografía (espejo de EditJobRequest) ────────────────────────────
    font: str | None = Field(default=None, max_length=64)
    font_scale: float | None = None
    text_case: str | None = Field(default=None, max_length=16)
    text_contrast: str | None = Field(default=None, max_length=16)
    frame_format: str | None = Field(default=None, max_length=16)
    # Paleta custom (hex/nombres coma-separados) usada cuando style=="custom".
    custom_colors: str | None = Field(default=None, max_length=200)
    # ── Portada / title card (espejo de EditJobRequest) ──────────────────
    title_template: str | None = Field(default=None, max_length=16)
    title_size: float | None = None
    title_artist_font: str | None = Field(default=None, max_length=64)
    title_song_font: str | None = Field(default=None, max_length=64)
    title_song_break: str | None = Field(default=None, max_length=200)
    # ── Biblioteca de fondos ─────────────────────────────────────────────
    # Una variante ES un job nuevo, así que reusa el mismo resolver que
    # /generate y /upload (_resolve_library_background) — no el edit_type
    # "background_library" de /edit. Cuando viene background_id, ese camino
    # REEMPLAZA la generación IA (mismo orden de precedencia que /generate).
    # background_mode acá es el modo de la BIBLIOTECA (as_is | variation),
    # NO un motor Veo/Imagen: el motor lo deriva movement_style.
    background_id: int | None = Field(default=None)
    background_mode: str | None = Field(
        default=None,
        pattern="^(as_is|variation)$",
    )
    # 2026-05-29 — Variant cap policy: each plan includes 3 renders of
    # the same song (original + 2 variants). The 4th onward costs
    # VARIANT_OVERAGE_COST_USD passthrough (Veo background generation
    # fee). The endpoint returns 402 with `code: variant_overage_unconfirmed`
    # if the operator tries to create the 4th+ without setting this flag.
    # Re-submit with `acknowledge_variant_overage: true` to proceed and
    # accept the charge. The acknowledgement is logged via AuditLog so
    # month-close billing surfaces the line items per tenant.
    acknowledge_variant_overage: bool = Field(default=False)


# Campos de VariantJobRequest que pisan render_params del padre 1:1 (el
# nombre del campo del body ES la key de render_params). Explícito y a
# nivel módulo para que el contrato sea grepeable y testeable sin leer el
# handler — y para que agregar un control al wizard sea una sola línea acá
# en vez de un `if` suelto más.
#
# Excluidos a propósito:
#   - style              → columna de la DB (Job.style), no render_params.
#   - background_id/mode → resuelven la Biblioteca, no son render params.
#   - bypass/force_content_validation → los mergea
#     _merge_content_validation_choice (mutuamente excluyentes).
#   - acknowledge_variant_overage → flag de billing, no de render.
_VARIANT_OVERRIDABLE_FIELDS = (
    # Escena / fondo
    "background_hint",
    "concept",
    "genre",
    "match_lyrics",
    "bg_verbatim",
    "movement_style",
    # FX + animaciones de letra
    "effect",
    "lyrics_animation",
    "line_transition",
    # Tipografía
    "font",
    "font_scale",
    "text_case",
    "text_contrast",
    "frame_format",
    "custom_colors",
    # Portada / title card
    "title_template",
    "title_size",
    "title_artist_font",
    "title_song_font",
    "title_song_break",
)

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

    400 si padre no done. 402 si el plan está sin capacidad. 404 si el
    padre no existe o pertenece a otro tenant. Los admins de plataforma
    pueden crear la variante cross-tenant, igual que pueden abrir y editar
    el video; en ese caso el job nuevo queda en el tenant y bajo el owner
    del padre, no bajo la cuenta interna del admin.
    """
    import uuid
    from database import Job as JobModel, AuditLog

    # Mismo scope que GET /status y POST /edit: un admin de plataforma
    # puede operar sobre cualquier tenant; un usuario común sólo sobre el
    # suyo. Antes /status cargaba el padre cross-tenant y montaba todo el
    # wizard, pero este query volvía a exigir el tenant del admin y el
    # submit terminaba en un falso 404 "Parent job not found".
    _parent_q = db.query(JobModel).filter(JobModel.job_id == parent_job_id)
    if current_user.get("role") != "admin":
        _parent_q = _parent_q.filter(
            JobModel.tenant_id == current_user["tenant_id"]
        )
    parent = _parent_q.first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent job not found")

    _is_cross_tenant_admin = (
        current_user.get("role") == "admin"
        and parent.tenant_id != current_user.get("tenant_id")
    )
    # Un admin cross-tenant actúa EN NOMBRE del owner original: la variante
    # debe aparecer en el historial del cliente y consumir su plan/cupo.
    # Para usuarios comunes (incluidos teammates del mismo tenant) se
    # conserva el contrato previo: el creador actual es el owner del job.
    variant_tenant_id = parent.tenant_id
    variant_user_id = (
        parent.user_id if _is_cross_tenant_admin else current_user["id"]
    )
    billing_user = db.query(User).filter(User.id == variant_user_id).first()
    if billing_user is None:
        # Job.user_id tiene FK y esto no debería ocurrir; fallar explícito
        # evita crear un job sin dueño o facturarlo al admin por accidente.
        raise HTTPException(
            status_code=409,
            detail="No se pudo resolver el owner del video original.",
        )
    _audit_cross_tenant_access(
        db, current_user, parent, "create_variant", commit=False,
    )
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
    plan = billing_user.plan_id or "100"
    usage_info = get_plan_usage(
        db,
        variant_user_id,
        variant_tenant_id,
        plan,
        billing_group=billing_user.billing_group,
    )
    if usage_info["alert_100"] and plan == "free":
        raise HTTPException(
            status_code=429,
            detail="Free plan limit reached. Upgrade to continue.",
        )
    # Para planes pagos, allow_overage decide si se permite pasarse del cap.
    if usage_info.get("alert_100") and not billing_user.allow_overage:
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
        .filter(JobModel.tenant_id == variant_tenant_id)
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
                "tenant_id": variant_tenant_id,
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
    # Las variantes intercambian el fondo (Veo) del video; un art track no
    # tiene fondo generado, así que "variante de un art track" no aplica. Sin
    # este corte, la variante hereda art_track=True en render_params (se
    # etiqueta como art track en la UI) pero se re-rendería como lyric vacío —
    # y sería una vía sin gatear de la feature. Bloquear de plano.
    if parent_render_params.get("art_track"):
        raise HTTPException(
            status_code=400,
            detail="No se pueden crear variantes de un Art Track.",
        )
    new_render_params = dict(parent_render_params)
    # Contrato None-aware espejado con /edit: None = heredar (no tocamos la
    # key del padre), valor = override, "" = clear explícito (persiste ""
    # en render_params y NO revive el valor viejo — mismo comportamiento
    # que el guard de background_hint en request_edit).
    _overridden_fields = []
    for _field in _VARIANT_OVERRIDABLE_FIELDS:
        _value = getattr(body, _field, None)
        if _value is None:
            continue
        if isinstance(_value, str):
            _value = _value.strip()
        # Misma invariante que en /edit y en el create: movement_style se
        # persiste CANÓNICO, nunca crudo.
        if _field == "movement_style":
            _value = _normalize_movement_style(_value or "")
        new_render_params[_field] = _value
        _overridden_fields.append(_field)
    new_render_params = _merge_content_validation_choice(
        new_render_params,
        bypass=body.bypass_content_validation,
        force=body.force_content_validation,
    )

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
                variant_tenant_id, new_job_id, src_filename
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

    # Biblioteca de fondos. Una variante es un job NUEVO, así que reusa el
    # mismo resolver que /generate y /upload (no el edit_type
    # "background_library" de /edit, que parchea un job existente). Se
    # resuelve ANTES de insertar la row: _resolve_library_background trae
    # gratis el tenant-gate (404 no-revelador) y cualquier 404/400 acá
    # aborta sin dejar un job huérfano en "processing" que nadie encola.
    # AssetUsage.job_id no tiene FK, así que registrar el uso con el
    # job_id que estamos por crear es seguro.
    variant_bg_path = None
    variant_bg_r2_key = None
    variant_variation_source_path = None
    variant_variation_source_r2_key = None
    variant_variation_parent_id = None
    if body.background_id:
        _variant_job_dir = os.path.join(OUTPUTS_DIR, new_job_id)
        os.makedirs(_variant_job_dir, exist_ok=True)
        # El admin conserva su identidad como actor, pero el uso del asset
        # se registra contra el tenant destino, que es quien recibe el job.
        _variant_asset_user = {
            **current_user,
            "tenant_id": variant_tenant_id,
        }
        (
            variant_bg_path,
            variant_bg_r2_key,
            variant_variation_source_path,
            variant_variation_source_r2_key,
            variant_variation_parent_id,
        ) = _resolve_library_background(
            body.background_id,
            body.background_mode or "as_is",
            _variant_asset_user,
            db,
            _variant_job_dir,
            new_job_id,
        )

    new_job = JobModel(
        job_id=new_job_id,
        user_id=variant_user_id,
        tenant_id=variant_tenant_id,
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
            # Qué ejes pisó el operador en el wizard (vs heredar del padre).
            # Sin esto, un "por qué salió distinto al padre" post-mortem
            # obligaba a diffear render_params a mano entre las dos rows.
            "overridden_fields": list(_overridden_fields),
            "background_id": body.background_id,
            "background_mode": body.background_mode if body.background_id else None,
            "variant_owns_input": variant_owns_input,
            "bypass_content_validation": bool(body.bypass_content_validation),
            "force_content_validation": bool(body.force_content_validation),
            "tenant_id": variant_tenant_id,
            "actor_tenant_id": current_user.get("tenant_id"),
            "cross_tenant_admin": _is_cross_tenant_admin,
        },
    ))
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
    # render_params (padre + overrides) → kwargs individuales de
    # run_pipeline. Cada nombre acá EXISTE en la firma de run_pipeline
    # (pipeline.py ~864); mandar uno que no exista revienta el enqueue.
    # lyric_transition + text_motion deprecados 2026-05-23 — fuera del whitelist.
    #
    # La whitelist es _VARIANT_OVERRIDABLE_FIELDS (todo lo que el wizard
    # deja tocar y por lo tanto tiene que llegar al render) + los ejes que
    # sólo se heredan del padre (animate_image, lyric_color, …). Antes se
    # mantenía a mano y se olvidaba de campos nuevos: title_song_break
    # (portada partida en 2 líneas) y los colores de letra se persistían en
    # render_params y NUNCA llegaban al render de la variante.
    for k in (
        *_VARIANT_OVERRIDABLE_FIELDS,
        # Sólo heredables (el wizard de variante no los expone hoy, pero el
        # padre puede tenerlos y perderlos sería una regresión visual).
        "animate_image",
        "lyric_color", "lyric_sung_color",
    ):
        # background_hint se trata aparte abajo ("" = sin hint, no "").
        if k == "background_hint":
            continue
        if k in new_render_params:
            pipeline_kwargs[k] = new_render_params[k]
    # background_hint: None/"" (nunca seteado, o clear explícito del
    # operador) → run_pipeline lo recibe como None y _ensure_background
    # sigue el flow default (PR #116, system prompt desbiaseado). Un ""
    # persistido en render_params NO revive el hint viejo — mismo contrato
    # que /edit (ambos lados coercen con `or None`).
    _variant_hint = new_render_params.get("background_hint")
    if _variant_hint:
        pipeline_kwargs["background_hint"] = _variant_hint

    _commit_pipeline_publication(
        db, new_job, "variant",
        mp3_path=None,
        artist=parent.artist,
        style=new_style,
        plan=plan,
        tenant_id=variant_tenant_id,
        # Biblioteca de fondos: mismo shape que /generate. Cuando hay
        # background_path/bg_r2_key el pipeline saltea la generación IA;
        # en modo "variation" van los variation_source_* y Veo deriva un
        # clip nuevo del asset.
        background_path=variant_bg_path,
        bg_r2_key=variant_bg_r2_key,
        variation_source_path=variant_variation_source_path,
        variation_source_r2_key=variant_variation_source_r2_key,
        variation_parent_asset_id=variant_variation_parent_id,
        **pipeline_kwargs,
    )
    logger.info(
        "[VARIANT] created job=%s parent=%s tenant=%s bypass=%s force=%s",
        new_job_id, parent.job_id, variant_tenant_id,
        bool(body.bypass_content_validation), bool(body.force_content_validation),
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


def _youtube_publish_enabled() -> bool:
    return os.environ.get("YOUTUBE_PUBLISH_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _require_youtube_publish_enabled() -> None:
    if not _youtube_publish_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "feature_disabled",
                "message": "YouTube publishing is not enabled.",
            },
        )


def _load_yt_settings(db: Session, user_id: int) -> dict:
    """The user's YouTube template (title format, header/footer, hashtags,
    mandatory tags, language) from UserSettings — so the template configured
    in Settings → YouTube is actually applied at publish time."""
    row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    return (row.settings_json or {}) if row else {}


@app.get("/youtube/connection-status")
async def youtube_connection_status(
    current_user: dict = Depends(get_current_user),
    _enabled: None = Depends(_require_youtube_publish_enabled),
):
    """Check if a YouTube account is connected and return channel info."""
    import asyncio
    from youtube_upload import get_connection_status
    loop = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, get_connection_status)
    return status


@app.get("/youtube/auth-url")
async def youtube_auth_url(
    current_user: dict = Depends(get_current_user),
    _enabled: None = Depends(_require_youtube_publish_enabled),
):
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
):
    """Disconnect the system YouTube account (admin only)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede desconectar YouTube.")
    from youtube_oauth import delete_system_token
    removed = delete_system_token(db)
    return {"disconnected": removed}


@app.get("/youtube/upload-progress/{job_id}")
async def youtube_upload_progress(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    _enabled: None = Depends(_require_youtube_publish_enabled),
):
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
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
    _enabled: None = Depends(_require_youtube_publish_enabled),
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


def _deliverables_never_produced(job, missing: list[str]) -> set[str]:
    """Entregables que este job NUNCA produjo — distinto de "todavía no están".

    Desde el incidente UMG Chile 2026-08-21 un job puede terminar BIEN sin
    short y/o sin thumbnail: el pipeline degrada la entrega en vez de tirar
    a la basura un master de 519 MB ya renderizado porque falló un clip
    vertical de 30 s (ver pipeline._accessory_failed). Con la lista fija de
    cinco esos jobs quedaban en un callejón sin salida: el gate de
    `publish_delivery_from_job` respondía 409 "Files not yet in R2: short.
    Wait for the render to finish" PARA SIEMPRE, esperando un archivo que
    ya se sabe que no va a existir.

    Hacen falta LAS DOS señales, y el orden importa:

    - la columna en NULL (`update_job(files=...)` sólo escribe las keys
      presentes en el dict, y /retry y /edit las resetean antes de
      re-renderizar), Y
    - el objeto ausente de R2.

    Con una sola alcanzaba para romper algo. Sólo la columna: hay jobs
    viejos con las columnas en NULL y los archivos perfectamente subidos
    (los fixtures de test_deliveries son justo esa forma) — los habríamos
    entregado a UMG sin el short, en silencio. Sólo el objeto ausente: es
    el caso legítimo de "el render todavía no terminó", que debe seguir
    dando 409. La conjunción sólo es verdadera cuando el pipeline decidió
    entregar sin ese archivo.

    `umg_master` y `video` no son negociables: sin master no hay entrega.
    """
    column = {
        "short": job.short_url,
        "umg_short": job.short_url,  # ProRes derivado de short.mp4
        "thumbnail": job.thumbnail_url,
    }
    return {
        ft for ft in missing
        if ft in column and not column[ft]
    }
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
    ddb: Session = Depends(get_deliveries_db),
):
    """Publish an approved job to the UMG portal. Admin only.

    Behaviour when the same job is re-sent: the existing active row is
    UPDATED (label refreshed, added_at bumped) instead of duplicated.
    Matches the manual workflow today — corrected re-renders replace
    the previous version rather than stack up as "Opción N".

    El Job se lee de la DB local (`db`); la fila Delivery se escribe en la
    DB de deliveries (`ddb`), que puede ser externa (portal de prod) cuando
    DELIVERIES_DATABASE_URL está seteada — así staging publica en el mismo
    portal que prod. Sin esa env, ddb == db y el comportamiento es idéntico.
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

    # Validate all 5 files exist in R2. The three render outputs are hard
    # requirements. ProRes is different: it is a lazy derivative and its
    # post-render prewarm is deliberately best-effort, so an explicit
    # "Enviar a UMG" must recover by force-enqueueing missing masters.
    missing = []
    for ft in _DEFAULT_DELIVERY_FILE_TYPES:
        key = _r2_key_for_delivery(job.tenant_id, job.job_id, ft)
        if not storage.object_exists(key):
            missing.append(ft)
    # Un entregable que el job NUNCA produjo no es un "esperá al render":
    # es una entrega parcial legítima. Se saca de los requisitos y de la
    # fila Delivery, así el operador puede mandar a UMG el master que sí
    # está en vez de chocar contra un 409 eterno.
    never_produced = _deliverables_never_produced(job, missing)
    delivery_file_types = [
        ft for ft in _DEFAULT_DELIVERY_FILE_TYPES if ft not in never_produced
    ]
    if never_produced:
        missing = [ft for ft in missing if ft not in never_produced]
        logger.warning(
            "[DELIVERY] job=%s es una entrega PARCIAL: se publica sin %s",
            job_id, sorted(never_produced),
        )
    if missing:
        missing_prores = [
            ft for ft in missing if ft in ("umg_master", "umg_short")
        ]
        missing_render_outputs = [
            ft for ft in missing if ft not in ("umg_master", "umg_short")
        ]
        if missing_render_outputs:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Files not yet in R2: "
                    f"{', '.join(missing_render_outputs)}. "
                    "Wait for the render to finish."
                ),
            )
        if missing_prores and not job.umg_spec:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "prores_required",
                    "message": (
                        "Este video fue generado sólo para YouTube. "
                        "Elegí la configuración ProRes para preparar los "
                        "masters antes de enviarlo a UMG."
                    ),
                    "missing": missing_prores,
                },
            )
        enqueued = []
        try:
            for file_type in missing_prores:
                rq_id = enqueue_prores_prewarm(
                    job_id, file_type, force=True,
                )
                if rq_id:
                    enqueued.append(file_type)
        except Exception as exc:
            logger.warning(
                "[DELIVERY] could not prepare ProRes for %s: %s",
                job_id, exc,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "No se pudo iniciar la preparación ProRes. "
                    "Probá de nuevo en un momento."
                ),
            ) from exc
        return JSONResponse(
            status_code=202,
            content={
                "ok": False,
                "status": "preparing_prores",
                "job_id": job_id,
                "missing": missing_prores,
                "enqueued": enqueued,
                "retry_after": 10,
            },
            headers={"Retry-After": "10"},
        )

    # Compute label. If caller passed one, honor it. Otherwise: first
    # delivery for this song gets "Renderizado"; subsequent ones get
    # "Opción N". Matches the manual items.json conventions.
    label = (body.label if body else None) or _compute_default_delivery_label(
        ddb, job.artist, job.song_title
    )

    # added_by_user_id es FK NOT NULL a users.id de la DB de deliveries. Con
    # DB externa (prod) el id de staging no existe allí → mapear a un admin
    # de prod vía deliveries_added_by(). Sin DB externa, es el current_user.
    added_by = deliveries_added_by(current_user["id"])

    # Replace-not-duplicate: if there's already an active Delivery for
    # this job_id, update it in place. Operator clicks "Enviar a UMG"
    # again after a re-render → we refresh the label + timestamp, the
    # R2 files stay the same (worker overwrites on edit).
    existing = (
        ddb.query(Delivery)
        .filter(Delivery.job_id == job_id)
        .filter(Delivery.removed_at.is_(None))
        .first()
    )
    if existing:
        existing.label = label
        existing.file_types = delivery_file_types
        existing.added_by_user_id = added_by
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
            file_types=delivery_file_types,
            artist_snapshot=job.artist,
            song_title_snapshot=job.song_title or "",
            tenant_snapshot=job.tenant_id,
            frame_size_snapshot=(job.umg_spec or {}).get("frame_size"),
            added_by_user_id=added_by,
            added_at=datetime.now(timezone.utc),
        )
        ddb.add(delivery)
        action = "delivery.create"

    # Commit del delivery (DB externa) PRIMERO: si falla, el AuditLog local no
    # se escribe y no queda fila de auditoría huérfana. El Job local solo se
    # leyó, nunca se muta, así que un fallo acá no corrompe estado local.
    ddb.commit()
    ddb.refresh(delivery)

    db.add(AuditLog(
        user_id=current_user["id"],
        action=action,
        detail={"job_id": job_id, "label": label, "artist": job.artist, "song": job.song_title},
    ))
    db.commit()

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
    ddb: Session = Depends(get_deliveries_db),
):
    """Soft-delete a portal entry. Admin only (JWT)."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return _soft_delete_delivery(ddb, db, delivery_id, current_user["id"])


def _soft_delete_delivery(ddb: Session, db: Session, delivery_id: int, actor_user_id: int | None):
    """Soft-delete: la fila Delivery vive en `ddb` (posible DB externa del
    portal); el AuditLog en la `db` local. removed_by_user_id es FK a los
    users de la DB de deliveries → mapear el id local a uno válido de esa DB."""
    delivery = ddb.query(Delivery).filter(Delivery.id == delivery_id).first()
    if delivery is None or delivery.removed_at is not None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    delivery.removed_at = datetime.now(timezone.utc)
    delivery.removed_by_user_id = (
        deliveries_added_by(actor_user_id) if actor_user_id is not None else None
    )
    ddb.commit()
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
    ddb: Session = Depends(get_deliveries_db),
):
    """Soft-delete from the portal itself. Auth: shared portal token."""
    _verify_portal_token(x_portal_token)
    # actor_user_id=None because the portal has no per-user identity.
    # The audit log entry records the action and which delivery; if we
    # later add per-recipient logins to the portal this will carry their
    # user id instead.
    return _soft_delete_delivery(ddb, db, delivery_id, actor_user_id=None)


@app.post("/api/deliveries/{delivery_id}/change-request")
async def portal_submit_change_request(
    delivery_id: int,
    body: dict,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
    ddb: Session = Depends(get_deliveries_db),
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
        ddb.query(Delivery)
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
    ddb.add(cr)
    ddb.commit()
    ddb.refresh(cr)
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
    try:
        from change_request_stats import classify as classify_change_request
        if {"letra", "sincronizacion"} & set(classify_change_request(comment)):
            from correction_learning import invalidate_job_observations
            invalidate_job_observations(
                db, delivery.job_id, "client_lyrics_or_timing_change_request",
            )
    except Exception as exc:
        logger.warning(
            "[QUALITY-LEARNING] change-request invalidation failed job=%s: %s",
            delivery.job_id, exc,
        )
    db.commit()

    # Notificación en tiempo real (Paso "Cambios de UMG" — el panel del admin
    # exige entrar a mirarlo; esto llega a la bandeja apenas UMG manda el
    # pedido, sin esperar a que alguien abra Operación). emails._send_email ya
    # contiene sus propios fallos de SMTP, pero el thread target igual se
    # envuelve acá (mismo criterio que billing._send_email_async) para que
    # NINGÚN error de este código best-effort — ni siquiera uno futuro por
    # fuera de emails.py — se filtre como excepción no manejada del thread.
    def _notify_umg_change_request():
        try:
            emails.send_umg_change_request_notification(
                delivery.artist_snapshot, delivery.song_title_snapshot,
                comment, delivery_id, delivery.job_id,
            )
        except Exception:
            logger.warning("[CR] notificación de cambio UMG falló", exc_info=True)

    threading.Thread(target=_notify_umg_change_request, daemon=True).start()

    return {"ok": True, "id": cr.id, "submitted_at": cr.submitted_at.isoformat()}


@app.post("/api/deliveries/{delivery_id}/approve")
async def portal_approve_delivery(
    delivery_id: int,
    x_portal_token: str | None = Header(default=None, alias="X-Portal-Token"),
    db: Session = Depends(get_db),
    ddb: Session = Depends(get_deliveries_db),
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
        ddb.query(Delivery)
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
    ddb.commit()
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
    ddb: Session = Depends(get_deliveries_db),
):
    """Portal user undoes a previous approval. Clears approved_at and
    approved_by_label so the row goes back to pending state on the
    portal listing. Idempotent — calling on an unapproved row is a no-op.
    """
    _verify_portal_token(x_portal_token)
    delivery = (
        ddb.query(Delivery)
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
    ddb.commit()
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
    db: Session = Depends(get_deliveries_db),
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
            "added_at": d.added_at.isoformat() if d.added_at else None,
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
    ddb: Session = Depends(get_deliveries_db),
):
    """List change requests for the operator UI. Admin only.

    status: "pending" (default), "resolved", or "all".
    Returns request + delivery context (artist/song/label/frame/job_id)
    plus resolved_by username so the admin sees who acted on each one.

    Las tablas del portal (DeliveryChangeRequest, Delivery, y el resolver
    User que las resolvió) se leen de `ddb` (posible DB externa de prod). El
    Job subyacente y su owner viven en la DB local (`db`).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if status not in ("pending", "resolved", "all"):
        raise HTTPException(status_code=400, detail="status must be pending|resolved|all")
    if limit < 1 or limit > 1000:
        limit = 200

    q = ddb.query(DeliveryChangeRequest)
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
        for d in (ddb.query(Delivery).filter(Delivery.id.in_(delivery_ids)).all() if delivery_ids else [])
    }
    # Resolver: resolved_by_user_id referencia los users de la DB de deliveries
    # (prod cuando es externa) → resolver sobre ddb.
    users_by_id = {
        u.id: u
        for u in (ddb.query(User).filter(User.id.in_(user_ids)).all() if user_ids else [])
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
        ddb.query(DeliveryChangeRequest)
        .filter(DeliveryChangeRequest.resolved_at.is_(None))
        .count()
    )
    resolved_count = (
        ddb.query(DeliveryChangeRequest)
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


# Presets internos del wizard, SOLO-ADMIN. Rellenan el formulario del operador
# con settings derivados del trabajo aprobado de un cliente. El nombre y los
# valores NO deben viajar en el bundle del frontend — un no-admin no debe ni
# recibir el JSON. Por eso viven acá, detrás de auth admin, en vez de hardcodeados
# en el cliente (audit 2026-07-27: la tarjeta "Receta UMG Argentina" estaba en el
# bundle público; sólo se renderizaba para admins pero el string+valores eran
# inspeccionables por DevTools).
_ADMIN_WIZARD_PRESETS = [
    {
        "key": "umg_argentina",
        "label": "Receta UMG Argentina",
        "description": (
            "Rellena los settings de menor retrabajo. "
            "Podés ajustar cualquiera antes de generar."
        ),
        # Cada campo mapea 1:1 a un setter del wizard en el cliente. Aplicarlo
        # sólo rellena el formulario del propio admin — no cambia defaults del
        # servidor ni del tenant, igual que si se tipeara a mano.
        "apply": {
            "style": "auto",
            "bgMode": "auto",
            "sceneMode": "lyrics",
            "enableScenes": False,
            "batchDefaults": {
                "movementStyle": "estatico",
                "font": "poppins-bold",
                "fontScale": "1.3",
                "textCase": "upper",
                "lyricsAnimation": "none",
                "lineTransition": "none",
                "effect": "",
                "titleTemplate": "auto",
                "frameFormat": "full",
            },
            "deliveryProfile": "both",
            "umgFrameSize": "HD",
            "umgFps": 24,
            "umgProresProfile": 3,
        },
    },
]


@app.get("/admin/wizard-presets")
async def admin_wizard_presets(
    current_user: dict = Depends(get_current_user),
):
    """Presets internos del wizard (SOLO-ADMIN). El frontend los pide únicamente
    cuando el usuario es admin, así el nombre + los valores nunca llegan al
    bundle de un no-admin. 403 para cualquier no-admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return {"presets": _ADMIN_WIZARD_PRESETS}


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
    ddb: Session = Depends(get_deliveries_db),
):
    """Mark a change request resolved. Optional resolution_note (<=2000 chars)
    so the operator can leave a one-liner explaining what was done."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    cr = ddb.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
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
    # resolved_by_user_id es FK a users de la DB de deliveries → mapear.
    cr.resolved_by_user_id = deliveries_added_by(current_user["id"])
    cr.resolution_note = note or None
    ddb.commit()
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
    ddb: Session = Depends(get_deliveries_db),
):
    """Undo a resolution. The original submission stays — only the
    resolved_at/resolved_by/resolution_note get cleared. Audit log
    records who reopened it."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    cr = ddb.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Change request not found")
    if cr.resolved_at is None:
        return {"ok": True, "already_pending": True}
    cr.resolved_at = None
    cr.resolved_by_user_id = None
    cr.resolution_note = None
    ddb.commit()
    db.add(AuditLog(
        user_id=current_user["id"],
        action="delivery.change_request.reopen",
        detail={"change_request_id": cr_id, "delivery_id": cr.delivery_id},
    ))
    db.commit()
    return {"ok": True}
