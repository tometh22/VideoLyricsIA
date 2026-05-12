"""FastAPI application for GenLy AI — Production SaaS."""

import asyncio
import json
import logging
import os
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

# --- Sentry (must init before FastAPI) ---
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.1")),
        # SENTRY_ENV overrides ENVIRONMENT only if explicitly set (back-compat).
        environment=os.environ.get("SENTRY_ENV") or ENVIRONMENT,
        release=os.environ.get("SENTRY_RELEASE", "genly@2.0.0"),
    )

from fastapi import FastAPI, File, Form, Query, UploadFile, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
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
    generate_api_key,
)
import storage
from datetime import datetime, timedelta, timezone

from database import (
    Job, User, UserSettings, AuditLog, APIKey, get_db, init_db,
    BackgroundAsset, AssetUsage, scoped_db, pool_stats,
)
from jobs import bulk_delete_jobs, create_job, delete_job, get_job, get_all_jobs, update_job
from observability import init_sentry, init_logging, health_snapshot
from pipeline import run_pipeline, transcribe
from queue_jobs import enqueue_pipeline, enqueue_edit, queue_depth, enqueue_prores_prewarm
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

# --- CORS: comma-separated list, e.g. "https://app.example.com,https://admin.example.com"
# In production we refuse to start with no allowed origins — wildcard +
# credentials is what Starlette's CORSMiddleware actually emits as
# "reflect-the-Origin", which lets any site make credentialed requests.
# Local dev is permitted to fall back to wildcard *without* credentials.
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]

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

if not _ALLOWED_ORIGINS:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "CORS_ORIGINS must be set explicitly in production. Set CORS_ORIGINS "
            "or FRONTEND_URL/APP_URL/RAILWAY_PUBLIC_DOMAIN for safe fallback."
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    def _reaper_loop():
        # Brief delay so the very first request doesn't compete with
        # a cold-start reaper holding a DB connection.
        _time.sleep(60)
        while True:
            try:
                n = _reap()
                if n > 0:
                    logger.warning(f"reaper killed {n} stuck job(s)")
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)  # back off on error
            _time.sleep(300)  # 5 min between successful passes

    threading.Thread(target=_reaper_loop, daemon=True, name="reaper").start()
    logger.info("reaper thread started (threshold=100min, every 5min)")

    # Outputs cleanup loop. Sweeps OUTPUTS_DIR every hour to keep
    # local disk bounded — deletes jobs whose deliverables are on R2
    # and retries the upload for jobs whose R2 push failed earlier.
    # Without this, a transient R2 outage leaves multi-GB ProRes
    # masters on disk forever and Railway disk fills over weeks.
    def _outputs_cleanup_loop():
        _time.sleep(120)  # let the API come up first
        while True:
            try:
                from scripts.cleanup_old_outputs import cleanup as _cleanup_outputs
                _cleanup_outputs()
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
async def list_backgrounds(
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
async def background_usage(
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
async def list_fonts(current_user: dict = Depends(get_current_user)):
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
    """Runtime health. No auth — used by load balancers and uptime probes.

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


# ---------------------------------------------------------------------------
# Auth endpoints (public)
# ---------------------------------------------------------------------------

# Pydantic max_length aplicado consistente a TODOS los inputs de
# cliente — ver CONTRIBUTING.md para convenciones de tamaños.
# Defensa contra DoS por payload size: sin esto un atacante manda
# 100 MB de string en cualquier field.
class LoginRequest(BaseModel):
    username: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


class RegisterRequest(BaseModel):
    username: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)
    email: str = Field(default="", max_length=320)  # RFC 5321 max email length


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., max_length=320)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., max_length=500)
    password: str = Field(..., max_length=200)


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., max_length=500)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)


class DeleteAccountRequest(BaseModel):
    password: str = Field(..., max_length=200)


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(..., max_length=100)


@app.post("/auth/login")
@limiter.limit("10/minute")
async def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user)

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
            "tenant_id": user.tenant_id,
            "plan": user.plan_id,
            "allow_overage": getattr(user, "allow_overage", False) or False,
            "features": {"prores_export": has_prores_access(user)},
        },
    }


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

    token = create_token(user)

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
            "features": {"prores_export": has_prores_access(user)},
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
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user info."""
    return current_user


@app.post("/auth/refresh")
@limiter.limit("60/minute")
async def refresh_token(
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
    return {"token": create_token(user)}


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
async def drive_status(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Devuelve si este user tiene Drive conectado y, si sí, qué cuenta.
    El frontend lo usa para decidir si mostrar 'Conectar' o 'Conectado
    como X — Desconectar' en Settings."""
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
async def usage(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return current plan usage with overage info."""
    return get_plan_usage(db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"))


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

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
_MP3_MAGIC_BYTES = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
_AUDIO_EXTENSIONS = (".mp3", ".wav")

_TITLE_NOISE_SUFFIXES = (
    "(Official Video)", "(Official Audio)", "(Lyric Video)",
    "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
    "(Live)", "(Lyrics)",
)


def _parse_filename_artist_title(filename: str) -> tuple[str, str]:
    """Best-effort artist/title extraction from a bare filename. Handles two
    naming conventions the operator commonly uploads under:

      "Artist - Title.ext"   → ("Artist", "Title")
      "Title_Artist.ext"     → ("Artist", "Title")   ← Suno/YouTube export form

    Falls back to ("", basename) when neither separator is present so the
    caller can decide whether to insist on a manual entry. Studio-version
    suffixes like "(Official Video)" are stripped from the title in either
    case so the lrclib lookup matches.
    """
    if not filename:
        return "", ""
    base = filename
    for ext in (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    artist, title = "", base.strip()
    if " - " in base:
        head, _, tail = base.partition(" - ")
        artist, title = head.strip(), tail.strip()
    elif "_" in base:
        head, _, tail = base.partition("_")
        title, artist = head.strip(), tail.strip()
    for sfx in _TITLE_NOISE_SUFFIXES:
        title = title.replace(sfx, "").strip()
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
    return {"tenant_id": current_user["tenant_id"]}


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


def _enforce_plan_quota(db: Session, current_user: dict) -> None:
    """Raise 402 if the tenant reached its monthly limit without overage allowed.

    The message is operator-facing (UMG, label teams). It avoids
    backend-y phrasing ("plan", "overage") and points at a human
    contact path so the operator knows what to do — keeping it
    blocking but not a dead-end.
    """
    plan = current_user.get("plan", "100")
    tenant_id = current_user["tenant_id"]
    _lock_user_for_quota(db, current_user["id"])
    usage = get_plan_usage(db, current_user["id"], tenant_id, plan)
    if plan != "unlimited" and usage["percent"] >= 80:
        _try_send_usage_alert(db, current_user, usage)
    if usage["remaining"] <= 0 and plan != "unlimited":
        if not current_user.get("allow_overage", False):
            support_email = os.environ.get("SUPPORT_EMAIL", "soporte@genly.pro")
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
# 200/month ≈ 7/day; a 50/day cap allows 7× headroom for legitimate bursts.
DEFAULT_DAILY_CAP = 50


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
    prevents a runaway from creating $200 of Veo in an hour."""
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
# Max size of a single multipart part. R2 accepts up to 5 GB / part but
# 8 MB is a healthy sweet spot for browser parallelism + retry granularity.
_MULTIPART_PART_SIZE_BYTES = int(
    os.environ.get("MULTIPART_PART_SIZE_BYTES", str(8 * 1024 * 1024))
)
_PRESIGN_PUT_TTL_S = int(os.environ.get("PRESIGN_PUT_TTL_S", "900"))


class _UploadUrlReq(BaseModel):
    filename: str = Field(..., max_length=500)
    content_type: str = Field(default="", max_length=200)
    size_bytes: int = 0
    artist: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=300)


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
    if body.size_bytes and body.size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
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
    parsed_artist, parsed_title = _parse_filename_artist_title(body.filename)
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

    use_multipart = (
        body.size_bytes > 0 and body.size_bytes >= _MULTIPART_THRESHOLD_BYTES
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
    job_id: str = Field(..., max_length=64)
    filename: str = Field(..., max_length=500)
    content_type: str = Field(default="", max_length=200)


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
    needs to start signing parts."""
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
        return {
            "upload_id": job_row.multipart_upload_id,
            "key": job_row.input_r2_key,
            "part_size": _MULTIPART_PART_SIZE_BYTES,
            "presign_ttl_s": _PRESIGN_PUT_TTL_S,
        }
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
    return {
        "upload_id": init["upload_id"],
        "key": init["key"],
        "part_size": _MULTIPART_PART_SIZE_BYTES,
        "presign_ttl_s": _PRESIGN_PUT_TTL_S,
    }


class _MultipartPartReq(BaseModel):
    job_id: str = Field(..., max_length=64)
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
        body.part_number, expiry_seconds=_PRESIGN_PUT_TTL_S,
    )
    if not url:
        raise HTTPException(status_code=503, detail="Could not sign part URL.")
    return {"url": url, "expires_in": _PRESIGN_PUT_TTL_S}


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
    to r2.cloudflarestorage.com which would require R2 bucket CORS config."""
    if part_number < 1 or part_number > 10_000:
        raise HTTPException(status_code=400, detail="part_number out of range")
    from jobs import get_job_model
    job_row = get_job_model(db, job_id)
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
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty chunk.")
    etag = storage.upload_part(
        job_row.input_r2_key, job_row.multipart_upload_id, part_number, data
    )
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
    from jobs import get_job_model
    job_row = get_job_model(db, job_id)
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
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file body.")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    ok = storage.put_object_bytes(job_row.input_r2_key, data, content_type)
    if not ok:
        raise HTTPException(status_code=502, detail="R2 upload failed.")
    return {"job_id": job_id, "key": job_row.input_r2_key}


class _MultipartCompleteReq(BaseModel):
    job_id: str = Field(..., max_length=64)
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
        storage.multipart_complete(
            job_row.input_r2_key, job_row.multipart_upload_id, parts_payload,
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"R2 multipart_complete failed: {e}",
        )
    # Clear the upload_id so the row is recognisably "complete" but
    # input_r2_key + status still need /transcribe-uploaded.
    job_row.multipart_upload_id = None
    db.commit()
    return {"job_id": body.job_id, "key": job_row.input_r2_key}


class _MultipartAbortReq(BaseModel):
    job_id: str = Field(..., max_length=64)


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
    job_id: str = Field(..., max_length=64)
    language: str = Field(default="", max_length=16)
    artist: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=300)


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
    if job_row.status not in ("awaiting_upload", "transcribed_pending"):
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

    _enforce_disk_capacity()
    _enforce_memory_pressure()
    transcription_lease = _try_acquire_transcription_slot()
    try:
        # Materialize the audio onto local disk for Whisper / ffmpeg / etc.
        job_id = body.job_id
        job_dir = os.path.join(OUTPUTS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        audio_path = os.path.join(job_dir, job_row.filename)

        if not os.path.exists(audio_path):
            import asyncio as _asyncio
            for _attempt in range(5):
                if storage.download_object(job_row.input_r2_key, audio_path):
                    break
                if _attempt < 4:
                    await _asyncio.sleep(0.5 * (2 ** _attempt))
            else:
                raise HTTPException(
                    status_code=502,
                    detail="No pudimos leer el archivo subido. Reintentá en unos segundos.",
                )
        _validate_audio_file_on_disk(job_row.filename, audio_path)

        # Reuse the existing Whisper / lrclib machinery from the legacy
        # /transcribe handler. Keeping the implementation in one place via
        # the helper below means the lyrics-recovery / hallucination logic
        # stays in lockstep with the legacy fallback.
        job_row.status = "transcribed_pending"
        job_row.current_step = "editing"
        db.commit()

        return await _run_transcription_for_job(
            request, db, current_user, job_id, audio_path,
            language=body.language, artist=body.artist, title=body.title,
        )
    finally:
        _release_transcription_slot(transcription_lease)


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
    artist: str = Form(..., max_length=200),
    song_title: str = Form("", max_length=300),
    style: str = Form("oscuro", max_length=100),
    language: str = Form("", max_length=16),
    delivery_profile: str = Form("youtube", max_length=16),
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
    animate_image: str = Form("", max_length=8),
    text_case: str = Form("upper", max_length=16),
    font_scale: str = Form("1.0", max_length=8),
    lyric_transition: str = Form("cut", max_length=16),
    text_motion: str = Form("none", max_length=16),
    text_contrast: str = Form("medium", max_length=16),
    match_lyrics: bool = Form(True),
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
        parsed_artist, parsed_title = _parse_filename_artist_title(file.filename or "")
        if not artist:
            artist = parsed_artist
        if not song_title:
            song_title = parsed_title

    _enforce_plan_quota(db, current_user)
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
    usage_info = get_plan_usage(db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"))
    if usage_info["alert_100"] and current_user.get("plan") == "free":
        raise HTTPException(status_code=429, detail="Free plan limit reached. Upgrade to continue.")

    tenant_id = current_user["tenant_id"]
    job_id = create_job(
        db,
        artist=artist, style=style, filename=file.filename,
        user_id=current_user["id"], tenant_id=tenant_id,
        delivery_profile=delivery_profile, umg_spec=umg_spec,
        initial_status=initial_status,
        song_title=song_title,
    )
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    await _stream_upload_to_disk(file, mp3_path)
    _validate_audio_file_on_disk(file.filename, mp3_path)

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
        bg_ext = os.path.splitext(background_file.filename)[1].lower()
        if bg_ext in (".mp4", ".mov", ".jpg", ".jpeg", ".png"):
            bg_filename = f"bg_custom{bg_ext}"
            bg_path = os.path.join(job_dir, bg_filename)
            with open(bg_path, "wb") as f:
                shutil.copyfileobj(background_file.file, f)
            # User-provided backgrounds also need to cross to the worker.
            if storage.is_enabled():
                bg_r2_key = storage.upload_input(
                    bg_path, tenant_id, job_id, bg_filename,
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
        animate_image=str(animate_image).strip().lower() in ("true", "1", "yes", "on"),
        song_title=song_title,
        text_case=text_case if text_case in ("upper", "title", "lower", "original") else "upper",
        font_scale=_font_scale,
        lyric_transition=lyric_transition if lyric_transition in ("cut", "fade", "fade_slow") else "cut",
        text_motion=text_motion if text_motion in ("none", "subtle", "float") else "none",
        text_contrast=text_contrast if text_contrast in ("subtle", "medium", "strong") else "medium",
        match_lyrics=match_lyrics,
    )

    return {"job_id": job_id, "status": initial_status}


@app.post("/transcribe")
@limiter.limit("20/minute")
async def transcribe_endpoint(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    language: str = Form("", max_length=16),
    artist: str = Form("", max_length=200),
    title: str = Form("", max_length=300),
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
    parsed_artist, parsed_title = _parse_filename_artist_title(file.filename or "")
    job_artist = artist_form or parsed_artist or "Unknown"
    job_song_title = title_form or parsed_title

    job_id = create_job(
        db,
        artist=job_artist,
        style="oscuro",                    # set for real in /generate
        filename=file.filename,
        user_id=current_user["id"],
        tenant_id=current_user["tenant_id"],
        delivery_profile="youtube",        # set for real in /generate
        initial_status="transcribed_pending",
        song_title=job_song_title,
    )

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    audio_path = os.path.join(job_dir, file.filename)
    # Stream the body in 1 MiB chunks. Lossless WAVs (~30-50 MB for a
    # 3-min track) used to OOM the API container under concurrent load
    # because we buffered the full payload in RAM.
    await _stream_upload_to_disk(file, audio_path)
    _validate_audio_file_on_disk(file.filename, audio_path)

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
            print(f"[TRANSCRIBE] R2 upload failed for {job_id}: {e}")

    # Per-request scratch dir for intermediate slices (intro/body cuts).
    # The main audio file lives under job_dir and stays around until
    # /generate enqueues it (or the reaper cleans it up).
    return await _run_transcription_for_job(
        request, db, current_user, job_id, audio_path,
        language=language, artist=artist, title=title,
        filename=file.filename,
    )


async def _run_transcription_for_job(
    request, db, current_user, job_id: str, audio_path: str,
    *, language: str = "", artist: str = "", title: str = "",
    filename: str = "",
):
    """Shared transcription pipeline: lrclib synced/plain → Whisper →
    hallucination recovery → segments. Used by both /transcribe (legacy
    multipart upload) and /transcribe-uploaded (presigned-R2 path).

    Returns the standard `{job_id, segments, reference_lyrics, ...}`
    dict. Cleans up its own scratch dir but never touches `audio_path`
    (caller owns that file)."""
    import tempfile
    import asyncio

    if not filename:
        filename = os.path.basename(audio_path)

    tmp_dir = tempfile.mkdtemp()
    tmp_path = audio_path

    try:
        lang = language.strip() if language.strip() else None
        loop = asyncio.get_event_loop()

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
            print(f"[LYRICS] no artist supplied for {filename!r} — "
                  f"Gemini fetch will be skipped, falling through to lyrics.ovh")

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
            _fetch_lrclib, _lrc_to_segments, _audio_duration,
            _slice_audio_prefix, _slice_audio_window, _verify_lrclib_alignment,
            _detect_hallucination, _synthesize_segments_from_plain,
            _align_whisper_to_plain, _fill_gaps_with_reference,
        )
        lrc = await asyncio.to_thread(_fetch_lrclib, artist_hint, song_hint, db)
        if lrc:
            synced = lrc.get("synced")
            plain = lrc.get("plain") or ""
            lrc_dur = lrc.get("duration")
            if synced:
                user_dur = await asyncio.to_thread(_audio_duration, tmp_path)
                offset = 0.0
                use_synced = True
                hybrid_intro_segs: list[dict] = []
                if user_dur is not None and lrc_dur:
                    diff = user_dur - lrc_dur
                    if abs(diff) <= 3.0:
                        offset = 0.0
                    elif 3.0 < diff <= 120.0:
                        offset = float(diff)
                        # User has extra audio at the start (typical "Official
                        # Video" cut with a dialogue intro). Slice that chunk
                        # and run Whisper on it so the operator gets the
                        # spoken dialogue subtitled too — they can prune in
                        # the editor if they don't want to publish it.
                        intro_path = os.path.join(tmp_dir, "intro.mp3")
                        if _slice_audio_prefix(tmp_path, intro_path, diff + 1.0):
                            try:
                                wsegs = await loop.run_in_executor(
                                    None, transcribe, intro_path, lang, plain,
                                )
                                # Keep only segments that fully sit in the
                                # intro window — defensive, in case ffmpeg
                                # cut on a frame boundary slightly past `diff`.
                                hybrid_intro_segs = [
                                    s for s in wsegs if s["end"] <= diff + 0.5
                                ]
                            except Exception as e:
                                print(f"[LYRICS] intro Whisper failed: {e}")
                            finally:
                                try:
                                    os.unlink(intro_path)
                                except OSError:
                                    pass
                        print(f"[LYRICS] lrclib duration mismatch "
                              f"(user={user_dur:.1f}s, lrclib={lrc_dur:.1f}s) "
                              f"— +{offset:.2f}s offset on song; intro Whisper "
                              f"produced {len(hybrid_intro_segs)} segments")
                    else:
                        use_synced = False
                        print(f"[LYRICS] lrclib duration mismatch "
                              f"(user={user_dur:.1f}s, lrclib={lrc_dur:.1f}s, "
                              f"diff={diff:+.1f}s) — too risky to auto-align, "
                              f"falling back to Whisper")
                if use_synced:
                    song_segs = _lrc_to_segments(
                        synced, lrc_dur, time_offset=offset,
                    )
                    # Alignment verification — when we applied an offset, we
                    # have NO ground-truth that the offset is correct (the
                    # extra audio could be at the end, not the start). Slice
                    # ~5 s of the user's audio at where we claim a song line
                    # starts, run Whisper, fuzzy-match. If the match is weak
                    # (< 0.4), we don't trust the alignment and fall through
                    # to plain + Whisper. Cost: 1 extra Whisper call on 5 s
                    # of audio (~$0.0005) and ~3 s of latency.
                    if offset > 0 and song_segs:
                        # Verify mid-song (more robust than first line which
                        # may be a short ad-lib like "¡Karol!")
                        mid_idx = min(len(song_segs) - 1, len(song_segs) // 2)
                        verify_seg = song_segs[mid_idx]
                        # Skip verification if the chosen text is too short
                        # to fuzzy-match reliably.
                        if len(verify_seg["text"]) >= 10:
                            confidence = await asyncio.to_thread(
                                _verify_lrclib_alignment,
                                tmp_path, verify_seg["text"], verify_seg["start"],
                            )
                            if confidence is not None:
                                if confidence < 0.4:
                                    print(f"[LYRICS] alignment verification FAILED "
                                          f"(confidence={confidence:.2f} at "
                                          f"t={verify_seg['start']:.1f}s for "
                                          f"{verify_seg['text'][:40]!r}) — "
                                          f"falling back to Whisper+plain")
                                    use_synced = False
                                else:
                                    print(f"[LYRICS] alignment verified "
                                          f"(confidence={confidence:.2f})")
                            elif diff > 60.0:
                                # High-diff offsets are riskier — if we can't
                                # even verify, don't gamble; fall back.
                                print(f"[LYRICS] alignment verification "
                                      f"unavailable for high-diff offset "
                                      f"({diff:.1f}s) — falling back")
                                use_synced = False
                    if use_synced and song_segs and len(song_segs) >= 8:
                        from pipeline import (
                            _filter_intro_song_overlap,
                            _fix_lrc_first_line_at_zero,
                        )
                        hybrid_intro_segs, _dup = _filter_intro_song_overlap(
                            hybrid_intro_segs, song_segs,
                        )
                        if _dup:
                            print(f"[LYRICS] discarded {_dup} intro seg(s) as "
                                  f"song-line hallucinations")
                        # Only apply the gap-based first-line correction
                        # when we don't have an intro Whisper pass to
                        # cover the pre-vocal region — otherwise the
                        # intro segments naturally sit before line 1 and
                        # there's no anomaly to fix.
                        if not hybrid_intro_segs:
                            song_segs, _moved = _fix_lrc_first_line_at_zero(
                                song_segs, audio_duration=user_dur,
                            )
                            if _moved is not None:
                                print(f"[LYRICS] lrclib line 1 was anchored "
                                      f"at 0s with a long gap to line 2; "
                                      f"shifted to {_moved:.2f}s based on "
                                      f"median cadence")
                        combined = hybrid_intro_segs + song_segs
                        print(f"[LYRICS] lrclib synced hit — "
                              f"{len(combined)} segments "
                              f"({len(hybrid_intro_segs)} intro + "
                              f"{len(song_segs)} song), skipping main Whisper "
                              f"for {artist_hint!r} - {song_hint!r}")
                        return {
                            "job_id": job_id,
                            "segments": combined,
                            "reference_lyrics": plain or synced,
                        }
            # No synced (or too few segments / unreliable timestamps) — but
            # we still have plain text from lrclib. Use it as the reference
            # so the editor's suggestion engine fires, and skip the Gemini-
            # grounded search step entirely (lrclib already gave us a clean
            # source).
            if plain:
                print(f"[LYRICS] lrclib plain hit ({len(plain)} chars) — "
                      f"running Whisper for timestamps (lyrics_hint primed), "
                      f"skipping Gemini")

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
                        print(f"[LYRICS] trimmed {intro_offset:.1f}s intro "
                              f"before Whisper "
                              f"(user={user_dur:.1f}s, lrclib={lrc_dur:.1f}s)")
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
                if intro_offset > 0:
                    intro_path = os.path.join(tmp_dir, "intro_only.mp3")
                    if await asyncio.to_thread(
                        _slice_audio_prefix, tmp_path, intro_path,
                        intro_offset + 1.0,
                    ):
                        try:
                            intro_segs_raw = await loop.run_in_executor(
                                None, transcribe, intro_path, lang, plain,
                            )
                            # Keep only segments that fully sit in the
                            # intro window; defensive against ffmpeg
                            # frame-boundary slop.
                            intro_segments = [
                                s for s in intro_segs_raw
                                if s["end"] <= intro_offset + 0.5
                            ]
                            print(f"[LYRICS] intro Whisper produced "
                                  f"{len(intro_segments)} segment(s) for "
                                  f"the {intro_offset:.0f}s dialogue prefix")
                        except Exception as e:
                            print(f"[LYRICS] intro Whisper failed: {e}")
                        finally:
                            try:
                                os.unlink(intro_path)
                            except OSError:
                                pass

                try:
                    segments = await loop.run_in_executor(
                        None, transcribe, transcribe_path, lang, plain,
                    )
                finally:
                    if trimmed_path:
                        try:
                            os.unlink(trimmed_path)
                        except OSError:
                            pass

                # Shift body-Whisper timestamps back into full-audio
                # frame so the song subtitles appear at the right moment.
                if intro_offset > 0:
                    segments = [
                        {**s,
                         "start": float(s["start"]) + intro_offset,
                         "end":   float(s["end"])   + intro_offset}
                        for s in segments
                    ]

                # Auto-recover: when Whisper still hallucinates after the
                # trim (instrumental-passage mega-segments, synonym loops,
                # implausibly low count), fall back to distributing lrclib
                # plain lyrics across the SONG REGION only. start_time =
                # intro_offset prevents the synthesizer from compressing
                # the song lines into the spoken-intro region — that would
                # show 3 lyric lines at 0:00 even though the song hasn't
                # started yet (the bug the operator reported).
                hallucinated, reason = _detect_hallucination(segments, user_dur)
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
                            print(f"[LYRICS] discarded {_dup} intro seg(s) "
                                  f"as song-line hallucinations (recovery)")
                        combined = intro_segments + recovered
                        print(f"[LYRICS] hallucination detected ({reason}) "
                              f"— auto-recovered with {len(recovered)} "
                              f"lines from lrclib plain "
                              f"({len(anchors)} time anchors, "
                              f"start={intro_offset:.1f}s, "
                              f"dur={user_dur:.1f}s) "
                              f"+ {len(intro_segments)} intro-Whisper "
                              f"segment(s)")
                        return {
                            "job_id": job_id,
                            "segments": combined,
                            "reference_lyrics": plain,
                            "coverage_warning": True,
                            "recovery_source": "lrclib_plain",
                        }
                # Happy path: Whisper returned plausibly-many segments.
                # Combine intro Whisper (if any) with the body output.
                from pipeline import _filter_intro_song_overlap
                intro_segments, _dup = _filter_intro_song_overlap(
                    intro_segments, segments,
                )
                if _dup:
                    print(f"[LYRICS] discarded {_dup} intro seg(s) as "
                          f"song-line hallucinations")
                combined = intro_segments + segments
                from pipeline import _filter_whisper_hallucinations
                combined, _dropped = _filter_whisper_hallucinations(combined)
                if _dropped:
                    print(f"[TRANSCRIBE] dropped {_dropped} Whisper hallucination phrase(s)")
                return {"job_id": job_id, "segments": combined, "reference_lyrics": plain}

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

        segments = await loop.run_in_executor(None, transcribe, tmp_path, lang)

        # Wait up to 2s after Whisper finishes for Gemini to complete.
        reference = ""
        try:
            result = await asyncio.wait_for(gemini_task, timeout=2.0)
            reference = result or ""
        except asyncio.TimeoutError:
            # Gemini still pending — let it finish in the background and
            # cache the result for the next request. Don't block the user.
            print("[LYRICS] gemini fetch slower than Whisper+2s — moving on")
            reference = ""
        except Exception as e:
            print(f"[LYRICS] gemini task failed: {e}")
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
            hallucinated, reason = _detect_hallucination(segments, user_dur)
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
                    print(f"[LYRICS] hallucination detected on fallback "
                          f"path ({reason}) — gap-fill produced "
                          f"{len(merged)} segments from {src} "
                          f"(~{plausible_count} kept-Whisper, "
                          f"{len(merged) - plausible_count} synthesized, "
                          f"dur={user_dur:.1f}s)")
                    return {
                        "job_id": job_id,
                        "segments": merged,
                        "reference_lyrics": reference,
                        "coverage_warning": True,
                        "recovery_source": src,
                    }

        from pipeline import _filter_whisper_hallucinations
        segments, _dropped = _filter_whisper_hallucinations(segments)
        if _dropped:
            print(f"[TRANSCRIBE] dropped {_dropped} Whisper hallucination phrase(s)")
        return {"job_id": job_id, "segments": segments, "reference_lyrics": reference}
    finally:
        # tmp_dir holds intermediate slices (intro/body cuts) only — the
        # main audio (audio_path) is under job_dir and must survive until
        # /generate enqueues it (or the reaper cleans it up).
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


@app.post("/generate")
@limiter.limit("120/minute")
async def generate_with_segments(
    request: Request,
    file: UploadFile = File(None),
    job_id: str = Form("", max_length=64),
    artist: str = Form("", max_length=200),
    song_title: str = Form("", max_length=300),
    style: str = Form("oscuro", max_length=100),
    language: str = Form("", max_length=16),
    # segments_json es el payload del frontend con timing de cada lyric;
    # un video largo puede pesar varios cientos de KB. 5 MB es techo
    # generoso que rechaza payload absurdo sin restringir casos reales.
    segments_json: str = Form(..., max_length=5_000_000),
    delivery_profile: str = Form("youtube", max_length=16),
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
    animate_image: str = Form("", max_length=8),
    text_case: str = Form("upper", max_length=16),
    font_scale: str = Form("1.0", max_length=8),
    lyric_transition: str = Form("cut", max_length=16),
    text_motion: str = Form("none", max_length=16),
    text_contrast: str = Form("medium", max_length=16),
    match_lyrics: bool = Form(True),
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
        if job_row.status not in ("transcribed_pending", "awaiting_upload"):
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
        parsed_artist, parsed_title = _parse_filename_artist_title(existing_filename or "")
        if not artist:
            artist = parsed_artist
        if not song_title:
            song_title = parsed_title

    _enforce_plan_quota(db, current_user)
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
    usage_info = get_plan_usage(db, current_user["id"], current_user["tenant_id"], current_user.get("plan", "100"))
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
        bg_ext = os.path.splitext(background_file.filename)[1].lower()
        if bg_ext in (".mp4", ".mov", ".jpg", ".jpeg", ".png"):
            bg_filename = f"bg_custom{bg_ext}"
            bg_path = os.path.join(job_dir, bg_filename)
            with open(bg_path, "wb") as f:
                shutil.copyfileobj(background_file.file, f)
            if storage.is_enabled():
                bg_r2_key = storage.upload_input(
                    bg_path, current_user["tenant_id"], job_id, bg_filename,
                )

    _font_scale_gen = 1.0
    try:
        _font_scale_gen = max(0.6, min(1.5, float(font_scale or "1.0")))
    except (ValueError, TypeError):
        pass

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=current_user.get("plan", "100"),
        segments_override=segments,
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
        animate_image=str(animate_image).strip().lower() in ("true", "1", "yes", "on"),
        song_title=song_title,
        text_case=text_case if text_case in ("upper", "title", "lower", "original") else "upper",
        font_scale=_font_scale_gen,
        lyric_transition=lyric_transition if lyric_transition in ("cut", "fade", "fade_slow") else "cut",
        text_motion=text_motion if text_motion in ("none", "subtle", "float") else "none",
        text_contrast=text_contrast if text_contrast in ("subtle", "medium", "strong") else "medium",
        match_lyrics=match_lyrics,
    )

    return {"job_id": job_id, "status": initial_status}


@app.get("/admin/queue")
async def admin_queue(current_user: dict = Depends(get_current_user)):
    """Return queue depth per priority. Admin only."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return queue_depth()


@app.get("/delivery-profiles")
async def get_delivery_profiles(current_user: dict = Depends(get_current_user)):
    """Return the catalog of accepted UMG specs for frontend dropdowns."""
    return {
        "profiles": ["youtube", "umg", "both"],
        "umg": umg_catalog(),
    }


@app.get("/status/{job_id}")
async def status(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from pipeline import _MAX_EDITS
    job = get_job(db, job_id, **_job_scope(current_user))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    edit_count = job.get("edit_count") or 0
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "current_step": job["current_step"],
        "progress": job["progress"],
        "files": job["files"],
        "error": job.get("error"),
        "artist": job.get("artist"),
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
        "render_params": job.get("render_params"),
    }


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

    TERMINAL = {"done", "pending_review", "error", "validation_failed"}
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
        # Merge de dos fixes:
        #   - PR #97: scoped_db() per tick para evitar pool starvation.
        #   - PR #95: re-validar tenant del user en cada tick (cierra el
        #     window donde admin transfiere user entre tenants mid-stream).
        # Ambas queries (User refresh + job fetch) ocurren dentro de la
        # misma sesión corta del context manager → 1 connection por tick,
        # ~2 SELECTs, devuelta al pool en milisegundos.
        unauthorized = False
        while True:
            with scoped_db() as db_tick:
                fresh_user = db_tick.query(User).filter(User.id == _initial_user_id).first()
                if not fresh_user or fresh_user.tenant_id != _initial_tenant_id:
                    unauthorized = True
                    job = None
                else:
                    job = get_job(db_tick, job_id, **scope)
            if unauthorized:
                yield f"event: unauthorized\ndata: {json.dumps({'reason': 'tenant_changed'})}\n\n"
                break
            if job is None:
                break
            sig = (job["status"], job["current_step"], job["progress"])
            if sig != last_sig:
                last_sig = sig
                payload = {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "current_step": job["current_step"],
                    "progress": job["progress"],
                    "error": job.get("error"),
                    "created_at": job.get("created_at"),
                    "completed_at": job.get("completed_at"),
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
async def list_jobs(
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
                print(f"[ZIP] missing source for {ftype}: {local}")
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
    user_model = db.query(User).filter(User.id == current_user["id"]).first()
    return {"token": create_media_token(user_model, job_id, file_type)}


@app.get("/download/{job_id}/{file_type}")
async def download(
    job_id: str,
    file_type: str,
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
    edit_type: str = Field(..., max_length=32)  # "typography" | "background"
    font: str | None = Field(default=None, max_length=64)
    font_scale: float | None = None
    text_case: str | None = Field(default=None, max_length=16)
    lyric_transition: str | None = Field(default=None, max_length=16)
    text_motion: str | None = Field(default=None, max_length=16)


class EnableProResRequest(BaseModel):
    """Body para POST /enable-prores/{job_id}. Mismos campos que el upload
    UMG. Strings sin parsear — _parse_umg_params se encarga de validar y
    convertir a tipos correctos."""
    umg_frame_size: str = Field(..., max_length=16)      # "HD" | "UHD-4K" | "DCI-4K" | "DCI-2K"
    umg_fps: str = Field(..., max_length=16)             # "23.976"...".60"
    umg_prores_profile: str = Field(..., max_length=4)   # "3" (422 HQ) | "4" (4444) | "5" (4444 XQ)


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

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.approve",
        detail={"job_id": job_id, "notes": body.notes},
    ))
    db.commit()

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
    if job.status != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=f"Job must be in pending_review to request edits (current: {job.status})",
        )

    current_edit_count = job.edit_count or 0
    if current_edit_count >= _MAX_EDITS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum edit limit ({_MAX_EDITS}) reached. Please approve or reject.",
        )

    valid_edit_types = ("typography", "background")
    if body.edit_type not in valid_edit_types:
        raise HTTPException(
            status_code=400,
            detail=f"edit_type must be one of {valid_edit_types}",
        )

    if body.edit_type == "typography" and not job.bg_r2_key_cached:
        raise HTTPException(
            status_code=400,
            detail=(
                "No cached background available for this job. "
                "Use edit_type='background' to regenerate it."
            ),
        )

    if not job.segments_json:
        raise HTTPException(
            status_code=400,
            detail="Job has no persisted transcription. Cannot re-render.",
        )

    edit_params: dict = {}
    if body.font is not None:
        edit_params["font"] = body.font
    if body.font_scale is not None:
        edit_params["font_scale"] = body.font_scale
    if body.text_case is not None:
        edit_params["text_case"] = body.text_case
    if body.lyric_transition is not None:
        edit_params["lyric_transition"] = body.lyric_transition
    if body.text_motion is not None:
        edit_params["text_motion"] = body.text_motion

    new_edit_count = current_edit_count + 1

    # Flip to editing immediately so the UI can show progress.
    job.status = "editing"
    job.edit_count = new_edit_count
    job.current_step = "video" if body.edit_type == "typography" else "background"
    job.progress = 0
    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.edit_request",
        detail={
            "job_id": job_id,
            "edit_type": body.edit_type,
            "edit_params": edit_params,
            "edit_count": new_edit_count,
        },
    ))
    db.commit()

    enqueue_edit(
        job_id=job_id,
        edit_type=body.edit_type,
        edit_params=edit_params,
        plan=current_user.get("plan", "100"),
    )

    return {
        "ok": True,
        "job_id": job_id,
        "edit_type": body.edit_type,
        "edit_count": new_edit_count,
        "edits_remaining": _MAX_EDITS - new_edit_count,
    }


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


@app.post("/retry/{job_id}")
async def retry_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-enqueue a failed or validation_failed job using the audio still stored
    in R2. Avoids forcing the user to re-upload a 30-50 MB WAV. Only allowed
    when the job is in an unrecoverable terminal state (error or
    validation_failed) and the source audio is still available in object
    storage (input_r2_key is set and the object exists)."""
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

    # Capturar status PREVIO antes de mutar. Sin esto el AuditLog
    # registraba siempre "processing" como previous_status (la línea de
    # abajo lee job.status DESPUÉS del mutate), haciendo el log
    # inservible para forensics ("¿en qué estado estaba el job cuando
    # el operador apretó retry?" → siempre 'processing').
    _previous_status = job.status

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

    db.add(AuditLog(
        user_id=current_user["id"],
        action="job.retry",
        detail={"job_id": job_id, "previous_status": _previous_status},
    ))
    db.commit()

    umg_spec = job.umg_spec or {}
    enqueue_pipeline(
        job_id=job_id,
        mp3_path=None,
        artist=job.artist,
        style=job.style or "oscuro",
        plan=current_user.get("plan", "100"),
        delivery_profile=job.delivery_profile or "youtube",
        input_r2_key=job.input_r2_key,
        song_title=job.song_title or "",
        umg_spec=umg_spec,
    )

    return {"ok": True, "status": "processing", "job_id": job_id}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
async def get_settings(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return app settings for the current user."""
    settings = db.query(UserSettings).filter(UserSettings.user_id == current_user["id"]).first()
    return settings.settings_json if settings else {}


@app.post("/settings")
async def save_settings(
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

@app.post("/youtube/upload/{job_id}")
async def youtube_upload(
    job_id: str,
    privacy: str = "unlisted",
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a completed job's video to YouTube with AI-generated metadata."""
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

    import asyncio
    from youtube_upload import upload_to_youtube
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, upload_to_youtube, video_path, thumb_path, artist, song, "", privacy, job_id,
    )

    update_job(job_id, youtube=result)

    return result


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

    from youtube_upload import generate_youtube_metadata
    from functools import partial
    import asyncio
    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(
        None, partial(generate_youtube_metadata, job.get("artist", ""), song, "", job_id=job_id),
    )
    return metadata
