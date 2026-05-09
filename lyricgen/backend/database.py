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
    String,
    Text,
    JSON,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)

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

# Pool sizing is *per-process*. With uvicorn --workers 4 + N RQ workers,
# the original (pool_size=20, max_overflow=10) easily blew past Postgres'
# default max_connections=100 under modest load and caused mid-job
# update_job() failures. Default is now ~5/5 per process (≈40 sockets at
# 4 API + 4 RQ). Override with DB_POOL_SIZE / DB_MAX_OVERFLOW for capacity
# tuning, but make sure max_connections on the DB matches:
#   max_connections >= (api_workers + rq_workers) × (pool_size + max_overflow)
# Default 8+8 per process (was 5+5). With 4 uvicorn workers + 3 RQ
# workers + the prewarm worker, peak demand is roughly:
#   4 API × (8+8) = 64 sockets
#   3 RQ × (8+8) = 48 sockets
# = 112 sockets total under burst, well below typical PG max_connections=200.
# Bumping to 8+8 absorbs concurrent /upload + /status + /download +
# update_job traffic during a 5-batch UMG flood without the previous
# fragile margin where a single slow query could starve the pool.
_DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
_DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "8"))

engine = create_engine(
    DATABASE_URL,
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=os.environ.get("SQL_ECHO", "").lower() == "true",
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
    umg_spec = Column(JSON, nullable=True)

    # File paths (relative to outputs dir)
    video_url = Column(String(500), nullable=True)
    short_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)
    umg_master_url = Column(String(500), nullable=True)
    umg_short_url = Column(String(500), nullable=True)

    # Cloud storage keys (when deliverables are uploaded to R2/S3)
    s3_keys = Column(JSON, nullable=True)

    # R2 key of the source audio uploaded by the user. Set by /transcribe
    # so /generate can hand the worker the same file without forcing the
    # browser to re-upload it (the previous flow uploaded the file twice
    # and OOMed the API container on lossless WAVs).
    input_r2_key = Column(String(500), nullable=True)

    # In-flight multipart upload id while the browser is still PUTting
    # parts directly to R2. Cleared on multipart_complete (or aborted by
    # the reaper if the upload is abandoned).
    multipart_upload_id = Column(String(255), nullable=True)

    # YouTube info
    youtube_data = Column(JSON, nullable=True)

    # Content validation (UMG Guideline 15)
    validation_result = Column(JSON, nullable=True)

    # Approval workflow (UMG compliance)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)

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
    settings_json = Column(JSON, default=dict)
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


class AuditLog(Base):
    """Tracks important actions for admin visibility."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    detail = Column(JSON, nullable=True)
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
    input_data_types = Column(JSON, nullable=True)       # ["lyrics_text", "artist_name"]
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
    source_urls = Column(JSON, nullable=True)         # list of grounding URIs
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
    ]
    with engine.begin() as conn:
        for sql in column_adds:
            try:
                conn.execute(text(sql))
            except Exception as e:  # pragma: no cover — dialect-specific
                print(f"[init_db] migrate skipped: {sql} → {e}")


def drop_db():
    """Drop all tables. Use only in tests."""
    Base.metadata.drop_all(bind=engine)
