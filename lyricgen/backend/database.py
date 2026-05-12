"""PostgreSQL database layer with SQLAlchemy + async support."""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)


class JSONB(TypeDecorator):
    """JSONB on PostgreSQL (supports equality operator); JSON on SQLite (tests)."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB as _JSONB
            return dialect.type_descriptor(_JSONB())
        return dialect.type_descriptor(JSON())

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://genly:genly@localhost:5432/genly",
)

# Handle Heroku-style postgres:// URLs
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Pool sizing is *per-process*. The formula that has to hold under
# burst is:
#
#   max_connections >= (api_workers + rq_workers) × (pool_size + max_overflow)
#
# Railway's default Postgres ships with max_connections=100. With
# 4 API workers + 4 RQ workers = 8 processes, that leaves
#   ceil((100 − 5_reserved_for_admin) / 8) ≈ 11 sockets per process.
# So 5 + 5 = 10 is the most we can run *with the default DB plan*.
#
# After fix/db-pool-streaming-scale: streaming endpoints (/preview,
# /download, /backgrounds/.../preview, /jobs/.../events, /download/all)
# release their pool slot before the file/SSE stream begins via
# scoped_db(). That lifts the per-process concurrency ceiling from
# "≤10 short queries + 0 streams" to "≤10 short queries, unbounded
# concurrent streams". 6 + 4 is now a comfortable default — 6 steady
# slots for the hot dashboard/auth/status endpoints, 4 overflow for
# bursts (UMG batch submissions, multiple operators logging in
# concurrently). The total is still 10 per process, fits the 100-cap.
#
# When (not if) you migrate to a bigger DB plan or front Postgres with
# PgBouncer (see docs/SCALING.md), raise:
#   - DB_POOL_SIZE      (steady-state per-process)
#   - DB_MAX_OVERFLOW   (burst headroom per-process)
# and confirm max_connections still bounds the product above. The fix
# above changes the failure shape — the cap is now real concurrent
# short queries, not concurrent downloads.
_DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "6"))
_DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "4"))

_keepalive_args: dict = {}
if DATABASE_URL.startswith("postgresql"):
    # TCP keepalives so PG notices a dead client in ~80s instead of
    # Railway's 2h default. Prevents zombie idle-in-transaction sessions
    # from a container that Railway killed during a failed deploy.
    _keepalive_args = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }

engine = create_engine(
    DATABASE_URL,
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=os.environ.get("SQL_ECHO", "").lower() == "true",
    connect_args=_keepalive_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


from contextlib import contextmanager  # noqa: E402 — kept next to the helper it powers


@contextmanager
def scoped_db():
    """Short-lived DB session for endpoints that stream large responses.

    `Depends(get_db)` releases the session AFTER FastAPI is done sending
    the response. For a 4 GB ProRes download or a 60-min SSE stream
    that means one pooled connection per in-flight request, held for
    the full duration of the transfer. With pool_size=8 + overflow=8
    per process, a handful of concurrent downloads is enough to lock
    out unrelated short queries (`/usage`, `/jobs`) until the pool
    timeout fires.

    Pattern:
        with scoped_db() as db:
            current_user = verify_media_token(token, job_id, ftype, db)
            job = get_job(db, job_id, ...)
        return FileResponse(file_path, ...)   # session already closed

    Read-only inside the block: no commit happens here. If you write,
    call db.commit() before returning from the block.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def pool_stats() -> dict:
    """Best-effort snapshot of the SQLAlchemy connection pool.

    Returned by `/health` so operators can see exhaustion approaching
    instead of finding out via the 30-second QueuePool timeout in
    Sentry. All counters are per-process — multiply by uvicorn worker
    count for the API-side total.
    """
    p = engine.pool
    try:
        return {
            "size": p.size(),               # configured pool_size
            "checked_out": p.checkedout(),  # in-use connections
            "overflow": p.overflow(),       # overflow connections currently open
            "available": p.checkedin(),     # idle in pool
            "max_overflow": _DB_MAX_OVERFLOW,
            "total_capacity": _DB_POOL_SIZE + _DB_MAX_OVERFLOW,
        }
    except Exception:
        # Pool subclasses without these methods (e.g. SQLite StaticPool
        # in tests) silently degrade to an empty dict.
        return {}


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # user, admin
    tenant_id = Column(String(100), nullable=False, default="default", index=True)
    plan_id = Column(String(20), nullable=False, default="100")
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    stripe_subscription_id = Column(String(255), nullable=True)

    # AI authorization (UMG compliance — Guideline 5)
    ai_authorized = Column(Boolean, default=False)
    ai_authorized_at = Column(DateTime(timezone=True), nullable=True)
    ai_authorized_by = Column(Integer, nullable=True)

    # Per-tenant volume cap. None = use system default DEFAULT_DAILY_CAP.
    # Catches accidental burst usage (mistake, abuse, or runaway loop).
    max_videos_per_day = Column(Integer, nullable=True)

    # Allow the user to keep generating past their plan's monthly limit,
    # paying overage rate per extra video. We bill those out-of-band
    # (transferencia / invoice) — the flag just removes the 402 wall.
    # Default False: a fresh user hits the cap as a hard block, which
    # is the safer behaviour for individuals; sales toggles it on for
    # B2B accounts that prefer overage to a stop-the-world.
    allow_overage = Column(Boolean, default=False, nullable=False, server_default="false")

    # Per-tenant concurrent-jobs cap (a.k.a. "batch size"). None = use system
    # default DEFAULT_MAX_CONCURRENT_JOBS (10). Counts only jobs in
    # status="processing"; pending_review and terminal states don't consume
    # pipeline resources so they don't count.
    max_concurrent_jobs = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    jobs = relationship("Job", back_populates="user", lazy="dynamic", foreign_keys="Job.user_id")
    invoices = relationship("Invoice", back_populates="user", lazy="dynamic")
    settings = relationship("UserSettings", back_populates="user", uselist=False)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "plan": self.plan_id,
            "is_active": self.is_active,
            "email_verified": self.email_verified,
            "ai_authorized": self.ai_authorized,
            "max_videos_per_day": self.max_videos_per_day,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "allow_overage": self.allow_overage,
            "stripe_customer_id": self.stripe_customer_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Job(Base):
    __tablename__ = "jobs"
    # __table_args__ uses the deferred-string form for the DESC column
    # so the Index() can be declared before the Column() it references.
    # SQLAlchemy resolves the names at mapper-config time.
    __table_args__ = (
        # Composite indexes that back the dashboard hot path. Mirrors
        # migration 8802e2187632. created_at is DESC to match the SQL
        # in /jobs (ORDER BY created_at DESC LIMIT 200) so Postgres can
        # forward-scan instead of backward-scanning the index.
        Index(
            "ix_jobs_tenant_status_created",
            "tenant_id",
            "status",
            text("created_at DESC"),
        ),
        Index(
            "ix_jobs_tenant_created",
            "tenant_id",
            text("created_at DESC"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(12), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    artist = Column(String(255), nullable=False)
    song_title = Column(String(500), nullable=True)
    style = Column(String(50), default="oscuro")
    filename = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, default="processing", index=True)
    current_step = Column(String(50), default="whisper")
    progress = Column(Integer, default=0)
    error = Column(Text, nullable=True)

    # Delivery profile (youtube | umg)
    delivery_profile = Column(String(20), default="youtube", nullable=False)
    umg_spec = Column(JSONB, nullable=True)

    # File paths (relative to outputs dir)
    video_url = Column(String(500), nullable=True)
    short_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)
    umg_master_url = Column(String(500), nullable=True)
    umg_short_url = Column(String(500), nullable=True)

    # Cloud storage keys (when deliverables are uploaded to R2/S3)
    s3_keys = Column(JSONB, nullable=True)

    # R2 key of the source audio uploaded by the user. Set by /transcribe
    # so /generate can hand the worker the same file without forcing the
    # browser to re-upload it (the previous flow uploaded the file twice
    # and OOMed the API container on lossless WAVs).
    input_r2_key = Column(Text, nullable=True)

    # In-flight multipart upload id while the browser is still PUTting
    # parts directly to R2. Cleared on multipart_complete (or aborted by
    # the reaper if the upload is abandoned). Uses Text because Cloudflare
    # R2 returns ~300+ char ids (the original VARCHAR(255) silently
    # truncated and crashed the commit on every >50MB upload).
    multipart_upload_id = Column(Text, nullable=True)

    # YouTube info
    youtube_data = Column(JSONB, nullable=True)

    # Content validation (UMG Guideline 15)
    validation_result = Column(JSONB, nullable=True)

    # Approval workflow (UMG compliance)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)

    # Edit requests (post-approval partial re-renders)
    # segments_json — persisted Whisper output so re-renders skip re-transcription.
    # render_params  — font/typography/motion settings used at render time.
    # edit_count     — how many partial re-renders the reviewer has requested (max 3).
    # bg_r2_key_cached — R2 key for the AI-generated background so typography-only
    #   edits can re-use it without paying for Veo again.
    segments_json = Column(JSONB, nullable=True)
    render_params = Column(JSONB, nullable=True)
    edit_count = Column(Integer, default=0, nullable=False, server_default="0")
    bg_r2_key_cached = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="jobs", foreign_keys=[user_id])
    provenance = relationship("AIProvenance", back_populates="job", lazy="dynamic")

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "artist": self.artist,
            "song_title": self.song_title,
            "style": self.style,
            "filename": self.filename,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "current_step": self.current_step,
            "progress": self.progress,
            "delivery_profile": self.delivery_profile,
            "umg_spec": self.umg_spec,
            "files": {
                "video_url": self.video_url,
                "short_url": self.short_url,
                "thumbnail_url": self.thumbnail_url,
                "umg_master_url": self.umg_master_url,
                "umg_short_url": self.umg_short_url,
            },
            "s3_keys": self.s3_keys,
            "error": self.error,
            "youtube": self.youtube_data,
            "validation_result": self.validation_result,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "review_notes": self.review_notes,
            "edit_count": self.edit_count or 0,
            "render_params": self.render_params,
            "created_at": self.created_at.timestamp() if self.created_at else None,
            "completed_at": self.completed_at.timestamp() if self.completed_at else None,
        }

    def to_list_dict(self):
        # `prores_ready` lets the dashboard / history cards show a
        # subtle badge ("✓ ProRes" vs "⏳ Generando ProRes") without
        # needing a second round-trip per row. Truthy iff the lazy
        # transcode has both deliverables on R2.
        s3 = self.s3_keys or {}
        wants_umg = (self.delivery_profile or "youtube") in ("umg", "both")
        return {
            "job_id": self.job_id,
            "status": self.status,
            "artist": self.artist,
            "song_title": self.song_title,
            "filename": self.filename,
            "delivery_profile": self.delivery_profile,
            "prores_ready": (
                bool(s3.get("umg_master")) and bool(s3.get("umg_short"))
                if wants_umg else None
            ),
            "created_at": self.created_at.timestamp() if self.created_at else None,
        }


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stripe_invoice_id = Column(String(255), unique=True, nullable=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), default="usd")
    status = Column(String(30), nullable=False, default="pending")  # pending, paid, failed, void
    description = Column(Text, nullable=True)
    invoice_url = Column(String(500), nullable=True)
    invoice_pdf = Column(String(500), nullable=True)
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User", back_populates="invoices")

    def to_dict(self):
        return {
            "id": self.id,
            "stripe_invoice_id": self.stripe_invoice_id,
            "amount": self.amount_cents / 100,
            "currency": self.currency,
            "status": self.status,
            "description": self.description,
            "invoice_url": self.invoice_url,
            "invoice_pdf": self.invoice_pdf,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    settings_json = Column(JSONB, default=dict)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="settings")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class APIKey(Base):
    """Personal access tokens for programmatic/enterprise integrations."""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    key_prefix = Column(String(12), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hex
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)


class AuditLog(Base):
    """Tracks important actions for admin visibility."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    detail = Column(JSONB, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)


class BackgroundAsset(Base):
    """Pre-approved background assets for video generation."""
    __tablename__ = "background_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    filename = Column(String(500), nullable=False)
    file_type = Column(String(10), nullable=False)  # mp4, jpg, png
    tags = Column(String(500), nullable=True)        # comma-separated: "landscape,ocean,calm"
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # NULL = global (visible to every tenant). A tenant_id string = exclusive
    # to that tenant. Set per-asset by the admin uploader and used as the
    # contractual gate for clients like Universal Music that require their
    # library to be unavailable to anyone else.
    owner_tenant_id = Column(String(100), nullable=True, index=True)
    # If this asset was generated as a variation derived from another library
    # asset (image-to-video off a frame of the parent), this is the parent's
    # id. Useful for audit and for surfacing "derived from X" in the UI.
    parent_asset_id = Column(Integer, ForeignKey("background_assets.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "file_type": self.file_type,
            "tags": self.tags.split(",") if self.tags else [],
            "uploaded_by": self.uploaded_by,
            "owner_tenant_id": self.owner_tenant_id,
            "parent_asset_id": self.parent_asset_id,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AssetUsage(Base):
    """Tracks every time a tenant uses a library asset in a generation job.

    Backs the "you already used this background on [date]" warning in the
    library picker (per-tenant, not per-user) and the usage audit that UMG
    asked for to enforce video uniqueness in their workflow.
    """
    __tablename__ = "asset_usage"
    __table_args__ = (
        Index("ix_asset_usage_asset_tenant", "asset_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("background_assets.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id = Column(String(100), nullable=False, index=True)
    job_id = Column(String(12), nullable=True, index=True)
    mode = Column(String(20), nullable=False, default="as_is")  # "as_is" | "variation"
    used_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "job_id": self.job_id,
            "mode": self.mode,
            "used_at": self.used_at.isoformat() if self.used_at else None,
        }


class AIProvenance(Base):
    """Records every AI tool invocation for UMG compliance and copyright audit."""
    __tablename__ = "ai_provenance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(12), ForeignKey("jobs.job_id"), nullable=False, index=True)
    step = Column(String(50), nullable=False)           # lyrics_analysis, video_bg, image_bg, yt_metadata
    tool_name = Column(String(100), nullable=False)      # gemini-2.5-flash, veo-3.1-generate-001, etc.
    tool_provider = Column(String(50), nullable=False)   # google_vertex
    tool_version = Column(String(100), nullable=True)
    prompt_sent = Column(Text, nullable=False)
    prompt_hash = Column(String(64), nullable=True)      # SHA-256 for dedup/search
    response_summary = Column(Text, nullable=True)       # truncated response
    input_data_types = Column(JSONB, nullable=True)      # ["lyrics_text", "artist_name"]
    output_artifact = Column(String(500), nullable=True) # path to generated file
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    job = relationship("Job", back_populates="provenance")


class LyricsCache(Base):
    """Reference lyrics fetched via Gemini-grounded web search, cached
    per (artist, title) so we only pay Gemini once per song across the
    entire worker fleet. Also serves as the audit row UMG can SELECT
    directly to verify lyrics provenance — every entry carries the
    grounding source URLs from the original Google Search response."""
    __tablename__ = "lyrics_cache"

    cache_key = Column(String(40), primary_key=True)  # sha1(artist|title)[:16]
    artist = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    lyrics = Column(Text, nullable=False)
    source_urls = Column(JSONB, nullable=True)        # list of grounding URIs
    fetched_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    fetched_by_model = Column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables. Call once at startup.

    Also runs lightweight idempotent column-add migrations so deploys
    that pre-date a new column (e.g. users.allow_overage) self-heal on
    boot without an Alembic setup. SQLAlchemy's create_all only creates
    missing TABLES — it ignores missing COLUMNS on existing tables.
    """
    Base.metadata.create_all(bind=engine)
    _migrate_user_columns()


def _migrate_user_columns():
    """Add columns to the `users` table if they're missing. Postgres
    supports `ADD COLUMN IF NOT EXISTS` natively (>= 9.6); SQLite has it
    since 3.35. Wrapped in try/except per dialect quirk so a transient
    failure here never aborts the whole init."""
    from sqlalchemy import text
    column_adds = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS allow_overage BOOLEAN DEFAULT FALSE NOT NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS umg_short_url VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS song_title VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_r2_key VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS multipart_upload_id VARCHAR(255)",
        # Library exclusivity (UMG): tenant-owned and variation-parent references.
        "ALTER TABLE background_assets ADD COLUMN IF NOT EXISTS owner_tenant_id VARCHAR(100)",
        "ALTER TABLE background_assets ADD COLUMN IF NOT EXISTS parent_asset_id INTEGER REFERENCES background_assets(id)",
        "CREATE INDEX IF NOT EXISTS ix_background_assets_owner_tenant_id ON background_assets(owner_tenant_id)",
        # Edit-requests feature: partial re-render support at review stage.
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS segments_json JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS render_params JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS edit_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bg_r2_key_cached TEXT",
    ]
    # Each statement gets its own transaction. In Postgres, a failed statement
    # inside a transaction puts it in aborted state — subsequent execute()
    # calls are silently ignored even if caught in Python. One tx per stmt
    # ensures a failed ADD COLUMN (column already exists via Alembic) never
    # blocks the ALTER COLUMN widening that follows it.
    for sql in column_adds:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text(sql))
        except Exception as e:  # pragma: no cover — dialect-specific
            print(f"[init_db] migrate skipped: {sql} → {e}")

    # Widen VARCHAR columns to TEXT — only when not already text, to avoid
    # acquiring an ACCESS EXCLUSIVE lock on the jobs table during rolling
    # deploys (new container starts while old one still holds connections).
    _widen_column_to_text("jobs", "input_r2_key")
    _widen_column_to_text("jobs", "multipart_upload_id")

    # Cast JSON → JSONB so PostgreSQL equality operators work (required for
    # DISTINCT queries and index support). Safe: JSONB is a strict superset.
    _cast_json_to_jsonb("jobs", "umg_spec")
    _cast_json_to_jsonb("jobs", "s3_keys")
    _cast_json_to_jsonb("jobs", "youtube_data")
    _cast_json_to_jsonb("jobs", "validation_result")
    _cast_json_to_jsonb("jobs", "segments_json")
    _cast_json_to_jsonb("jobs", "render_params")
    _cast_json_to_jsonb("user_settings", "settings_json")
    _cast_json_to_jsonb("audit_log", "detail")
    _cast_json_to_jsonb("ai_provenance", "input_data_types")
    _cast_json_to_jsonb("lyrics_cache", "source_urls")


def _widen_column_to_text(table: str, column: str) -> None:
    """Run ALTER COLUMN TYPE TEXT only if the column is not already text.
    Skipping avoids an ACCESS EXCLUSIVE lock that would block during a
    rolling deploy where the previous replica is still accepting requests."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).fetchone()
        if row and row[0].lower() == "text":
            return  # already widened — no lock needed
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT"))
    except Exception as e:  # pragma: no cover
        print(f"[init_db] widen skipped: {table}.{column} → {e}")


def _cast_json_to_jsonb(table: str, column: str) -> None:
    """ALTER COLUMN TYPE JSONB only if currently json. No-op on non-PostgreSQL
    backends (SQLite in tests). Skips when already jsonb to avoid an
    unnecessary ACCESS EXCLUSIVE lock during rolling deploys."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).fetchone()
        if not row or row[0].lower() == "jsonb":
            return
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE JSONB "
                f"USING {column}::text::jsonb"
            ))
    except Exception as e:  # pragma: no cover
        print(f"[init_db] cast_json_to_jsonb skipped: {table}.{column} → {e}")


def drop_db():
    """Drop all tables. Use only in tests."""
    Base.metadata.drop_all(bind=engine)
