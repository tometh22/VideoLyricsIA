"""PostgreSQL database layer with SQLAlchemy + async support."""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
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


# RQ attempt ids are persisted as concurrency tokens.  Keep enough room for
# future queue namespaces even though v6 currently uses a compact hashed id.
# PostgreSQL enforces VARCHAR limits while SQLite does not, so worker startup
# validates the deployed column against this constant explicitly.
QUALITY_ATTEMPT_ID_MAX_LENGTH = 160


def validate_quality_attempt_id_column(
    data_type: Optional[str], character_maximum_length: Optional[int],
) -> None:
    """Validate the PostgreSQL type contract for the quality CAS token."""
    normalized = str(data_type or "").strip().lower()
    if normalized == "text":
        return
    if normalized not in {"character varying", "varchar"}:
        raise RuntimeError(
            "jobs.active_quality_attempt_id must be TEXT or VARCHAR"
        )
    if int(character_maximum_length or 0) < QUALITY_ATTEMPT_ID_MAX_LENGTH:
        raise RuntimeError(
            "jobs.active_quality_attempt_id is too short: "
            f"requires >= {QUALITY_ATTEMPT_ID_MAX_LENGTH} characters"
        )


def assert_quality_attempt_id_schema_contract() -> None:
    """Fail worker startup when PostgreSQL still has the unsafe v6 width."""
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'jobs'
              AND column_name = 'active_quality_attempt_id'
        """)).first()
    if row is None:
        raise RuntimeError("jobs.active_quality_attempt_id is missing")
    validate_quality_attempt_id_column(row[0], row[1])

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
# Pools are per process. The live budget must include every uvicorn worker
# plus Worker and ShortWorker replica. With 14 DB-owning processes, a
# 100-connection Postgres instance leaves about six sockets per process after
# administrative headroom, so 4 + 2 is the conservative default.
#
# After fix/db-pool-streaming-scale: streaming endpoints (/preview,
# /download, /backgrounds/.../preview, /jobs/.../events, /download/all)
# release their pool slot before the file/SSE stream begins via
# scoped_db(). That lifts the per-process concurrency ceiling from
# "≤10 short queries + 0 streams" to short metadata queries independent of
# stream duration. 4 + 2 provides six slots per process; burst resilience
# belongs in bounded retries, not in letting every replica consume the full
# Postgres connection budget.
#
# When (not if) you migrate to a bigger DB plan or front Postgres with
# PgBouncer (see docs/SCALING.md), raise:
#   - DB_POOL_SIZE      (steady-state per-process)
#   - DB_MAX_OVERFLOW   (burst headroom per-process)
# and confirm max_connections still bounds the product above. The fix
# above changes the failure shape — the cap is now real concurrent
# short queries, not concurrent downloads.
_DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "4"))
_DB_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "2"))

def _build_pg_connect_args() -> dict:
    """psycopg2 connect_args for Railway Postgres.

    - TCP keepalives so PG notices a dead client in ~80s instead of
      Railway's 2h default. Prevents zombie idle-in-transaction sessions
      from a container that Railway killed during a failed deploy.
    - connect_timeout bounds the libpq TCP connect. WITHOUT it the connect
      has no upper bound, so engine.connect() — used by the /health probe
      (observability.py:health_snapshot) and by every fresh pool checkout —
      can hang for tens of seconds when Railway's PRIVATE NETWORKING flaps,
      blowing past the API's healthcheckTimeout=90 and getting a healthy
      replica pulled out of rotation right when the blip hits. 5s turns the
      blip into a fast, catchable error instead of a hang. Env-tunable
      (DB_CONNECT_TIMEOUT) so it can be retuned without a code change.
    """
    return {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
    }


_keepalive_args: dict = {}
if DATABASE_URL.startswith("postgresql"):
    _keepalive_args = _build_pg_connect_args()

engine = create_engine(
    DATABASE_URL,
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    # Force-recycle pool connections every 120 s. Defensive layer on top
    # of pool_pre_ping for Railway Postgres, which drops idle conns in a
    # narrow window between the pre-ping and the actual query — a race
    # we see surface as `psycopg2.OperationalError: SSL connection has
    # been closed unexpectedly` on hot endpoints (/upload-part-proxy).
    # Previously 300 s. Lower = more reconnect churn but less stale
    # surface area.
    pool_recycle=120,
    # Rollback any in-flight transaction state when a session returns to
    # the pool. Prevents a half-aborted tx from a previous request from
    # poisoning the next checkout. No-op in SQLite (used in tests); on
    # Postgres this is a cheap ROLLBACK at checkin time.
    pool_reset_on_return="rollback",
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


# ── Deliveries external DB (portal umg.genly.pro) ─────────────────────────
# El portal es prod-backed: sus /api/deliveries/* pegan al backend de PROD →
# tabla `deliveries` de la DB de PROD. Para que el operador pueda "Enviar a
# UMG" indistintamente desde staging o prod y aparezca en el mismo portal,
# las escrituras/lecturas de deliveries pueden rutearse a una DB externa
# (la de prod) vía DELIVERIES_DATABASE_URL.
#
# Solo se activa si la env var está seteada (staging). Sin ella,
# DeliveriesSessionLocal ES SessionLocal y get_deliveries_db es idéntico a
# get_db → prod y dev quedan byte-a-byte iguales que hoy. NO se corre
# create_all contra este engine: la DB externa (prod) es dueña de su schema.
DELIVERIES_DATABASE_URL = os.environ.get("DELIVERIES_DATABASE_URL", "").strip()
if DELIVERIES_DATABASE_URL.startswith("postgres://"):
    DELIVERIES_DATABASE_URL = DELIVERIES_DATABASE_URL.replace(
        "postgres://", "postgresql://", 1
    )

# El added_by_user_id de deliveries es FK NOT NULL a users.id de la DB
# destino. Un user id de staging no existe en prod → al escribir en la DB
# externa hay que mapearlo a un admin válido de prod (igual que
# scripts/migrate_deliveries_staging_to_prod.py con DEST_ADMIN_USER_ID).
_DELIVERIES_ADDED_BY = os.environ.get("DELIVERIES_ADDED_BY_USER_ID")

if DELIVERIES_DATABASE_URL:
    deliveries_engine = create_engine(
        DELIVERIES_DATABASE_URL,
        # Pool chico: es cross-project (staging→prod, conexión pública) y de
        # bajo volumen (un puñado de clicks/día). No inflar el pool de prod.
        pool_size=int(os.environ.get("DELIVERIES_DB_POOL_SIZE", "1")),
        max_overflow=int(os.environ.get("DELIVERIES_DB_MAX_OVERFLOW", "2")),
        pool_pre_ping=True,
        pool_recycle=120,
        pool_reset_on_return="rollback",
        echo=os.environ.get("SQL_ECHO", "").lower() == "true",
        connect_args=_build_pg_connect_args(),
    )
    DeliveriesSessionLocal = sessionmaker(
        bind=deliveries_engine, autoflush=False, expire_on_commit=False
    )
else:
    deliveries_engine = None
    DeliveriesSessionLocal = SessionLocal  # fallback: idéntico a hoy


def get_deliveries_db():
    """FastAPI dependency: sesión para las tablas del portal (deliveries /
    delivery_change_requests). Ruta a la DB externa si DELIVERIES_DATABASE_URL
    está seteada; si no, es la sesión local de siempre."""
    db = DeliveriesSessionLocal()
    try:
        yield db
    finally:
        db.close()


def deliveries_added_by(default_user_id):
    """user id FK-válido para la DB de deliveries. Con DB externa configurada
    usa DELIVERIES_ADDED_BY_USER_ID (un admin de prod); si no, el id local
    que venía usándose (current_user)."""
    if DELIVERIES_DATABASE_URL and _DELIVERIES_ADDED_BY:
        return int(_DELIVERIES_ADDED_BY)
    return default_user_id


# ── Peer environment DB (read-only, para atribución de costos) ────────────
# La producción gestionada para UMG corre en STAGING bajo cuentas del equipo,
# mientras que el autoservicio de Universal corre en PROD bajo tenants
# universal_*. Como además staging y prod comparten proyecto de GCP, bucket R2
# y proyecto de Railway, ninguna factura se puede separar por entorno: el
# costo real por canción SOLO sale mirando las dos bases a la vez.
#
# `PEER_DATABASE_URL` apunta al OTRO entorno (desde prod → staging; desde
# staging → prod). En staging ya existe esa conexión como
# DELIVERIES_DATABASE_URL, así que se reusa por defecto y no hay que
# configurar nada. Sin la var, los endpoints de atribución siguen andando
# con un solo entorno y lo dicen explícitamente — nunca reportan que el otro
# entorno costó $0, que sería la mentira peligrosa.
#
# SOLO LECTURA por convención: no se corre create_all contra este engine y
# ningún camino de escritura lo usa.
PEER_DATABASE_URL = os.environ.get("PEER_DATABASE_URL", "").strip() or DELIVERIES_DATABASE_URL
if PEER_DATABASE_URL.startswith("postgres://"):
    PEER_DATABASE_URL = PEER_DATABASE_URL.replace("postgres://", "postgresql://", 1)

if (
    PEER_DATABASE_URL
    and deliveries_engine is not None
    and PEER_DATABASE_URL == DELIVERIES_DATABASE_URL
):
    # Staging normally points both features at the production DB.  Reuse the
    # existing low-volume deliveries pool instead of reserving a second pool
    # (and up to three more sockets per API process) for cost attribution.
    peer_engine = deliveries_engine
    PeerSessionLocal = DeliveriesSessionLocal
elif PEER_DATABASE_URL and PEER_DATABASE_URL == DATABASE_URL:
    # An explicitly configured peer URL can also point at the local DB.  Keep
    # one pool in that case; peer_session() still returns an independent
    # short-lived Session.
    peer_engine = engine
    PeerSessionLocal = SessionLocal
elif PEER_DATABASE_URL:
    peer_engine = create_engine(
        PEER_DATABASE_URL,
        # Pool mínimo: cross-project por red pública y de uso esporádico
        # (un par de consultas cuando alguien abre el panel de costos).
        pool_size=int(os.environ.get("PEER_DB_POOL_SIZE", "1")),
        max_overflow=int(os.environ.get("PEER_DB_MAX_OVERFLOW", "2")),
        pool_pre_ping=True,
        pool_recycle=120,
        pool_reset_on_return="rollback",
        echo=os.environ.get("SQL_ECHO", "").lower() == "true",
        connect_args=_build_pg_connect_args(),
    )
    PeerSessionLocal = sessionmaker(
        bind=peer_engine, autoflush=False, expire_on_commit=False
    )
else:
    peer_engine = None
    PeerSessionLocal = None


def peer_session():
    """Sesión al otro entorno, o None si no está configurado.

    Devuelve None en vez de caer a la sesión local: mezclar los datos del
    entorno propio como si fueran los del peer duplicaría el gasto y el
    resultado se vería plausible, que es peor que no tenerlo."""
    return PeerSessionLocal() if PeerSessionLocal else None


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


@contextmanager
def scoped_deliveries_db():
    """Sesión a la DB del portal, garantizada cerrada.

    Sin `DELIVERIES_DATABASE_URL`, `DeliveriesSessionLocal` ES `SessionLocal`,
    así que una fuga acá drena el pool PRINCIPAL. Y como `pool_stats()` mide
    el pool entero, una sola sesión colgada rompe chequeos de salud y tests
    de fuga que no tienen nada que ver con el endpoint culpable. Usar esto en
    vez de abrir la sesión a mano."""
    db = DeliveriesSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def scoped_peer_db():
    """Sesión al otro entorno, o None si no está configurado.

    Pensado para `with scoped_peer_db() as peer:` seguido de
    `if peer is not None:` — el bloque corre igual cuando no hay peer, así el
    llamador no necesita un camino de cierre aparte que se pueda olvidar."""
    db = peer_session()
    try:
        yield db
    finally:
        if db is not None:
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
    # Perfil del usuario (Configuración → Perfil). full_name se muestra en
    # vez del username cuando existe; avatar_url es la key R2 del avatar
    # (servido vía GET /auth/avatar/{id} con signed URL).
    full_name = Column(String(200), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    # Cuenta de facturación compartida entre tenants. Caso Universal Music:
    # "universal_argentina" y "universal_chile" son tenants separados (no se
    # ven los videos entre sí) pero AMBOS consumen del mismo plan de 250/mes
    # → los usuarios de ambos tenants llevan billing_group="universal_music"
    # y get_plan_usage() cuenta la cuota sobre todos los tenants del grupo.
    # NULL = sin grupo (cuota por tenant, comportamiento histórico).
    billing_group = Column(String(100), nullable=True, index=True)
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    stripe_subscription_id = Column(String(255), nullable=True)

    # Dunning state for the in-app "payment failed" banner (Fase 1.5).
    # "active" = in good standing; "past_due" = a charge failed and Stripe
    # is retrying (Smart Retries) — the user keeps access during the grace
    # period but the app nudges them to fix their card. Maintained purely
    # by the Stripe webhooks in billing.py; "active" is the safe default so
    # no row shows a banner until a real failure flips it.
    billing_status = Column(String(20), nullable=False, default="active",
                            server_default="active")

    # Monotonic credential epoch embedded in every access JWT. Incrementing
    # it invalidates every previously-issued access token for this user
    # without rotating the shared JWT signing secret across a mixed fleet.
    auth_version = Column(Integer, nullable=False, default=0, server_default="0")

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
            "billing_group": self.billing_group,
            "full_name": self.full_name,
            "avatar_url": self.avatar_url,
            "is_active": self.is_active,
            "email_verified": self.email_verified,
            "ai_authorized": self.ai_authorized,
            "max_videos_per_day": self.max_videos_per_day,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "allow_overage": self.allow_overage,
            "stripe_customer_id": self.stripe_customer_id,
            "billing_status": self.billing_status or "active",
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
    # Workload isolation. Existing rows and all ordinary wizard uploads stay
    # interactive; campaign-created rows are marked batch by the server and
    # route to dedicated RQ fleets. Clients never choose this value.
    workload_class = Column(
        String(16), nullable=False, default="interactive",
        server_default="interactive", index=True,
    )
    campaign_id = Column(
        String(12), ForeignKey("batch_campaigns.id"), nullable=True, index=True,
    )
    campaign_item_id = Column(
        String(36), ForeignKey("batch_campaign_items.id"), nullable=True,
        unique=True, index=True,
    )
    artist = Column(String(255), nullable=False)
    song_title = Column(String(500), nullable=True)
    style = Column(String(50), default="oscuro")
    filename = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, default="processing", index=True)
    current_step = Column(String(50), default="whisper")
    progress = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    # Categoría del error (veo | render | upload | timing | validation |
    # timeout | reaper | unknown). La setea error_taxonomy.classify_error()
    # en los sinks del pipeline/reaper para que el dashboard de actividad
    # agrupe errores sin parsear mensajes. Nullable: rows viejas se
    # clasifican a lectura con el mismo clasificador.
    error_category = Column(String(32), nullable=True)
    # Stable machine-readable code. ``error`` is deliberately sanitized for
    # browser delivery; full exception details remain in structured logs.
    error_code = Column(String(64), nullable=True)
    # Which engine produced the lyric timing for this job: forced_align |
    # lrclib_synced | gemini_aligner | whisper. Observability so we can
    # answer "what timed this job?" without grepping logs that scroll.
    timing_source = Column(String(20), nullable=True)

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
    # Immutable identity of the currently selected source audio.  Quality
    # workers bind every result to these fields so replacing an object at the
    # same logical job can never attach evidence from audio B to lyrics A.
    input_audio_sha256 = Column(String(64), nullable=True)
    input_audio_etag = Column(Text, nullable=True)
    audio_revision = Column(
        BigInteger, default=0, nullable=False, server_default="0",
    )
    active_quality_attempt_id = Column(
        String(QUALITY_ATTEMPT_ID_MAX_LENGTH), nullable=True,
    )
    # Durable publication identities for the currently-authorized worker
    # attempts. Context-bound workers are fenced in jobs.update_job so a late
    # process from an older enqueue cannot overwrite a newer retry.
    active_pipeline_attempt_id = Column(String(36), nullable=True)
    active_transcription_attempt_id = Column(String(36), nullable=True)

    # In-flight multipart upload id while the browser is still PUTting
    # parts directly to R2. Cleared on multipart_complete (or aborted by
    # the reaper if the upload is abandoned). Uses Text because Cloudflare
    # R2 returns ~300+ char ids (the original VARCHAR(255) silently
    # truncated and crashed the commit on every >50MB upload).
    multipart_upload_id = Column(Text, nullable=True)

    # YouTube info
    youtube_data = Column(JSONB, nullable=True)
    youtube_short_data = Column(JSONB, nullable=True)

    # Content validation (UMG Guideline 15)
    validation_result = Column(JSONB, nullable=True)

    # Approval workflow (UMG compliance)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    review_notes = Column(Text, nullable=True)

    # Archivado de intentos fallidos (2026-06-10, Fase 1). Cuando un job
    # del mismo user+filename llega a `done`, los intentos previos
    # fallidos (error / rejected / validation_failed / transcription_
    # failed) se marcan aca — NUNCA se borran (audit trail UMG). La
    # historia los esconde por default detras de un toggle. NULL = visible.
    archived_at = Column(DateTime(timezone=True), nullable=True)

    # Edit requests (post-approval partial re-renders)
    # segments_json — persisted Whisper output so re-renders skip re-transcription.
    # render_params  — font/typography/motion settings used at render time.
    # edit_count     — how many partial re-renders the reviewer has requested (max 3).
    # bg_r2_key_cached — R2 key for the AI-generated background so typography-only
    #   edits can re-use it without paying for Veo again.
    segments_json = Column(JSONB, nullable=True)
    # Persisted verdict from transcription_quality.py.  Keeping the policy,
    # metrics, retry evidence and revision-scoped acknowledgement together
    # prevents API/editor/worker drift without adding a column per metric.
    transcription_quality = Column(JSONB, nullable=True)
    # Set atomically with the durable pre-human editor snapshot.  Approval
    # fails closed for these jobs when the snapshot is absent or inconsistent.
    machine_snapshot_required = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    # Final-render label-style preflight.  Kept separate from transcription
    # quality because it is bound to an encoded render, not only to segments.
    delivery_qc = Column(JSONB, nullable=True)
    # Server-owned optimistic concurrency version for editor writes.
    segments_revision = Column(BigInteger, default=0, nullable=False, server_default="0")
    # Monotonic invalidation fence for asynchronous correction learning. Every
    # later edit/change request increments it even when no observation exists
    # yet, so a delayed quality worker cannot resurrect a rejected snapshot.
    quality_learning_epoch = Column(
        BigInteger, default=0, nullable=False, server_default="0",
    )
    quality_learning_invalidated_at = Column(DateTime(timezone=True), nullable=True)
    render_params = Column(JSONB, nullable=True)
    edit_count = Column(Integer, default=0, nullable=False, server_default="0")
    bg_r2_key_cached = Column(Text, nullable=True)
    # Add-on premium "Escenas" (multi-escena). Storyboard generado por
    # scenes.build_scene_plan: { bible:{...}, sections:[...], scenes:[{ id,
    # recurrence_key, section_type, energy, movement_style, prompt, cache_token,
    # clip_cache_key, thumb_key, status }], params:{...}, degraded:{failed,total},
    # audio_duration }. NULL = job de fondo único (camino histórico). El toggle de
    # opt-in vive en render_params ("enable_scenes": true) porque es un setting de
    # render. cache_token bustea la caché Veo por escena en un regen; clip_cache_key
    # es la key R2 del clip (para GC); thumb_key alimenta el filmstrip.
    scene_plan = Column(JSONB, nullable=True)
    # Variantes: cuando este job fue creado via POST /jobs/{id}/variant,
    # parent_job_id apunta al job_id que sirvió de base (mismo audio +
    # mismo segments_json, distinto Veo prompt / concept / style).
    # NULL para jobs primarios (uploads frescos). Soft FK — si el padre
    # se borra, la variante sobrevive como job independiente. Indexado
    # para listar hijos en /jobs eficientemente.
    parent_job_id = Column(String(32), nullable=True, index=True)
    # Set by /edit when the operator triggers an edit (typography/lyrics/
    # background). The reaper uses this to detect edits that died mid-render
    # (worker killed by deploy/OOM): if a job is status="editing" and
    # editing_started_at is older than ~30 min, the worker is gone.
    # Created_at can't be used as a proxy because it represents the
    # original upload time — lyrics edits on day-old "done" jobs would
    # otherwise look ancient the instant they kicked off.
    editing_started_at = Column(DateTime(timezone=True), nullable=True)
    # Updated by jobs.update_job whenever the worker reports progress. The
    # reaper uses this to detect the "dead zone" between find_orphan_polling_jobs
    # (which requires an in-flight AIProvenance row) and find_stuck_jobs (which
    # has a 100-min created_at threshold). A worker SIGKILLed during ffmpeg or
    # moviepy compositing has no provenance to anchor the orphan sweep and 100
    # min is too long to make the user wait. Confirmed in prod 2026-05-12:
    # job 2144aacb453e killed at video/40% during a deploy, invisible to any
    # reaper for 87 min.
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    # Archive of deliverable s3_keys overwritten by a previous re-render
    # (typography/lyrics/background edit). Each entry:
    #   {"version": N, "edit_type": "lyrics", "archived_at": "ISO-8601",
    #    "keys": {"video": "...v1", "short": "...v1", ...}}
    # Populated by run_edit_pipeline right before _upload_deliverables_to_r2
    # so an operator can roll back a bad re-sync (manually fetch the .vN
    # key from R2). NULL for jobs that have never been edited.
    previous_versions = Column(JSONB, nullable=True)
    # Touched by every authenticated user action that signals "I'm still
    # working on this job" (POST /jobs/{id}/save-segments, GET /status/{id},
    # etc). find_abandoned_transcribed uses coalesce(last_user_activity_at,
    # created_at) as the staleness anchor, so an editor session that takes
    # 90 min to review 5 songs no longer gets reaped at the 30-min mark.
    # Confirmed in prod 2026-05-14: Agus lost 5 jobs to the old created_at-
    # only threshold while batch-editing.
    last_user_activity_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="jobs", foreign_keys=[user_id])
    # cascade="all, delete-orphan": deleting a Job deletes its ai_provenance
    # audit rows with it. Without this, SQLAlchemy's default is to NULL the FK
    # on the children before deleting the parent — but ai_provenance.job_id is
    # NOT NULL, so the `UPDATE ai_provenance SET job_id=NULL` raised
    # IntegrityError, poisoned the session (PendingRollbackError → HTTP 500
    # "Sin respuesta del servidor"), and left an undeleteable stale job that
    # blocked re-uploading the same audio (incident 2026-06-26, Universal).
    provenance = relationship(
        "AIProvenance", back_populates="job", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    editor_document = relationship(
        "EditorDocument", back_populates="job", uselist=False,
        cascade="all, delete-orphan",
    )
    editor_versions = relationship(
        "EditorVersion", back_populates="job", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        s3 = self.s3_keys or {}
        wants_umg = (
            (self.delivery_profile or "youtube") in ("umg", "both")
            or bool(self.umg_spec)
        )
        return {
            "job_id": self.job_id,
            "artist": self.artist,
            "song_title": self.song_title,
            "style": self.style,
            "filename": self.filename,
            "tenant_id": self.tenant_id,
            "workload_class": self.workload_class or "interactive",
            "campaign_id": self.campaign_id,
            "campaign_item_id": self.campaign_item_id,
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
            "prores_ready": (
                bool(s3.get("umg_master")) and bool(s3.get("umg_short"))
                if wants_umg else None
            ),
            "error": self.error,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "youtube": self.youtube_data,
            "youtube_short": self.youtube_short_data,
            "validation_result": self.validation_result,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "review_notes": self.review_notes,
            "edit_count": self.edit_count or 0,
            "render_params": self.render_params,
            # EditRequestPanel needs both to drive its UI: segments_json hydrates
            # the inline lyrics editor; bg_r2_key_cached gates the typography
            # mode (you can only re-render typography on top of a cached bg).
            # Without these, the panel falsely tells the user the job has no
            # lyrics and lets them attempt typography edits that the backend
            # then rejects with a raw English error.
            "segments_json": self.segments_json,
            "segments_revision": self.segments_revision or 0,
            "transcription_quality": self.transcription_quality,
            "delivery_qc": self.delivery_qc,
            "bg_r2_key_cached": self.bg_r2_key_cached,
            # Storyboard multi-escena (NULL en jobs de fondo único). El panel
            # de edición lo usa para mostrar las escenas y ofrecer "regenerar
            # escena" sin rehacer todo el video.
            "scene_plan": self.scene_plan,
            # Lineage de variantes — el JobDetail muestra un pill "Variante
            # de X" cuando este field está set. variant_count se calcula
            # en el handler (query separada para evitar lazy load N+1).
            "parent_job_id": self.parent_job_id,
            "created_at": self.created_at.timestamp() if self.created_at else None,
            "completed_at": self.completed_at.timestamp() if self.completed_at else None,
        }

    def to_list_dict(self):
        # `prores_ready` lets the dashboard / history cards show a
        # subtle badge ("✓ ProRes" vs "⏳ Generando ProRes") without
        # needing a second round-trip per row. Truthy iff the lazy
        # transcode has both deliverables on R2.
        s3 = self.s3_keys or {}
        wants_umg = (
            (self.delivery_profile or "youtube") in ("umg", "both")
            or bool(self.umg_spec)
        )
        return {
            "job_id": self.job_id,
            "status": self.status,
            "artist": self.artist,
            "song_title": self.song_title,
            "filename": self.filename,
            "workload_class": self.workload_class or "interactive",
            "campaign_id": self.campaign_id,
            "campaign_item_id": self.campaign_item_id,
            "delivery_profile": self.delivery_profile,
            "umg_spec": self.umg_spec,
            "prores_ready": (
                bool(s3.get("umg_master")) and bool(s3.get("umg_short"))
                if wants_umg else None
            ),
            # Lineage badges en la lista — "Variante" cuando parent_job_id
            # está set, "N hijos" cuando este job tiene variantes. La cuenta
            # de hijos se computa por separado (subquery en /jobs handler)
            # para evitar lazy load N+1.
            "parent_job_id": self.parent_job_id,
            "created_at": self.created_at.timestamp() if self.created_at else None,
            # Archivado Fase 1: la historia esconde archived por default.
            "archived_at": self.archived_at.timestamp() if self.archived_at else None,
            "youtube": self.youtube_data,
            "youtube_short": self.youtube_short_data,
            # Multi-escena: la tira de corrección por escena vive en JobDetail,
            # que recibe el job DESDE LA LISTA (prop), no vía fetch de detalle.
            # Sin esto, `job.scene_plan` llegaba undefined y el filmstrip NUNCA
            # aparecía aunque el video tuviera escenas (bug 2026-06-30). Solo
            # pesa en jobs con Escenas; los normales llevan null.
            "scene_plan": self.scene_plan,
        }


class BatchCampaign(Base):
    """Tenant-scoped durable container for a high-volume audio campaign."""

    __tablename__ = "batch_campaigns"
    __table_args__ = (
        Index("ix_batch_campaigns_tenant_created", "tenant_id", text("created_at DESC")),
    )

    id = Column(String(12), primary_key=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    expected_count = Column(Integer, nullable=False, default=0, server_default="0")
    default_render_params = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class BatchCampaignItem(Base):
    """One source audio registered before a Job is promoted for transcription."""

    __tablename__ = "batch_campaign_items"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sha256", name="uq_batch_item_campaign_sha"),
        UniqueConstraint("campaign_id", "technical_code", name="uq_batch_item_campaign_code"),
        Index("ix_batch_items_campaign_upload", "campaign_id", "upload_state", "ordinal"),
    )

    id = Column(String(36), primary_key=True)
    campaign_id = Column(
        String(12), ForeignKey("batch_campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(String(100), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    filename = Column(String(500), nullable=False)
    title = Column(String(500), nullable=True)
    artist = Column(String(255), nullable=True)
    technical_code = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    duration_seconds = Column(Float, nullable=True)
    sha256 = Column(String(64), nullable=False)
    metadata_error = Column(String(255), nullable=True)
    upload_state = Column(String(20), nullable=False, default="registered", server_default="registered")
    upload_key = Column(Text, nullable=True)
    multipart_upload_id = Column(Text, nullable=True)
    upload_error = Column(String(500), nullable=True)
    upload_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
    render_overrides = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BatchUploadSession(Base):
    """Short-lived, campaign-only credential exchanged from a pairing code."""

    __tablename__ = "batch_upload_sessions"

    id = Column(String(36), primary_key=True)
    campaign_id = Column(
        String(12), ForeignKey("batch_campaigns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(String(100), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_hash = Column(String(64), nullable=True, unique=True, index=True)
    code_expires_at = Column(DateTime(timezone=True), nullable=False)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class EditorDocument(Base):
    """Durable editor working copy layered over the legacy Job snapshot."""
    __tablename__ = "editor_documents"

    job_id = Column(
        String(12), ForeignKey("jobs.job_id", ondelete="CASCADE"), primary_key=True,
    )
    tenant_id = Column(String(100), nullable=False, index=True)
    current_segments = Column(JSONB, nullable=False)
    original_segments = Column(JSONB, nullable=False)
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    lock_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Browser-tab identity. User id alone cannot distinguish two tabs opened
    # by the same reviewer, so both used to believe they owned one document.
    lock_session_id = Column(String(64), nullable=True)
    lock_expires_at = Column(DateTime(timezone=True), nullable=True)
    # Raw proposal text is intentionally tenant-scoped in the editor layer;
    # analytics/quality JSON stores only hashes and aggregate diagnostics.
    quality_proposal = Column(JSONB, nullable=True)
    # Tenant-private raw hypotheses and machine decisions captured before any
    # human edit.  Unlike analytics lineage this intentionally preserves text.
    machine_evidence = Column(JSONB, nullable=True)

    job = relationship("Job", back_populates="editor_document")


class JobOutboxEvent(Base):
    """Durable publication intent committed with the owning Job mutation."""
    __tablename__ = "job_outbox_events"
    __table_args__ = (
        Index("ix_job_outbox_status_available", "status", "available_at"),
        Index("ix_job_outbox_events_dedupe_key", "dedupe_key", unique=True),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(12), ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type = Column(String(64), nullable=False, index=True)
    dedupe_key = Column(String(160), nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    available_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    processing_at = Column(DateTime(timezone=True), nullable=True)
    processing_token = Column(String(36), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(160), nullable=True)


class EditorVersion(Base):
    """Immutable editor checkpoints; approved snapshots are never pruned."""
    __tablename__ = "editor_versions"
    __table_args__ = (
        Index("ix_editor_versions_job_revision", "job_id", "revision", unique=True),
        Index("ix_editor_versions_job_created", "job_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(12), ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(String(100), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    segments = Column(JSONB, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    reason = Column(String(20), nullable=False, default="autosave")
    is_approved = Column(Boolean, nullable=False, default=False, server_default="false")
    # Hash-only lineage for the initial machine checkpoint. No audio bytes or
    # duplicate lyric content is stored here.
    provenance = Column(JSONB, nullable=True)

    job = relationship("Job", back_populates="editor_versions")


class ProductEvent(Base):
    """Privacy-safe editor telemetry; never stores lyric text or audio."""
    __tablename__ = "product_events"
    __table_args__ = (
        Index("ix_product_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_product_events_name_created", "name", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    job_id = Column(String(12), nullable=True, index=True)
    name = Column(String(80), nullable=False, index=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    properties = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class CorrectionObservation(Base):
    """Privacy-safe delta between machine output and an approved revision.

    Raw lyrics remain exclusively in ``EditorDocument``/``EditorVersion``.
    This table contains only hashes, bounded counters and acoustic/context
    features so observations can be aggregated across tenants safely.
    """
    __tablename__ = "correction_observations"
    __table_args__ = (
        Index("ix_correction_observations_tier_created", "label_tier", "created_at"),
        Index("ix_correction_observations_release_created", "pipeline_release", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    identity_hash = Column(String(64), nullable=False, unique=True, index=True)
    job_id = Column(
        String(12), ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id = Column(String(100), nullable=False, index=True)
    original_revision = Column(Integer, nullable=False)
    approved_revision = Column(Integer, nullable=False)
    approved_version_id = Column(
        String(36), ForeignKey("editor_versions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    original_hash = Column(String(64), nullable=False)
    approved_hash = Column(String(64), nullable=False)
    audio_hash = Column(String(64), nullable=True)
    pipeline_release = Column(String(64), nullable=False, index=True)
    pipeline_config_fingerprint = Column(String(64), nullable=False)
    timing_source = Column(String(64), nullable=False)
    pipeline_route = Column(String(64), nullable=False, default="unknown")
    label_tier = Column(String(20), nullable=False, default="observed", index=True)
    source_confidence = Column(String(24), nullable=False, default="exact")
    operator_hmac = Column(String(64), nullable=True)
    session_hmac = Column(String(64), nullable=True)
    artist_hmac = Column(String(64), nullable=True)
    song_hmac = Column(String(64), nullable=True)
    hmac_key_id = Column(String(32), nullable=False, default="legacy-v1")
    categories = Column(JSONB, nullable=False)
    features = Column(JSONB, nullable=False)
    metrics = Column(JSONB, nullable=False)
    active_edit_ms = Column(BigInteger, nullable=True)
    matures_at = Column(DateTime(timezone=True), nullable=True, index=True)
    trusted_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    invalidation_reason = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class QualityPattern(Base):
    """Aggregated, k-anonymous association discovered from corrections."""
    __tablename__ = "quality_patterns"

    id = Column(String(36), primary_key=True)
    fingerprint = Column(String(64), nullable=False, unique=True, index=True)
    category = Column(String(64), nullable=False, index=True)
    context_key = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False, default="emerging", index=True)
    support_jobs = Column(Integer, nullable=False, default=0)
    support_tenants = Column(Integer, nullable=False, default=0)
    support_artists = Column(Integer, nullable=False, default=0)
    baseline_rate = Column(Float, nullable=False, default=0.0)
    observed_rate = Column(Float, nullable=False, default=0.0)
    relative_risk = Column(Float, nullable=False, default=0.0)
    ci_low = Column(Float, nullable=False, default=0.0)
    ci_high = Column(Float, nullable=False, default=0.0)
    impact_seconds = Column(Float, nullable=False, default=0.0)
    evidence = Column(JSONB, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class QualityFixProposal(Base):
    """Human-governed candidate. Approval never mutates runtime config."""
    __tablename__ = "quality_fix_proposals"
    __table_args__ = (
        Index(
            "ix_quality_fix_proposals_idempotency",
            "last_idempotency_key", unique=True,
        ),
    )

    id = Column(String(36), primary_key=True)
    pattern_id = Column(
        String(36), ForeignKey("quality_patterns.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    proposal_type = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    hypothesis = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="draft", index=True)
    version = Column(Integer, nullable=False, default=1)
    candidate_config = Column(JSONB, nullable=False)
    expected_impact = Column(JSONB, nullable=False)
    validation_summary = Column(JSONB, nullable=True)
    ready_artifact = Column(JSONB, nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    decision_reason = Column(String(500), nullable=True)
    last_idempotency_key = Column(String(100), nullable=True)
    action_idempotency_keys = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class QualityExperimentRun(Base):
    """Immutable record of one no-render baseline/candidate evaluation."""
    __tablename__ = "quality_experiment_runs"

    id = Column(String(36), primary_key=True)
    proposal_id = Column(
        String(36), ForeignKey("quality_fix_proposals.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    status = Column(String(24), nullable=False, default="queued", index=True)
    baseline_config_hash = Column(String(64), nullable=True)
    candidate_config_hash = Column(String(64), nullable=False)
    benchmark_report_hash = Column(String(64), nullable=True)
    metrics = Column(JSONB, nullable=False)
    failure_reason = Column(String(500), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class Delivery(Base):
    # Versions exposed on the UMG deliverables portal (umg.genly.pro).
    # Replaces the previous static items.json workflow — admins click
    # "Enviar a UMG" on an approved job and a row lands here; the portal
    # fetches the list dynamically and signs R2 URLs on demand.
    __tablename__ = "deliveries"
    __table_args__ = (
        # The portal lists active (non-removed) deliveries grouped by
        # song. Composite index supports the listing query without a
        # full scan once we have a few hundred entries.
        Index("ix_deliveries_active", "removed_at", "added_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # job_id references the short hash on jobs.job_id, NOT the integer PK.
    # No FK constraint because some legacy job rows have been hard-deleted
    # but their R2 files remain — we want those to still be deliverable.
    job_id = Column(String(12), nullable=False, index=True)
    label = Column(String(120), nullable=False, default="Renderizado")
    # JSON list of file_type identifiers the portal should expose for this
    # entry. Matches keys in Job.s3_keys (umg_master, umg_short, video,
    # short, thumbnail). Stored as a list so future deliveries with a
    # different mix (e.g. master-only) don't need a schema change.
    file_types = Column(JSONB, nullable=False)
    # Byte size per file type, captured at publish time (the publish step
    # already HEADs R2 to validate the files exist). The portal listing
    # reads these instead of HEAD'ing R2 on every page load — so it's
    # instant for the first visitor, no cold-cache penalty. Shape:
    # {"video": 12345, "umg_master": 678, ...}. Null on pre-existing rows
    # (the listing falls back to a Redis-cached HEAD for those).
    file_sizes = Column(JSONB, nullable=True)
    # Snapshot of song metadata at publish time. Job rows can be soft-
    # deleted later or have their artist/title corrected; the portal
    # should keep showing whatever was approved at the moment of publish.
    artist_snapshot = Column(String(255), nullable=False)
    song_title_snapshot = Column(String(500), nullable=False)
    tenant_snapshot = Column(String(100), nullable=False)
    frame_size_snapshot = Column(String(20), nullable=True)  # HD | UHD-4K | DCI-4K
    added_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    added_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Soft delete: keeps the row + R2 files but hides from the portal.
    # Hard delete + R2 cleanup is intentionally not implemented yet —
    # an accidental delete from the portal would otherwise be unrecoverable.
    removed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    removed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Portal-side approval (set by UMG via the "Aprobar" button).
    # approved_by_label is free-form because the portal authenticates via
    # a shared password, not per-user — we record "UMG" by default and
    # leave room for per-user portal logins to write usernames here later.
    approved_at = Column(DateTime(timezone=True), nullable=True, index=True)
    approved_by_label = Column(String(120), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "label": self.label,
            "file_types": list(self.file_types or []),
            "artist": self.artist_snapshot,
            "song_title": self.song_title_snapshot,
            "tenant": self.tenant_snapshot,
            "frame_size": self.frame_size_snapshot,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "approved_by_label": self.approved_by_label,
        }


class DeliveryChangeRequest(Base):
    # Free-form change requests UMG (or any portal user) leaves on a
    # specific delivery version. UI affordance: "Solicitar cambios"
    # button on the song detail modal opens a textarea; submit lands here.
    # The operator picks them up in the GenLy admin, acts on them
    # (re-render, edit lyrics, etc.), and marks them resolved.
    __tablename__ = "delivery_change_requests"
    __table_args__ = (
        Index("ix_dcr_pending", "resolved_at", "submitted_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    delivery_id = Column(
        Integer, ForeignKey("deliveries.id"), nullable=False, index=True,
    )
    # Free text. Capped at 5000 chars by the endpoint (not the column)
    # so we can relax the limit later without a migration.
    comment = Column(Text, nullable=False)
    submitted_at = Column(
        DateTime(timezone=True), default=utcnow, nullable=False,
    )
    # Set when the operator marks the request handled (re-rendered,
    # edited, dismissed). Null = still pending.
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id = Column(
        Integer, ForeignKey("users.id"), nullable=True,
    )
    resolution_note = Column(Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "comment": self.comment,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_note": self.resolution_note,
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


class UserDriveTokens(Base):
    """OAuth tokens para la integración Google Drive, uno por user.

    El refresh_token va Fernet-encrypted at rest (DRIVE_TOKEN_ENCRYPTION_KEY
    en env). Access tokens son short-lived (~1h) y se derivan del refresh
    en cada uso → no se persisten.

    Scope que usamos: `drive.file` — Drive solo le da acceso a archivos
    que la app crea, no a todo el Drive del user. Evita Google app
    verification y mantiene el blast radius chico.
    """
    __tablename__ = "user_drive_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    encrypted_refresh_token = Column(String(2048), nullable=False)
    scope = Column(String(500), nullable=False)
    google_email = Column(String(255), nullable=True)  # display only en Settings
    connected_at = Column(DateTime(timezone=True), default=utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)


class SystemYoutubeToken(Base):
    """Token OAuth de la cuenta de YouTube del SISTEMA (global, singleton).

    A diferencia de UserDriveTokens (uno por user), YouTube usa una única
    cuenta central a la que suben los videos de todos los tenants. Por eso
    es singleton: siempre hay 0 o 1 fila.

    El token completo (access + refresh + client info, formato compatible
    con google.oauth2.credentials.Credentials) va Fernet-encrypted at rest,
    reusando DRIVE_TOKEN_ENCRYPTION_KEY. Por qué DB y no archivo: el
    filesystem de Railway es efímero (se borra en cada deploy), así que
    persistir acá es lo que hace que la conexión a YouTube sobreviva los
    redeploys en vez de obligar a reconectar cada vez.
    """
    __tablename__ = "system_youtube_token"

    id = Column(Integer, primary_key=True, autoincrement=True)
    encrypted_token_json = Column(Text, nullable=False)
    channel_id = Column(String(255), nullable=True)
    channel_name = Column(String(255), nullable=True)
    channel_thumbnail = Column(String(500), nullable=True)
    connected_by_user_id = Column(Integer, nullable=True)  # auditoría
    connected_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DriveTransfer(Base):
    """Track de una transferencia R2 → Google Drive (uno por click de
    'Guardar en Drive'). El worker que corre rclone va updateando
    progress_pct + bytes_transferred mientras corre.
    """
    __tablename__ = "drive_transfers"

    # uuid hex (12 chars como job_id). Suficiente para evitar collisions.
    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id = Column(String(12), ForeignKey("jobs.job_id"), nullable=False, index=True)
    file_type = Column(String(20), nullable=False)  # "umg_master" | "umg_short" | "video" | "short"
    status = Column(String(20), nullable=False, default="queued", index=True)
    # queued → running → done | error
    progress_pct = Column(Integer, default=0)
    bytes_transferred = Column(BigInteger, default=0)
    bytes_total = Column(BigInteger, default=0)
    drive_file_id = Column(String(100), nullable=True)
    web_view_link = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


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


class CreditGrant(Base):
    """Créditos de regalo (promos como el lanzamiento de Escenas).

    Un pool por CUENTA que se consume ANTES del cupo del plan, con
    vencimiento. NO se decrementa en vivo: `auth.get_plan_usage()` calcula
    cuánto se consumió contando los videos aprobados desde `granted_at` (con
    el mismo peso de créditos que la cuota: normal=1, Escenas=N). Así
    reject/un-approve revierten el consumo solos —igual que la cuota— sin
    tocar ninguna fila acá.

    Scope (igual que la cuota): si la cuenta tiene `billing_group` (ej.
    Universal con tenants AR/CL), el grant es del grupo y lo comparten todos
    sus tenants. Si no, es por `tenant_id`. Se setea UNO de los dos.

    `create_all()` la crea en el boot (no requiere migración Alembic).
    """
    __tablename__ = "credit_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Scope de la cuenta: exactamente uno de los dos.
    billing_group = Column(String(100), nullable=True, index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    amount = Column(Integer, nullable=False)            # créditos otorgados
    reason = Column(String(100), nullable=False, default="escenas_launch", index=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    # NULL = sin vencimiento. La promo de lanzamiento setea now + TTL.
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Kill-switch sin borrar la fila (auditable).
    revoked = Column(Boolean, default=False, nullable=False, server_default="false")

    def to_dict(self):
        return {
            "id": self.id,
            "billing_group": self.billing_group,
            "tenant_id": self.tenant_id,
            "amount": self.amount,
            "reason": self.reason,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
        }


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


class UserSession(Base):
    """Sesiones de uso de la app, alimentadas por POST /telemetry/heartbeat.

    Backs el "tiempo en la app" y el "en línea ahora" del tab Actividad del
    AdminPanel. El frontend manda un heartbeat por minuto mientras la
    pestaña está visible; el endpoint extiende la sesión abierta
    (last_seen_at) o crea una nueva cuando el gap supera los 30 min.
    Tiempo en app = SUM(last_seen_at - started_at) por ventana.

    Gateado por TELEMETRY_ENABLED (env, default off) — sin la flag el
    endpoint no escribe y el frontend ni siquiera manda heartbeats.
    """
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_started", "user_id", text("started_at DESC")),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Desnormalizado para poder filtrar sesiones por tenant sin join.
    tenant_id = Column(String(100), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Backs el "online now" (last_seen < 3 min) y el sweep de retención.
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    # Cantidad de heartbeats acumulados — distingue una sesión real de un
    # ping suelto y sirve de sanity check (heartbeats ≈ minutos visibles).
    heartbeats = Column(Integer, default=1, nullable=False, server_default="1")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "heartbeats": self.heartbeats or 0,
        }


class UiEvent(Base):
    """Eventos de comportamiento de UI (wizard), alimentados por POST /telemetry/events.

    Backs el funnel del wizard del panel Insights: qué paso alcanza cada
    sesión de creación, dónde se abandona, qué fondos se seleccionan, etc.
    El frontend manda batches best-effort (telemetryTrack.js); el endpoint
    whitelistea event_type y acota event_data, así que la tabla solo
    contiene eventos conocidos con payloads chicos.

    Gateado por TELEMETRY_ENABLED igual que user_sessions — sin la flag el
    endpoint no escribe. Retención/purga: pendiente (volumen acotado por
    cap de batch + whitelist; revisar cuando crezca).
    """
    __tablename__ = "ui_events"
    __table_args__ = (
        Index("ix_ui_events_user_created", "user_id", text("created_at DESC")),
        Index("ix_ui_events_type_created", "event_type", text("created_at DESC")),
    )

    # BigInteger en Postgres (la tabla puede crecer mucho); variant Integer
    # en SQLite porque solo INTEGER PRIMARY KEY autoincrementa ahí (tests).
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Desnormalizado para filtrar por tenant sin join (mismo criterio que
    # user_sessions).
    tenant_id = Column(String(100), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    event_data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "event_type": self.event_type,
            "event_data": self.event_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LoginSession(Base):
    """Sesión de login = un dispositivo/navegador con un token activo.

    Distinta de UserSession (eso es telemetría de tiempo-en-app). Esta
    backs "Configuración → Dispositivos": ver dónde estás logueado y
    cerrar sesión remota.

    El JWT lleva un `jti` (uuid) que apunta a la fila acá. get_current_user
    valida que la fila exista y no esté revocada → revocar = setear
    revoked_at y ese token queda 401 en su próximo request, aunque el JWT
    en sí siga sin expirar. Tokens viejos sin jti (emitidos antes de esta
    feature) se aceptan sin chequeo y expiran solos.
    """
    __tablename__ = "login_sessions"
    __table_args__ = (
        Index("ix_login_sessions_user_active", "user_id", "revoked_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    jti = Column(String(64), unique=True, nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(400), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, current_jti=None):
        return {
            "id": self.id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "revoked": self.revoked_at is not None,
            "current": current_jti is not None and self.jti == current_jti,
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
    # Only pre-submit Veo budget reservations use this lease. Once the worker
    # crosses the provider POST boundary it is cleared and the row remains in
    # the rolling ceiling as actual/ambiguous spend via response_summary.
    reservation_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    job = relationship("Job", back_populates="provenance")


class VeoBudgetLedger(Base):
    """Minimal spend tombstones that survive deletion of failed jobs.

    The rolling Veo ceiling cannot depend solely on ``ai_provenance`` because
    operators may hard-delete stuck/failed jobs and their provenance rows.
    We retain no catalogue metadata or prompt here: ``scope_hash`` is a
    one-way tenant+song identity and ``source_provenance_id`` only makes the
    archival insert idempotent.
    """
    __tablename__ = "veo_budget_ledger"
    __table_args__ = (
        Index(
            "ix_veo_budget_ledger_scope_call_at",
            "scope_hash",
            "provider_call_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_hash = Column(String(64), nullable=False)
    source_provenance_id = Column(Integer, unique=True, nullable=False)
    provider_call_at = Column(DateTime(timezone=True), nullable=False)
    archived_at = Column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True,
    )


class DeletedJobLyricsArchive(Base):
    """Best-effort copy of an operator's hand-corrected lyrics, taken right
    before a stuck/failed Job row (and its ON DELETE CASCADE children
    editor_documents/editor_versions) is hard-deleted via delete_job /
    bulk_delete_jobs.

    Incident (audited 2026-08-24): 137 jobs with real lyric corrections were
    hard-deleted via the operator cleanup flow with no recoverable trace of
    which song they belonged to — editor_documents/editor_versions cascade
    with the Job, and the Job row itself (artist/song_title) is gone by the
    time anyone notices. This table exists solely to survive that delete:

    - `job_id` is a PLAIN string, deliberately with NO ForeignKey to
      jobs.job_id — the whole point is that this row outlives the job.
    - `artist`/`song_title` are copied from the Job BEFORE it's deleted,
      which is exactly the piece of context the 137 lost rows are missing.

    Never blocks deletion: written best-effort in the same transaction as
    the delete, and only when there's actually something to archive (a
    non-empty editor_documents.current_segments, or at least one
    editor_versions row). Jobs nobody ever touched in the editor produce no
    row here — this is a safety net for lost human work, not a full audit
    log of every deletion.
    """
    __tablename__ = "deleted_job_lyrics_archive"
    __table_args__ = (
        Index(
            "ix_deleted_job_lyrics_archive_archived_at", "archived_at",
        ),
        Index(
            "ix_deleted_job_lyrics_archive_tenant_job",
            "tenant_id",
            "job_id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(12), nullable=False, index=True)
    tenant_id = Column(String(100), nullable=False)
    artist = Column(String(255), nullable=True)
    song_title = Column(String(500), nullable=True)
    job_status_at_deletion = Column(String(20), nullable=False)
    segments = Column(JSONB, nullable=False)
    # "editor_documents" | "editor_versions" — which table the segments were
    # recovered from. editor_documents.current_segments is preferred (it's
    # the operator's latest working copy); editor_versions is the fallback
    # when there's no live document but at least one saved checkpoint.
    source = Column(String(20), nullable=False)
    archived_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Nullable, deliberately no ForeignKey (unlike AuditLog.user_id): this
    # archive must survive independently of both the job AND the acting
    # user's row, and users.id has no ON DELETE behavior defined today —
    # a strict FK here would risk a future user deletion blocking on, or
    # cascading into, lyrics-recovery history that has nothing to do with
    # user-account lifecycle.
    deleted_by_user_id = Column(Integer, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "artist": self.artist,
            "song_title": self.song_title,
            "job_status_at_deletion": self.job_status_at_deletion,
            "segments": self.segments,
            "source": self.source,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "deleted_by_user_id": self.deleted_by_user_id,
        }


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


class TranscriptionCache(Base):
    """Cache de outputs de inferencia ASR (whisperX en Replicate) keyed
    por audio content hash + engine + language + lyrics_hint hash.

    Motivación 2026-05-25 (UMG dry-run): el operador re-subió el mismo
    archivo 2x durante diagnóstico y cada vez whisperX corrió 75-180s
    sin razón. Mismo audio + misma config → mismo output determinístico.
    Cachear evita la 2da llamada a Replicate (~$0.005 + 75-180s).

    Diseño:
    - `cache_key` encode las variables que afectan el output: audio
      content hash + engine + language + lyrics_hint hash (porque el
      initial_prompt cambia la transcripción).
    - `segments` guarda el JSON de output del modelo (sin tocar) —
      caller hace json.loads.
    - Sin TTL hard (reaper barre después por age si crece la tabla;
      por ahora un cache hit ahorra ~$0.005 + 75 s, valor positivo).
    """
    __tablename__ = "transcription_cache"

    cache_key = Column(String(64), primary_key=True)
    audio_hash = Column(String(32), nullable=False, index=True)
    engine = Column(String(20), nullable=False)           # "whisperx" | "fa" (futuro)
    language = Column(String(8), nullable=True)
    lyrics_hint_hash = Column(String(16), nullable=True)
    segments = Column(Text, nullable=False)               # JSON serializado
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)


class SalesLead(Base):
    """Public sales/contact form submissions from the landing page.
    Captured by the unauthenticated POST /api/leads endpoint and also
    emailed to the sales inbox. Created by create_all() on boot — no
    Alembic migration needed (mirrors how new tables land here)."""
    __tablename__ = "sales_leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    company = Column(String(255), nullable=True)
    email = Column(String(255), nullable=False, index=True)
    volume = Column(String(100), nullable=True)
    message = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)


class CostSnapshot(Base):
    """Monthly invoiced cost per provider, pulled by billing_sources.py.

    Provider billing APIs only expose a rolling window (Railway shows the
    open cycle, GitHub the current billing period, Replicate paginates
    predictions that eventually age out), so a month that is never
    snapshotted becomes unrecoverable. This table is the durable record:
    one row per (period, source), refreshed by POST /admin/cost/refresh
    and frozen after that source's post-close finalization window. Captures
    made while usage is still accruing remain provisional.

    `amount_usd` is nullable on purpose — a source that was not configured
    yet must be distinguishable from one that genuinely cost $0, otherwise
    a missing credential silently reads as free. See `status`.
    """
    __tablename__ = "cost_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    period = Column(String(7), nullable=False, index=True)   # "YYYY-MM"
    source = Column(String(32), nullable=False)              # gcp | railway | ...
    amount_usd = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default="ok")
    detail = Column(Text, nullable=True)
    is_estimate = Column(Boolean, nullable=False, default=False)
    breakdown = Column(JSONB, nullable=True)
    fetched_at = Column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("period", "source", name="uq_cost_snapshot_period_source"),
    )


class CostCollectionRun(Base):
    """Did we manage to ask provider X about day D — and what happened.

    THE POINT OF THIS TABLE: without it, a day the collector could not fetch
    is indistinguishable from a cheap day. `cost_daily` would simply have no
    rows for it, the month total would quietly drop, and the panel would
    render a dip that looks like good news. The whole reason the cost panel
    exists is to be trusted without a monthly manual audit, so "failed
    silently" is a worse outcome than "no panel".

    The row is written with status='pending' BEFORE the provider call. A
    crash mid-call therefore leaves evidence; a missing row means the
    collector never even got to that day, which is itself the alarm.
    """
    __tablename__ = "cost_collection_runs"

    day = Column(Date, primary_key=True)
    source = Column(String(32), primary_key=True)
    # pending | ok | error | not_configured
    status = Column(String(20), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_cost_runs_status_day", "status", "day"),
    )


class CostDaily(Base):
    """One raw cost fact, at the finest grain the provider will give us.

    DESIGN RULE — the collector stores the provider's RAW granularity; every
    business rule is applied at read time. That rule is not stylistic, it is
    what keeps the numbers correct:

      * R2's free allowances (10 GB-month, 1M class-A, 10M class-B) are a
        function of the MONTH. Subtracting 1M class-A per day yields $0 every
        day even when the month blows past the million.
      * Railway's plan minimum is `max(metered, 20)` over the month — no
        per-day split reproduces it.
      * OpenAI's line-item filter changes over time (July's `gpt-4o-mini` was
        not ours; August's is). Filtering at collect time freezes a wrong
        answer into history forever.

    `amount_usd` is NULLABLE for the same reason as CostSnapshot: a source we
    could not reach must never read as $0.

    `grain` exists because a monthly-only fact (flat subscriptions, or the
    invoice total itself) must never be summed into a day range. Mixing the
    two in one table without it is how a month gets counted twice.

    Writes are DELETE-then-INSERT per (day, source, grain) inside one
    transaction, never upsert: a dimension that stops being reported (a SKU
    reversed to a credit, a tenant that went quiet) must disappear, or
    `SUM(dims)` drifts above `total` forever.
    """
    __tablename__ = "cost_daily"

    day = Column(Date, primary_key=True)
    source = Column(String(32), primary_key=True)
    grain = Column(String(8), primary_key=True)        # day | month
    dim_type = Column(String(16), primary_key=True)    # total | sku | service | line_item | job
    dim_value = Column(String(255), primary_key=True)

    qty = Column(Float, nullable=True)                 # cantidad cruda del proveedor
    unit = Column(String(32), nullable=True)           # GB-min, requests, seconds...
    amount_usd = Column(Float, nullable=True)

    # fijo | variable | stock — separa el piso mensual del costo marginal por
    # video. Sin esto el "$/video" baja al subir el volumen y hace parecer
    # una mejora lo que en realidad recorta la ganancia absoluta.
    cost_behavior = Column(String(10), nullable=True)

    basis = Column(String(16), nullable=False, default="measured")  # measured|allocated|invoice_manual
    basis_detail = Column(Text, nullable=True)
    is_estimate = Column(Boolean, nullable=False, default=False)
    fetched_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_cost_daily_day_source", "day", "source"),
        Index("ix_cost_daily_dim", "dim_type", "dim_value"),
    )


# ---------------------------------------------------------------------------
# Gold corpus annotation (validator calibration — 50-song blind double-
# annotation project, see corpus.py). Deliberately separate from Job: these
# rows are never client jobs, never render anything, and must never be
# joined into tenant-scoped job queries by accident.
# ---------------------------------------------------------------------------

class CorpusSong(Base):
    """One gold-corpus song. The audio itself is not duplicated here — it
    already lives in R2 (same bucket other jobs use); this row is just the
    pointer + metadata an admin registers once via POST /admin/corpus/songs."""
    __tablename__ = "corpus_songs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artist = Column(String(255), nullable=False)
    title = Column(String(500), nullable=False)
    # R2 key of the source audio (mp3/wav), same object-storage mechanism
    # jobs.input_r2_key uses. Text (not VARCHAR) — mirrors jobs.input_r2_key,
    # which was widened after R2 handed back keys longer than 255 chars.
    audio_r2_key = Column(Text, nullable=False)
    audio_sha256 = Column(String(64), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    # Pre-review precarga (see corpus_reference.py): cleaned-down copy of
    # the already-human-reviewed editor_documents.current_segments for the
    # delivered job this song was copied from — [{start, end, text,
    # event_type}], event_type always "lexical" (production data has no
    # vocalization/mixed classification). NULL means "start empty", either
    # because this is a control song (see is_control) or because no
    # reviewed editor_documents row could be matched for it.
    reference_segments = Column(JSONB, nullable=True)
    # True for the handful of songs deliberately held out with NO
    # precarga (marked "CONTROL:" in `notes`) — the check that annotators
    # do just as well starting from zero as they do reviewing a precarga.
    # Never combine this with a populated reference_segments: the backfill
    # in corpus_reference.py enforces that, and _get_or_create_own_annotation
    # in corpus.py double-checks it before seeding a draft.
    is_control = Column(Boolean, nullable=False, default=False, server_default="false")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    def to_dict(self, *, include_admin_fields: bool = False):
        d = {
            "id": self.id,
            "artist": self.artist,
            "title": self.title,
            "duration_seconds": self.duration_seconds,
            "notes": self.notes,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_admin_fields:
            # Admin-only: revealing `is_control` to an annotator would tell
            # her which song is the blind control, defeating its purpose.
            # Annotator-facing responses must never call to_dict(True).
            d["is_control"] = self.is_control
            d["has_reference_segments"] = bool(self.reference_segments)
        return d


class CorpusAnnotatorToken(Base):
    """Magic-link identity for one non-technical annotator. Knowledge of the
    `token` string IS the auth — no username/password, no login screen, and
    (deliberately) no expiry: the annotator is a real person doing manual
    work over days/weeks and must never get logged out mid-song. Admin
    revokes access by flipping `is_active` off, not by rotating a secret."""
    __tablename__ = "corpus_annotator_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }


class CorpusAnnotation(Base):
    """One annotator's segment markup for one corpus song — draft or
    submitted. Blind by construction: every token-scoped endpoint in
    corpus.py resolves `annotator_token_id` from the caller's OWN URL
    token and filters by it server-side. There is no endpoint that accepts
    an arbitrary annotator id, so annotator A's request can never reach
    annotator B's row for the same song — the only surface that can see
    both sides at once is the admin-only comparison endpoint."""
    __tablename__ = "corpus_annotations"
    __table_args__ = (
        UniqueConstraint(
            "song_id", "annotator_token_id",
            name="uq_corpus_annotation_song_annotator",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    song_id = Column(
        Integer, ForeignKey("corpus_songs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    annotator_token_id = Column(
        Integer, ForeignKey("corpus_annotator_tokens.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # List of {start, end, text, event_type} — event_type in
    # (lexical, vocalization, mixed). Validated at the API boundary, not
    # here — the column itself just carries whatever the annotator saved.
    segments = Column(JSONB, nullable=False, default=list)
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    # True when this row's initial `segments` came from the song's
    # reference_segments precarga (set once, at row creation, in
    # corpus._get_or_create_own_annotation — never touched again, even if
    # the annotator later empties every line). Lets the frontend keep
    # showing the "this one already has a first pass — verify it" note on
    # every later open of the song, not just the very first one.
    seeded_from_reference = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "song_id": self.song_id,
            "segments": self.segments or [],
            "status": self.status,
            "seeded_from_reference": self.seeded_from_reference,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def assert_runtime_schema_contract() -> None:
    """Fail fast when Alembic has not installed the model contract.

    Production-like services must never repair schema while accepting traffic:
    startup DDL races rolling deploys and a swallowed lock timeout leaves a
    superficially healthy process with an unusable database.
    """
    inspector = inspect(engine)
    present_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    missing_tables = sorted(expected_tables - present_tables)
    missing_columns = []
    for table_name in sorted(expected_tables & present_tables):
        present = {item["name"] for item in inspector.get_columns(table_name)}
        expected = {column.name for column in Base.metadata.tables[table_name].columns}
        missing_columns.extend(
            f"{table_name}.{column}" for column in sorted(expected - present)
        )
    if missing_tables or missing_columns:
        detail = ", ".join(
            [*(f"table:{name}" for name in missing_tables), *missing_columns]
        )
        raise RuntimeError(f"database schema is behind Alembic head: {detail}")


def init_db():
    """Create all tables. Call once at startup.

    Also runs lightweight idempotent column-add migrations so deploys
    that pre-date a new column (e.g. users.allow_overage) self-heal on
    boot without an Alembic setup. SQLAlchemy's create_all only creates
    missing TABLES — it ignores missing COLUMNS on existing tables.
    """
    environment = os.environ.get("ENVIRONMENT", "production").strip().lower()
    if environment in {"prod", "production", "staging"}:
        assert_runtime_schema_contract()
        return
    Base.metadata.create_all(bind=engine)
    _migrate_user_columns()


def _migrate_user_columns():
    """Add columns to the `users` table if they're missing.
    Only runs on PostgreSQL — `create_all()` already creates the full
    schema from scratch in SQLite (test) environments, so there are no
    missing columns to patch. The `IF NOT EXISTS` / `JSONB` / `TIMESTAMPTZ`
    syntax used here is PostgreSQL-specific anyway."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    column_adds = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS allow_overage BOOLEAN DEFAULT FALSE NOT NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS umg_short_url VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS song_title VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_r2_key VARCHAR(500)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_audio_sha256 VARCHAR(64)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_audio_etag TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audio_revision BIGINT DEFAULT 0 NOT NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS active_quality_attempt_id VARCHAR(160)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS multipart_upload_id VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS timing_source VARCHAR(20)",
        # Library exclusivity (UMG): tenant-owned and variation-parent references.
        "ALTER TABLE background_assets ADD COLUMN IF NOT EXISTS owner_tenant_id VARCHAR(100)",
        "ALTER TABLE background_assets ADD COLUMN IF NOT EXISTS parent_asset_id INTEGER REFERENCES background_assets(id)",
        "CREATE INDEX IF NOT EXISTS ix_background_assets_owner_tenant_id ON background_assets(owner_tenant_id)",
        # Edit-requests feature: partial re-render support at review stage.
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS segments_json JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcription_quality JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS machine_snapshot_required BOOLEAN DEFAULT FALSE NOT NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivery_qc JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS render_params JSONB",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS edit_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bg_r2_key_cached TEXT",
        # Reaper signal for "edit died mid-render". Set by /edit handler,
        # read by reaper.find_abandoned_edits to detect worker deaths
        # without relying on the original created_at (which would be stale
        # for lyrics edits on day-old "done" jobs).
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS editing_started_at TIMESTAMPTZ",
        # Reaper signal for the "stalled render" dead-zone. Set by
        # jobs.update_job() whenever the worker reports progress; read by
        # reaper.find_stalled_renders to catch processing jobs whose worker
        # died in a non-AI step (ffmpeg, moviepy, R2 upload).
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ",
        # User-side staleness anchor. The reaper coalesces this with
        # created_at to decide if a transcribed_pending job is abandoned.
        # Filled by /save-segments and any other authenticated touch.
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_user_activity_at TIMESTAMPTZ",
        # Variantes: POST /jobs/{id}/variant crea un job nuevo que hereda
        # audio + segments del padre pero re-genera el Veo background.
        # parent_job_id apunta al padre. Soft FK (no REFERENCES) para que
        # delete del padre no rompa la variante. Indexado para que el
        # /jobs liste con `variant_count` eficientemente.
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS parent_job_id VARCHAR(32)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_parent_job_id ON jobs(parent_job_id)",
        # Archive of previous deliverable s3_keys overwritten by a partial
        # re-render (lyrics/typography/background edit). Populated by
        # run_edit_pipeline before _upload_deliverables_to_r2 so an operator
        # can roll back a bad re-sync — the {key}.vN object stays in R2.
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS previous_versions JSONB",
        # Portal-side approval state. Added 2026-05-18 to back the
        # `POST /api/deliveries/{id}/approve` endpoint that the portal v3
        # frontend was already calling against a 404 (the endpoint was
        # missing from the backend, UMG saw "No se pudo aprobar: Not
        # Found"). Two columns: approved_at (timestamp) and
        # approved_by_label (free-form, defaults to "UMG").
        "ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS file_sizes JSONB",
        "ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_deliveries_approved_at ON deliveries(approved_at)",
        "ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS approved_by_label VARCHAR(120)",
        # Categoría del error para el dashboard de actividad (PR telemetría).
        # Se setea en los sinks de error del pipeline/reaper vía
        # error_taxonomy.classify_error(). Espejo de la migración Alembic
        # de user_sessions + error_category (mismo patrón que
        # last_user_activity_at: la tabla nueva la crea create_all()).
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS error_category VARCHAR(32)",
        # Cuenta de facturación compartida entre tenants (caso Universal
        # Music AR + CL con un solo plan de 250/mes). Espejo de la migración
        # Alembic de billing_group.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_group VARCHAR(100)",
        "CREATE INDEX IF NOT EXISTS ix_users_billing_group ON users(billing_group)",
        # Perfil (Configuración → Perfil). Espejo de la migración de
        # full_name/avatar_url. login_sessions la crea create_all().
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(200)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
        # Credential epoch used to revoke access JWTs without rotating the
        # fleet-wide signing secret. Alembic remains the primary migration;
        # this mirrors the repository's startup self-heal convention.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS segments_revision BIGINT DEFAULT 0 NOT NULL",
        "ALTER TABLE editor_documents ADD COLUMN IF NOT EXISTS quality_proposal JSONB",
        "ALTER TABLE editor_documents ADD COLUMN IF NOT EXISTS machine_evidence JSONB",
        "ALTER TABLE job_outbox_events ADD COLUMN IF NOT EXISTS processing_at TIMESTAMPTZ",
        "ALTER TABLE job_outbox_events ADD COLUMN IF NOT EXISTS processing_token VARCHAR(36)",
        "ALTER TABLE job_outbox_events ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS youtube_short_data JSONB",
    ]
    # Each statement gets its own transaction. In Postgres, a failed statement
    # inside a transaction puts it in aborted state — subsequent execute()
    # calls are silently ignored even if caught in Python. One tx per stmt
    # ensures a failed ADD COLUMN (column already exists via Alembic) never
    # blocks the ALTER COLUMN widening that follows it.
    for sql in column_adds:
        try:
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text("SET LOCAL lock_timeout = '3s'"))
                conn.execute(text(sql))
        except Exception as e:  # pragma: no cover — dialect-specific
            print(f"[init_db] migrate skipped: {sql} → {e}")

    # Widen VARCHAR columns to TEXT — only when not already text, to avoid
    # acquiring an ACCESS EXCLUSIVE lock on the jobs table during rolling
    # deploys (new container starts while old one still holds connections).
    _widen_column_to_text("jobs", "input_r2_key")
    _widen_column_to_text("jobs", "multipart_upload_id")
    _widen_varchar_column("jobs", "active_quality_attempt_id", 160)

    # Cast JSON → JSONB so PostgreSQL equality operators work (required for
    # DISTINCT queries and index support). Safe: JSONB is a strict superset.
    _cast_json_to_jsonb("jobs", "umg_spec")
    _cast_json_to_jsonb("jobs", "s3_keys")
    _cast_json_to_jsonb("jobs", "youtube_data")
    _cast_json_to_jsonb("jobs", "youtube_short_data")
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
    rolling deploy where the previous replica is still accepting requests.
    No-op on non-PostgreSQL backends (SQLite uses dynamic typing)."""
    if engine.dialect.name != "postgresql":
        return
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


def _widen_varchar_column(table: str, column: str, minimum: int) -> None:
    """Expand an existing PostgreSQL VARCHAR without narrowing TEXT columns."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT data_type, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).fetchone()
        if not row or str(row[0]).lower() == "text":
            return
        if (
            str(row[0]).lower() in {"character varying", "varchar"}
            and int(row[1] or 0) >= int(minimum)
        ):
            return
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                f"TYPE VARCHAR({int(minimum)})"
            ))
    except Exception as e:  # pragma: no cover
        print(f"[init_db] varchar widen skipped: {table}.{column} → {e}")


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
