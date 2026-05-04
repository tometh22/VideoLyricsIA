"""FastAPI application for GenLy AI — Production SaaS."""

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

# --- Sentry (must init before FastAPI) ---
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.1")),
        environment=os.environ.get("SENTRY_ENV", "production"),
        release=os.environ.get("SENTRY_RELEASE", "genly@2.0.0"),
    )

from fastapi import FastAPI, File, Form, Query, UploadFile, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
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
    get_plan_usage,
    ensure_default_admin,
    pwd_context,
    PLANS,
)
import storage
from datetime import datetime, timedelta, timezone

from database import Job, User, UserSettings, AuditLog, get_db, init_db
from jobs import create_job, get_job, get_all_jobs, update_job
from observability import init_sentry, init_logging, health_snapshot
from pipeline import run_pipeline, transcribe
from queue_jobs import enqueue_pipeline, queue_depth
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
# Empty falls back to "*" for local dev.
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Include routers ---
app.include_router(billing_router)
app.include_router(admin_router)


# --- Startup ---
@app.on_event("startup")
def on_startup():
    """Initialize DB and create default admin."""
    init_db()
    db = next(get_db())
    try:
        ensure_default_admin(db)
    finally:
        db.close()
    logger.info("GenLy AI started — database initialized")


# --- Background library (public, authenticated) ---
_BACKGROUNDS_LIB = os.path.join(os.path.dirname(__file__), "..", "assets", "backgrounds", "library")


@app.get("/backgrounds")
async def list_backgrounds(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List active pre-approved background assets."""
    from database import BackgroundAsset
    assets = db.query(BackgroundAsset).filter(BackgroundAsset.is_active == True).order_by(BackgroundAsset.created_at.desc()).all()
    return [a.to_dict() for a in assets]


@app.get("/backgrounds/{asset_id}/preview")
async def preview_background(
    asset_id: int,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Serve a background asset file for preview."""
    get_current_user_from_token_param(token, db)
    from database import BackgroundAsset
    asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    file_path = os.path.join(_BACKGROUNDS_LIB, asset.filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "video/mp4" if asset.file_type == "mp4" else f"image/{asset.file_type}"
    return FileResponse(file_path, media_type=media_type)


@app.get("/health")
async def health():
    """Runtime health. No auth — used by load balancers and uptime probes."""
    return health_snapshot()


# ---------------------------------------------------------------------------
# Auth endpoints (public)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str = ""


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class VerifyEmailRequest(BaseModel):
    token: str


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
        },
    }


@app.post("/auth/register")
@limiter.limit("5/minute")
async def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Public self-registration."""
    if len(body.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

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
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user = verify_password_reset_token(db, body.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.hashed_password = pwd_context.hash(body.password)
    db.commit()

    return {"ok": True, "message": "Password reset successfully"}


@app.post("/auth/verify-email")
async def verify_email_endpoint(body: VerifyEmailRequest, db: Session = Depends(get_db)):
    """Verify email address."""
    user = verify_email_token(db, body.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    return {"ok": True, "message": "Email verified successfully"}


@app.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user info."""
    return current_user


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

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
_MP3_MAGIC_BYTES = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def _validate_mp3_upload(file, data: bytes) -> None:
    """Validate a freshly-read MP3 payload. Raises 400 on any problem.

    Bypassing the `.mp3` extension check is trivial, so we also peek at the
    first bytes and enforce a size ceiling.
    """
    if not file.filename or not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")
    size_mb = len(data) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max allowed: {MAX_UPLOAD_MB} MB.",
        )
    if not data.startswith(_MP3_MAGIC_BYTES):
        raise HTTPException(
            status_code=400,
            detail="File does not look like a valid MP3 (magic bytes check failed).",
        )


def _enforce_plan_quota(db: Session, current_user: dict) -> None:
    """Raise 402 if the tenant reached its monthly limit without overage allowed."""
    plan = current_user.get("plan", "100")
    tenant_id = current_user["tenant_id"]
    usage = get_plan_usage(db, current_user["id"], tenant_id, plan)
    if usage["remaining"] <= 0 and plan != "unlimited":
        if not current_user.get("allow_overage", False):
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Plan '{plan}' monthly limit reached "
                    f"({usage['used']}/{usage['limit']}). "
                    "Upgrade the plan or enable overage to continue."
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


def _parse_umg_params(
    delivery_profile: str,
    umg_frame_size: str,
    umg_fps: str,
    umg_prores_profile: str,
) -> dict | None:
    """Parse and validate UMG delivery params. Returns umg_spec dict or None."""
    if delivery_profile not in ("youtube", "umg", "both"):
        raise HTTPException(
            status_code=400,
            detail="delivery_profile must be one of: youtube, umg, both",
        )
    if delivery_profile == "youtube":
        return None
    if not (umg_frame_size and umg_fps and umg_prores_profile):
        raise HTTPException(
            status_code=400,
            detail="umg_frame_size, umg_fps and umg_prores_profile are required "
                   "when delivery_profile includes UMG",
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


@app.post("/upload")
@limiter.limit("120/minute")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    artist: str = Form(...),
    style: str = Form("oscuro"),
    language: str = Form(""),
    delivery_profile: str = Form("youtube"),
    umg_frame_size: str = Form(""),
    umg_fps: str = Form(""),
    umg_prores_profile: str = Form(""),
    background_id: int = Form(None),
    background_file: UploadFile = File(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Receive an MP3 and start processing."""
    data = await file.read()
    _validate_mp3_upload(file, data)

    _enforce_plan_quota(db, current_user)
    _enforce_daily_volume_cap(db, current_user)
    # Every submission is accepted as queued; RQ gives it to a worker the
    # moment one is free, and pipeline.run_pipeline flips status to
    # "processing" on its first line. No 429 for capacity reasons.
    initial_status = "queued"

    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile)

    # Check AI authorization (UMG Guideline 5) — skip if using library background (no AI)
    if not background_id and current_user.get("role") != "admin":
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
    )
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        f.write(data)

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
    if background_id:
        from database import BackgroundAsset
        asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == background_id, BackgroundAsset.is_active == True).first()
        if asset:
            bg_path = os.path.join(_BACKGROUNDS_LIB, asset.filename)
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
    )

    return {"job_id": job_id, "status": initial_status}


@app.post("/transcribe")
@limiter.limit("20/minute")
async def transcribe_endpoint(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """Transcribe an MP3 and return segments for review/editing."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    import tempfile
    import asyncio

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, file.filename)
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        lang = language.strip() if language.strip() else None
        loop = asyncio.get_event_loop()
        segments = await loop.run_in_executor(None, transcribe, tmp_path, lang)

        # Fetch reference lyrics
        import requests as _req
        basename = os.path.splitext(file.filename)[0]
        artist_hint, song_hint = "", basename
        if " - " in basename:
            artist_hint, song_hint = basename.split(" - ", 1)
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(En Vivo)", "(Live)", "(Lyrics)",
                     "- River Plate", "- Luna Park", "- En Vivo"]:
            song_hint = song_hint.replace(sfx, "").strip()

        reference = ""
        try:
            res = _req.get(f"https://api.lyrics.ovh/v1/{artist_hint}/{song_hint}", timeout=10)
            if res.status_code == 200:
                reference = res.json().get("lyrics", "").strip()
        except Exception:
            pass

        return {"segments": segments, "reference_lyrics": reference}
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


@app.post("/generate")
@limiter.limit("120/minute")
async def generate_with_segments(
    request: Request,
    file: UploadFile = File(...),
    artist: str = Form(...),
    style: str = Form("oscuro"),
    language: str = Form(""),
    segments_json: str = Form(...),
    delivery_profile: str = Form("youtube"),
    umg_frame_size: str = Form(""),
    umg_fps: str = Form(""),
    umg_prores_profile: str = Form(""),
    background_id: int = Form(None),
    background_file: UploadFile = File(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate video using user-edited segments (skips Whisper)."""
    data = await file.read()
    _validate_mp3_upload(file, data)

    _enforce_plan_quota(db, current_user)
    _enforce_daily_volume_cap(db, current_user)
    # Every submission is accepted as queued; RQ gives it to a worker the
    # moment one is free, and pipeline.run_pipeline flips status to
    # "processing" on its first line. No 429 for capacity reasons.
    initial_status = "queued"

    # Check AI authorization (UMG Guideline 5) — skip if using library background (no AI)
    if not background_id and current_user.get("role") != "admin":
        user_model = db.query(User).filter(User.id == current_user["id"]).first()
        if user_model and not user_model.ai_authorized:
            raise HTTPException(status_code=403, detail="AI tool usage not authorized. Contact admin for approval.")

    segments = json.loads(segments_json)
    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile)

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
    )
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        f.write(data)

    # Cross-container input transfer via R2 — see /upload for the full reason.
    input_r2_key = None
    if storage.is_enabled():
        input_r2_key = storage.upload_input(
            mp3_path, current_user["tenant_id"], job_id, file.filename,
        )

    # Resolve background: library asset > custom upload > AI generation
    bg_path = None
    bg_r2_key = None
    if background_id:
        from database import BackgroundAsset
        asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == background_id, BackgroundAsset.is_active == True).first()
        if asset:
            bg_path = os.path.join(_BACKGROUNDS_LIB, asset.filename)
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
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
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
    }


@app.get("/jobs")
async def list_jobs(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = current_user["tenant_id"]
    return get_all_jobs(db, tenant_id=tenant_id)


FILE_MAP = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
}

MEDIA_TYPES = {
    "video": "video/mp4",
    "short": "video/mp4",
    "thumbnail": "image/jpeg",
    "umg_master": "video/quicktime",
}

# File types that can't be previewed in-browser (ProRes is not browser-playable).
NON_PREVIEWABLE = {"umg_master"}


@app.get("/download/{job_id}/{file_type}")
async def download(
    job_id: str,
    file_type: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_token_param(token, db)
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")

    # Prefer a pre-signed URL to R2 so the uvicorn worker isn't tied up
    # streaming multi-GB ProRes masters.
    s3_key = (job.get("s3_keys") or {}).get(file_type)
    if s3_key and storage.is_enabled():
        url = storage.generate_signed_url(s3_key, expiry_seconds=3600)
        if url:
            return RedirectResponse(url, status_code=302)

    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, filename=FILE_MAP[file_type], media_type="application/octet-stream")


@app.get("/preview/{job_id}/{file_type}")
async def preview(
    job_id: str,
    file_type: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_token_param(token, db)
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    if file_type in NON_PREVIEWABLE:
        raise HTTPException(
            status_code=415,
            detail=f"{file_type} is a delivery master and cannot be previewed in-browser. "
                   f"Use /download/{job_id}/{file_type} instead.",
        )
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in ("done", "pending_review"):
        raise HTTPException(status_code=400, detail="Job is not ready for preview.")

    # Local copy is removed after R2 upload to keep disk usage bounded. Fall
    # back to a signed URL for the preview in that case.
    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type=MEDIA_TYPES[file_type])

    s3_key = (job.get("s3_keys") or {}).get(file_type)
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
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
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
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
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
    notes: str = ""


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

    job = db.query(JobModel).filter(
        JobModel.job_id == job_id,
        JobModel.tenant_id == current_user["tenant_id"],
    ).first()
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

    job = db.query(JobModel).filter(
        JobModel.job_id == job_id,
        JobModel.tenant_id == current_user["tenant_id"],
    ).first()
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
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
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
    tenant_id = current_user["tenant_id"]
    job = get_job(db, job_id, tenant_id=tenant_id)
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
