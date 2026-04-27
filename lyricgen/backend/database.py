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
    Integer,
    String,
    Text,
    JSON,
    create_engine,
    event,
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

engine = create_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=10,
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
            "stripe_customer_id": self.stripe_customer_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(12), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    artist = Column(String(255), nullable=False)
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

    # Cloud storage keys (when deliverables are uploaded to R2/S3)
    s3_keys = Column(JSON, nullable=True)

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
        return {
            "job_id": self.job_id,
            "status": self.status,
            "artist": self.artist,
            "filename": self.filename,
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
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
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


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables. Call once at startup."""
    Base.metadata.create_all(bind=engine)


def drop_db():
    """Drop all tables. Use only in tests."""
    Base.metadata.drop_all(bind=engine)
