"""JWT authentication module for GenLy AI — PostgreSQL backed."""

import hashlib
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import (
    EmailVerificationToken,
    LoginSession,
    PasswordResetToken,
    User,
    get_db,
    utcnow,
)

# --- Plan definitions ---
# bg_preview_enabled — gating del pre-render del fondo (Capa C, 2026-05-24).
# Free OFF: cada preview descartado gasta $0.80-3.20 Veo. Para un trial que
# toquetea opciones es bleeding puro. Paid ON: el preview ahorra 30-90s al
# apretar "Crear video".
PLANS = {
    "free": {"limit": 5, "price_per_video": 0, "overage_rate": 0, "monthly_price": 0,
             "stripe_price_id": None, "bg_preview_enabled": False},
    "100": {"limit": 100, "price_per_video": 9.00, "overage_rate": 1.30, "monthly_price": 900,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_100"), "bg_preview_enabled": True},
    # Plan "250": $8/video included in $2000/mo, with overage at $15/video
    # ($8 × 1.875). UMG-style B2B accounts opt into allow_overage so they
    # never get blocked at 250 — extra videos invoice out-of-band by
    # transfer.
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.875, "monthly_price": 2000,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_250"), "bg_preview_enabled": True},
    "500": {"limit": 500, "price_per_video": 7.00, "overage_rate": 1.30, "monthly_price": 3500,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_500"), "bg_preview_enabled": True},
    "1000": {"limit": 1000, "price_per_video": 6.00, "overage_rate": 1.30, "monthly_price": 6000,
             "stripe_price_id": os.environ.get("STRIPE_PRICE_1000"), "bg_preview_enabled": True},
    "unlimited": {"limit": 999999, "price_per_video": 0, "overage_rate": 1.0, "monthly_price": 0,
                  "stripe_price_id": None, "bg_preview_enabled": True},
}

# --- Configuration (loaded from environment) ---
_DEFAULT_INSECURE_SECRET = "genly-default-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEFAULT_INSECURE_SECRET)
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "10080"))  # 7 days

# Tenants allowed to request the broadcast / ProRes deliverable. Comma-
# separated list, e.g. "umg,warner". The product otherwise hides every
# ProRes-related affordance — broadcast clients are private B2B and we
# don't want their brand names visible to retail / self-serve users.
# Admin role bypasses this list (so the operator account can demo /
# QC the feature without opting into a specific tenant).
PRORES_TENANTS = {
    t.strip().lower()
    for t in os.environ.get("PRORES_TENANTS", "").split(",")
    if t.strip()
}


def has_prores_access(user) -> bool:
    """True iff `user` is allowed to request a broadcast (ProRes) master.

    Accepts either a SQLAlchemy `User` model or the dict produced by
    `get_current_user`. Returns False for unauthenticated callers. The
    rule is intentionally simple — admin OR allow-listed tenant —
    because the policy lives entirely in the operator's hands (env var
    + tenant assignment when creating the user).
    """
    if user is None:
        return False
    role = getattr(user, "role", None) if not isinstance(user, dict) else user.get("role")
    if role == "admin":
        return True
    tenant_id = getattr(user, "tenant_id", None) if not isinstance(user, dict) else user.get("tenant_id")
    # Match por tenant O por billing_group: una cuenta B2B como Universal
    # abarca varios tenants (universal_argentina, universal_chile, …) bajo
    # un billing_group ("universal_music"). Gatear por grupo hace que TODOS
    # los tenants de la cuenta — actuales y futuros — hereden ProRes con
    # PRORES_TENANTS=umg,universal_music, sin agregar cada país a mano.
    billing_group = getattr(user, "billing_group", None) if not isinstance(user, dict) else user.get("billing_group")
    return (tenant_id or "").lower() in PRORES_TENANTS or (billing_group or "").lower() in PRORES_TENANTS


# Tenants con acceso a la integración Google Drive ("Guardar en Drive").
# Canary: default vacío → SOLO admin pasa. Cuando se quiera abrir a un
# tenant específico (UMG, Warner, etc) se setea DRIVE_ENABLED_TENANTS=umg
# en env vars sin redeploy de código. Mismo patrón que PRORES_TENANTS.
DRIVE_ENABLED_TENANTS = {
    t.strip().lower()
    for t in os.environ.get("DRIVE_ENABLED_TENANTS", "").split(",")
    if t.strip()
}


def has_drive_access(user) -> bool:
    """True iff `user` puede usar la integración Google Drive.

    Admin role siempre pasa. Para usuarios no-admin se chequea contra
    DRIVE_ENABLED_TENANTS (vacío por defecto = solo admin). Mismo shape
    que has_prores_access — operator policy vía env var.
    """
    if user is None:
        return False
    role = getattr(user, "role", None) if not isinstance(user, dict) else user.get("role")
    if role == "admin":
        return True
    tenant_id = getattr(user, "tenant_id", None) if not isinstance(user, dict) else user.get("tenant_id")
    # Match por tenant O por billing_group — mismo criterio que ProRes, para
    # que mover un usuario entre tenants de la misma cuenta B2B no le saque
    # el acceso a Drive.
    billing_group = getattr(user, "billing_group", None) if not isinstance(user, dict) else user.get("billing_group")
    return (tenant_id or "").lower() in DRIVE_ENABLED_TENANTS or (billing_group or "").lower() in DRIVE_ENABLED_TENANTS


# Tenants/cuentas con acceso al add-on premium "Escenas" (multi-escena).
# Mismo patrón canario que PRORES_TENANTS/DRIVE_ENABLED_TENANTS: default
# vacío → solo admin. Se abre por env var sin redeploy de código:
#   SCENES_ENABLED_TENANTS=umg,universal_music
# Es un add-on OPT-IN: además del acceso, el job debe pedir enable_scenes.
SCENES_ENABLED_TENANTS = {
    t.strip().lower()
    for t in os.environ.get("SCENES_ENABLED_TENANTS", "").split(",")
    if t.strip()
}


def _scenes_globally_enabled() -> bool:
    """Kill-switch global de Escenas. Default ON: la feature es pública y se
    gobierna por CRÉDITOS (scenes_credit_cost), no por allowlist. Poné
    SCENES_GLOBALLY_ENABLED=0 para volver al esquema viejo (admin/allowlist)
    como rollback sin deploy."""
    return os.environ.get("SCENES_GLOBALLY_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on", "y", "t",
    )


def scenes_credit_cost() -> int:
    """Cuántos créditos consume un video con Escenas (multi-escena).

    Env-tunable (SCENES_CREDIT_COST) para lanzar en 3 y subir a 4 sin deploy.
    Mínimo 1 (1 = Escenas no cuesta extra, p.ej. perk para un tenant)."""
    try:
        return max(1, int(os.environ.get("SCENES_CREDIT_COST", "3")))
    except (TypeError, ValueError):
        return 3


def has_scenes_access(user) -> bool:
    """True iff `user` puede usar Escenas (multi-escena).

    Modelo de créditos (2026-06): Escenas es PÚBLICO — cualquier usuario
    autenticado puede usarlo; lo que lo gobierna es el COSTO (N créditos por
    video, ver `scenes_credit_cost`), no una allowlist. Admin siempre pasa.
    Si SCENES_GLOBALLY_ENABLED=0, vuelve al esquema viejo (admin O
    SCENES_ENABLED_TENANTS) como rollback sin deploy. El opt-in real
    (enable_scenes) se sigue decidiendo por job; esto sólo gobierna la
    ELEGIBILIDAD de ver/activar la opción.
    """
    if user is None:
        return False
    role = getattr(user, "role", None) if not isinstance(user, dict) else user.get("role")
    if role == "admin":
        return True
    if _scenes_globally_enabled():
        return True
    tenant_id = getattr(user, "tenant_id", None) if not isinstance(user, dict) else user.get("tenant_id")
    billing_group = getattr(user, "billing_group", None) if not isinstance(user, dict) else user.get("billing_group")
    return (tenant_id or "").lower() in SCENES_ENABLED_TENANTS or (billing_group or "").lower() in SCENES_ENABLED_TENANTS


# Tenants/cuentas que VEN la feature "Art Track" (official audio: cover +
# master audio → video sin letra). Mismo patrón canario que SCENES/PRORES/
# DRIVE, pero con default OFF (kill-switch en "0"): la feature entra a prod
# APAGADA para todos los clientes (incluido UMG) y solo la ve admin, hasta
# abrirla por env var SIN redeploy de código:
#   ART_TRACK_ENABLED_TENANTS=universal_music   (por tenant)
#   ART_TRACK_GLOBALLY_ENABLED=1                 (todos)
ART_TRACK_ENABLED_TENANTS = {
    t.strip().lower()
    for t in os.environ.get("ART_TRACK_ENABLED_TENANTS", "").split(",")
    if t.strip()
}


def _art_track_globally_enabled() -> bool:
    """Kill-switch global de Art Track. Default OFF: la feature se lanza
    apagada (solo admin la ve) y se abre por tenant o global sin deploy."""
    return os.environ.get("ART_TRACK_GLOBALLY_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on", "y", "t",
    )


def has_art_track_access(user) -> bool:
    """True iff `user` puede ver/usar la feature Art Track (official audio).

    Default OFF → solo admin. Se abre SIN deploy con ART_TRACK_GLOBALLY_ENABLED
    (todos) o ART_TRACK_ENABLED_TENANTS=universal_music (por tenant). Gobierna
    tanto la visibilidad de la opción en el wizard (features.art_track) como el
    gate del backend en /generate (defensa en profundidad: un tenant sin acceso
    que pegue a la API con art_track=true igual es rechazado)."""
    if user is None:
        return False
    role = getattr(user, "role", None) if not isinstance(user, dict) else user.get("role")
    if role == "admin":
        return True
    if _art_track_globally_enabled():
        return True
    tenant_id = getattr(user, "tenant_id", None) if not isinstance(user, dict) else user.get("tenant_id")
    billing_group = getattr(user, "billing_group", None) if not isinstance(user, dict) else user.get("billing_group")
    return (tenant_id or "").lower() in ART_TRACK_ENABLED_TENANTS or (billing_group or "").lower() in ART_TRACK_ENABLED_TENANTS


def telemetry_enabled() -> bool:
    """True si la telemetría de sesiones (heartbeat de tiempo-en-app) está prendida.

    Env flag TELEMETRY_ENABLED, default off (mismo patrón que los kill
    switches del pipeline). Se lee en cada llamada — no a import-time —
    para que los tests la monkeypatcheen y para que el endpoint
    /telemetry/heartbeat y el feature flag del frontend compartan una
    única fuente de verdad.
    """
    return os.environ.get("TELEMETRY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _super_admin_allowlist() -> set:
    """Parse SUPER_ADMIN_USERS (comma-separated usernames/emails, case-insensitive).

    Leído en cada request (no a import-time) para que los tests puedan
    monkeypatchear el env y para que un cambio de la var en Railway
    aplique con el redeploy sin sorpresas de orden de import.

    Vive en auth (y no en admin) porque el flag is_super_admin se computa
    en get_current_user/verify_api_key — admin.py ya importa de auth, así
    que esta es la única dirección sin ciclo.
    """
    raw = os.environ.get("SUPER_ADMIN_USERS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def is_explicitly_local_environment(environment: Optional[str] = None) -> bool:
    """Return true only for a small allowlist of non-deployed environments."""
    value = environment
    if value is None:
        value = (
            os.environ.get("ENVIRONMENT")
            or os.environ.get("ENV")
            or "production"
        )
    return value.strip().lower() in {
        "dev", "development", "test", "testing", "local"
    }


def is_super_admin(username, email, role) -> bool:
    """Mismo criterio que admin.require_super_admin, como predicado puro.

    Only explicitly local environments preserve the convenient admin
    fallback. Every deployed or unknown value fails closed when the
    allowlist is missing, so a typo can never promote every tenant admin
    to platform operator.
    """
    if role != "admin":
        return False
    allow = _super_admin_allowlist()
    if not allow:
        return is_explicitly_local_environment()
    return (username or "").lower() in allow or (email or "").lower() in allow


# Anyone who knows the default secret can forge admin tokens, so running with
# it in production is unacceptable. Fail fast at import time.
_ENV = (
    os.environ.get("ENV")
    or os.environ.get("ENVIRONMENT")
    or "dev"
).lower()
if _ENV in ("prod", "production") and (
    not JWT_SECRET or JWT_SECRET == _DEFAULT_INSECURE_SECRET
):
    raise RuntimeError(
        "JWT_SECRET must be set to a strong value when ENV=prod/production or ENVIRONMENT=production. "
        "Generate one with: openssl rand -base64 32"
    )

# Transcription falls back to a local Whisper model (~1.5 GB RAM per request)
# when OPENAI_API_KEY is missing — see pipeline._transcribe in pipeline.py.
# That fallback can't sustain more than a couple of concurrent users, which
# is fine for dev but unacceptable for paying clients. Fail boot in prod
# if the key is missing so the deployment never accidentally serves customer
# traffic from the local-Whisper path.
if _ENV in ("prod", "production") and not os.environ.get("OPENAI_API_KEY", "").strip():
    raise RuntimeError(
        "OPENAI_API_KEY must be set when ENV=prod/production. "
        "The local Whisper fallback uses ~1.5 GB RAM per request and won't scale. "
        "Get a key at https://platform.openai.com/api-keys."
    )

# --- Password hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt silently truncates input at 72 bytes. A 200-char passphrase has the
# entropy of its first 72 bytes — and verify() succeeds for ANY password that
# shares those first 72 bytes. Rejecting outright is safer than silently
# truncating; if longer passphrases are needed, switch the scheme to
# bcrypt_sha256 (which pre-hashes with SHA-256).
BCRYPT_MAX_BYTES = 72
PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> None:
    """Raise ValueError if password fails baseline checks.

    The two checks here are non-negotiable:
      - At least 8 characters (NIST SP 800-63B baseline).
      - At most 72 bytes when UTF-8 encoded (bcrypt's hard limit; longer
        inputs become indistinguishable from their 72-byte prefix).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password is too long ({BCRYPT_MAX_BYTES}-byte max). "
            "Consider a passphrase that is shorter than 72 bytes "
            "(roughly 72 ASCII chars or 36 emoji-heavy chars)."
        )

security = HTTPBearer(auto_error=False)


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, key_hash). The full key is shown to the user
    exactly once and never stored in plaintext — only the SHA-256 hash is kept."""
    raw = secrets.token_hex(32)
    full_key = f"gly_{raw}"
    prefix = full_key[:12]
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, prefix, key_hash


def verify_api_key(db: Session, full_key: str) -> Optional[dict]:
    """Verify a raw API key and return the user dict, or None if invalid."""
    from database import APIKey
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key = db.query(APIKey).filter(
        APIKey.key_hash == key_hash,
        APIKey.is_active.is_(True),
    ).first()
    if not key:
        return None
    user = get_user_by_id(db, key.user_id)
    if not user or not user.is_active:
        return None
    key.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_super_admin": is_super_admin(user.username, user.email, user.role),
        "tenant_id": user.tenant_id,
        "billing_group": getattr(user, "billing_group", None),
        "plan": user.plan_id,
        "allow_overage": getattr(user, "allow_overage", False) or False,
        "features": {
            "prores_export": has_prores_access(user),
            "drive_export": has_drive_access(user),
        },
    }


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id).first()


# Same markers as jobs.get_job_model_resilient — Railway Postgres drops
# idle conns AFTER pool_pre_ping. Auth dependency hits the DB on every
# request, and upload-proxy bodies > 1 MiB can't be replayed by the
# global middleware, so we retry inline here too. Confirmed in prod
# logs 2026-05-14 15:59 on /upload-part-proxy AFTER the jobs.py fix
# deployed — the same drop was happening one layer earlier.
_AUTH_TRANSIENT_DB_MARKERS = (
    "SSL connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
)


def get_user_by_id_resilient(
    db: Session, user_id: int, max_attempts: int = 3
) -> Optional[User]:
    """get_user_by_id with retry on transient Postgres SSL drops.

    Used by get_current_user (the auth dependency for every protected
    endpoint, including the upload proxies). Without this, an idle-drop
    bubbles up before the handler-level get_job_model_resilient gets a
    chance to run, and the request 500s.
    """
    from sqlalchemy.exc import OperationalError
    last_err: Optional[OperationalError] = None
    for attempt in range(max_attempts):
        try:
            return get_user_by_id(db, user_id)
        except OperationalError as e:
            if not any(m in str(e) for m in _AUTH_TRANSIENT_DB_MARKERS):
                raise
            last_err = e
            try:
                db.rollback()
            except OperationalError:
                pass
    assert last_err is not None
    raise last_err


def create_user(
    db: Session,
    username: str,
    password: str,
    email: str = None,
    role: str = "user",
    tenant_id: str = None,
    plan: str = "free",
    ai_authorized: bool = True,
    enforce_reserved: bool = True,
    commit: bool = True,
) -> User:
    """Create a new user. Raises ValueError if username/email exists or
    the password fails baseline strength checks.

    `ai_authorized` defaults to True so that self-registered users (the
    public funnel) can generate immediately. Admins creating users for
    regulated tenants (e.g. UMG, where Guideline 5 requires explicit
    per-operator authorization) must pass `ai_authorized=False` and use
    `/admin/users/{id}/authorize-ai` once the user has been cleared.

    `enforce_reserved` (default True): block creation of users whose
    derived `tenant_id` lands on a reserved system tenant (`default`,
    `admin`, `umg`, etc.). The HTTP `/auth/register` endpoint relies on
    this. Internal callers — `ensure_default_admin`, tests, admin
    seeding scripts — pass `enforce_reserved=False` so they can populate
    the system tenants without tripping the self-registration guard.
    """
    validate_password_strength(password)
    if get_user_by_username(db, username):
        raise ValueError(f"User '{username}' already exists")
    if email and get_user_by_email(db, email):
        raise ValueError(f"Email '{email}' already registered")

    # Auto-generate tenant_id from username if not provided
    if not tenant_id:
        tenant_id = username.lower().replace(" ", "_")

    # SECURITY (incident 2026-05-24, PR #284): block users from
    # auto-deriving a reserved tenant_id. Without this, anyone
    # registering with `username="default"` lands in the admin tenant
    # (`ensure_default_admin` uses `tenant_id="default"`) and inherits
    # visibility of every admin job via the tenant_id-only `_job_scope`.
    # Same risk for any tenant whose id is a username-shaped string
    # ("umg", "epical", etc.).
    #
    # HOTFIX 2026-05-25 (PR #284 regression): this check broke 206
    # existing tests whose fixtures use `tenant_id="default"`. The
    # check is the RIGHT call at the HTTP-register-endpoint boundary
    # — internal bootstrap (`ensure_default_admin`) and tests need to
    # populate system tenants. Two escape hatches:
    #   - `enforce_reserved=False` argument (explicit, used by
    #     `ensure_default_admin`)
    #   - `ENVIRONMENT in {test, development, dev}` (implicit, used
    #     by pytest fixtures via conftest setting ENVIRONMENT=development)
    _env_bypass = (os.environ.get("ENVIRONMENT", "").lower()
                   in ("test", "dev", "development"))
    if enforce_reserved and not _env_bypass:
        _RESERVED_TENANT_IDS = {
            "default", "admin", "system", "root", "internal",
            "umg", "epical", "genly",
        }
        if tenant_id in _RESERVED_TENANT_IDS:
            raise ValueError(
                f"Tenant '{tenant_id}' is reserved — choose a different username "
                f"or contact an administrator to be added to that team."
            )
        # Also defend against tenant_id collision with an existing
        # tenant: if any user already owns that tenant_id, refuse (the
        # new user would see those users' jobs).
        existing_in_tenant = db.query(User).filter(User.tenant_id == tenant_id).first()
        if existing_in_tenant is not None:
            raise ValueError(
                f"Tenant '{tenant_id}' already exists — choose a different "
                f"username (yours would have been derived to the same tenant)."
            )

    user = User(
        username=username,
        email=email,
        hashed_password=pwd_context.hash(password),
        role=role,
        tenant_id=tenant_id,
        plan_id=plan,
        ai_authorized=ai_authorized,
    )
    db.add(user)
    if commit:
        db.commit()
        db.refresh(user)
    else:
        # Registration can flush the user and let start_login_session commit
        # both rows atomically. A session failure then rolls back the account.
        db.flush()
    return user


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    """Verify credentials. Returns User or None."""
    # Allow login by username or email
    user = get_user_by_username(db, username)
    if not user:
        user = get_user_by_email(db, username)
    if not user or not user.is_active:
        return None
    if not pwd_context.verify(password, user.hashed_password):
        return None
    return user


def ensure_default_admin(db: Session):
    """Create default admin user if no users exist.

    In production we refuse to bootstrap with a hardcoded password — anyone
    who knows the published default ("genly2026") would have root on a
    fresh DB. The operator must set ADMIN_PASSWORD explicitly.
    """
    if db.query(User).count() != 0:
        return

    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    is_prod = _ENV in ("prod", "production")
    if is_prod and not admin_pw:
        raise RuntimeError(
            "Refusing to create default admin in production without "
            "ADMIN_PASSWORD set. Generate one (e.g. `openssl rand -base64 24`) "
            "and pass it as an environment variable."
        )
    if not admin_pw:
        # Dev / test only — keep the legacy default to avoid breaking
        # local-first onboarding flows and the existing test suite.
        admin_pw = "genly2026"

    create_user(
        db,
        username="admin",
        password=admin_pw,
        email=os.environ.get("ADMIN_EMAIL"),
        role="admin",
        tenant_id="default",
        plan="unlimited",
        # Internal bootstrap — must populate the `default` tenant even
        # though it's in `_RESERVED_TENANT_IDS`. The self-registration
        # path passes `enforce_reserved=True` (default).
        enforce_reserved=False,
    )


# ---------------------------------------------------------------------------
# Password reset / email verification
# ---------------------------------------------------------------------------

def create_password_reset_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(48)
    db.add(PasswordResetToken(
        user_id=user.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    ))
    db.commit()
    return token


def verify_password_reset_token(
    db: Session, token: str, *, commit: bool = True,
) -> Optional[User]:
    """Atomically claim a password-reset token.

    Two callers concurrently presenting the same valid token would both
    pass the `used == False` filter and both proceed to set a new
    password — letting the same token be used twice. We claim the token
    in a single UPDATE … WHERE used=false statement; only the first
    claim's rowcount is 1, the second sees 0 and is rejected.

    On successful claim we ALSO invalidate every other outstanding reset
    token for that user, so a phished token can't survive a self-service
    reset.
    """
    now = datetime.now(timezone.utc)
    rowcount = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,  # noqa: E712 — SQLAlchemy needs ==
            PasswordResetToken.expires_at > now,
        )
        .update({PasswordResetToken.used: True}, synchronize_session=False)
    )
    if rowcount == 0:
        if commit:
            db.commit()
        else:
            db.rollback()
        return None
    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == token)
        .first()
    )
    if record is None:
        if commit:
            db.commit()
        else:
            db.rollback()
        return None
    # Invalidate every other live token for this user.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == record.user_id,
        PasswordResetToken.token != token,
        PasswordResetToken.used == False,  # noqa: E712
    ).update({PasswordResetToken.used: True}, synchronize_session=False)
    if commit:
        db.commit()
    return get_user_by_id(db, record.user_id)


def create_email_verification_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(48)
    db.add(EmailVerificationToken(
        user_id=user.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
    ))
    db.commit()
    return token


def verify_email_token(db: Session, token: str) -> Optional[User]:
    """Atomically claim an email-verification token.

    See verify_password_reset_token() for the race rationale; same fix.
    """
    now = datetime.now(timezone.utc)
    rowcount = (
        db.query(EmailVerificationToken)
        .filter(
            EmailVerificationToken.token == token,
            EmailVerificationToken.used == False,  # noqa: E712
            EmailVerificationToken.expires_at > now,
        )
        .update({EmailVerificationToken.used: True}, synchronize_session=False)
    )
    if rowcount == 0:
        db.commit()
        return None
    record = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.token == token)
        .first()
    )
    if record is None:
        db.commit()
        return None
    user = get_user_by_id(db, record.user_id)
    if user:
        user.email_verified = True
    db.commit()
    return user


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

_ACCESS_TOKEN_TYPE = "access"


def _record_session_rejection(name: str = "session_rejected") -> None:
    try:
        from ops_metrics import increment
        increment(name)
    except Exception:
        pass


def create_token(user: User, jti: str = None) -> str:
    """Create a JWT token for the given user.

    `jti` (JWT ID) liga el token a una fila de login_sessions para poder
    cerrarlo remotamente. Login/registro pasan un jti nuevo (sesión nueva);
    refresh reusa el jti del token actual (misma sesión, nuevo exp). Si no
    se pasa, se genera uno — pero el token sólo es revocable si además
    existe la fila de sesión (la crea start_login_session).
    """
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
        "jti": jti or uuid.uuid4().hex,
        "tt": _ACCESS_TOKEN_TYPE,
        "av": int(getattr(user, "auth_version", 0) or 0),
        "exp": time.time() + JWT_EXPIRE_MINUTES * 60,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def start_login_session(db: Session, user: User, request: Request = None) -> str:
    """Crea una fila login_sessions (dispositivo) y devuelve el token JWT
    ligado a ella vía jti. Usado por login y registro.

    Fail-closed: el JWT se construye antes, pero sólo se devuelve después de
    que la fila revocable fue confirmada. Una tabla ausente o un fallo de DB
    devuelve 503 y nunca entrega una credencial huérfana.
    """
    jti = uuid.uuid4().hex
    ip = None
    ua = None
    if request is not None:
        ip = request.client.host if request.client else None
        ua = (request.headers.get("user-agent") or "")[:400] or None
    token = create_token(user, jti=jti)
    try:
        db.add(LoginSession(user_id=user.id, jti=jti, ip_address=ip, user_agent=ua))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to create a revocable login session",
        ) from exc
    return token


def invalidate_user_access(
    db: Session,
    user: User,
    *,
    keep_jti: str | None = None,
) -> tuple[int, int]:
    """Invalidate existing access JWTs without committing the transaction.

    Password reset callers pass no ``keep_jti`` and revoke every session.
    Authenticated password changes may retain the current session row, then
    mint a replacement token with the incremented auth version. The caller
    owns the commit so the password hash, version bump and revocations remain
    one transaction.
    """
    user.auth_version = int(getattr(user, "auth_version", 0) or 0) + 1
    query = db.query(LoginSession).filter(
        LoginSession.user_id == user.id,
        LoginSession.revoked_at.is_(None),
    )
    if keep_jti:
        query = query.filter(LoginSession.jti != keep_jti)
    revoked = query.update(
        {LoginSession.revoked_at: datetime.now(timezone.utc)},
        synchronize_session=False,
    )
    return user.auth_version, revoked


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        # jwt.decode() validates the `exp` claim automatically and raises
        # JWTError if expired — no manual time check needed.
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def _validate_access_claims(payload: dict, user: User) -> str:
    """Validate access-only claims and return the session ``jti``.

    Missing ``tt``/``av`` are accepted only as the pre-cutover access-token
    shape (version zero). Missing ``jti`` is not accepted: an access token
    that cannot be tied to a persisted, revocable session is invalid.
    """
    token_type = payload.get("tt")
    if token_type not in (None, _ACCESS_TOKEN_TYPE):
        _record_session_rejection()
        raise HTTPException(status_code=401, detail="Wrong token type")

    try:
        token_version = int(payload.get("av", 0))
        user_version = int(getattr(user, "auth_version", 0) or 0)
    except (TypeError, ValueError) as exc:
        _record_session_rejection()
        raise HTTPException(status_code=401, detail="Invalid token version") from exc
    if token_version != user_version:
        _record_session_rejection()
        raise HTTPException(status_code=401, detail="Session credentials have changed")

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        _record_session_rejection()
        raise HTTPException(status_code=401, detail="Login session required")
    return jti


def _validate_login_session(db: Session, user: User, jti: str) -> None:
    """Require a live session row; only last_seen persistence is best-effort."""
    try:
        session = (
            db.query(LoginSession)
            .filter(LoginSession.jti == jti, LoginSession.user_id == user.id)
            .first()
        )
    except SQLAlchemyError as exc:
        db.rollback()
        _record_session_rejection("session_validation_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session validation temporarily unavailable",
        ) from exc

    if session is None or session.revoked_at is not None:
        _record_session_rejection()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session closed. Sign in again.",
        )

    now = datetime.now(timezone.utc)
    last_seen = session.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or (now - last_seen).total_seconds() > 300:
        session.last_seen_at = now
        try:
            db.commit()
        except SQLAlchemyError:
            # A heartbeat must never turn an already-validated session into
            # an auth outage. Roll back the optional write and continue.
            db.rollback()


# Plain `def` (not `async def`) ON PURPOSE: this dependency runs on EVERY
# authenticated request (50+ endpoints) and does 1-2 BLOCKING SQLAlchemy
# queries (verify_api_key / get_user_by_id_resilient) plus a sync JWT decode.
# As `async def` it ran that blocking work directly on the uvicorn event loop,
# serializing all traffic under load. FastAPI runs a sync dependency in its
# threadpool, so the blocking auth no longer freezes the loop. The body has no
# `await` and its sub-deps (security, get_db) are sync, so this is a pure
# concurrency-model change — identical behavior. Do NOT re-add `async` unless
# you also move the DB work off the loop.
def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency — accepts either a JWT Bearer token or an X-API-Key header."""
    # API key path (enterprise integrations)
    api_key_value = request.headers.get("X-API-Key")
    if api_key_value:
        user_dict = verify_api_key(db, api_key_value)
        if not user_dict:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        return user_dict

    # JWT path (browser/app)
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    # Refresh user data from DB to get latest plan etc.
    # Resilient: see get_user_by_id_resilient — auth dep runs on every
    # /upload-part-proxy request and the global middleware can't replay
    # multi-MB bodies if this throws an SSL drop.
    try:
        subject_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    try:
        user = get_user_by_id_resilient(db, subject_id)
    except SQLAlchemyError as exc:
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        _record_session_rejection("session_validation_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session validation temporarily unavailable",
        ) from exc
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    jti = _validate_access_claims(payload, user)
    _validate_login_session(db, user, jti)

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": getattr(user, "full_name", None),
        "avatar_url": getattr(user, "avatar_url", None),
        "jti": jti,
        "role": user.role,
        # Visibilidad de la sección Insights (panel CEO). Solo gating de UI:
        # la seguridad real es require_super_admin en cada endpoint.
        "is_super_admin": is_super_admin(user.username, user.email, user.role),
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
        # Cuenta de facturación compartida entre tenants (None = cuota por
        # tenant). Lo lee _enforce_plan_quota y el endpoint /usage.
        "billing_group": getattr(user, "billing_group", None),
        "allow_overage": getattr(user, "allow_overage", False) or False,
        "stripe_customer_id": user.stripe_customer_id,
        # Dunning state for the in-app past-due banner (Fase 1.5). Read
        # fresh from the DB here (this dep refreshes the user row on every
        # request), so the banner clears the moment a retry succeeds.
        # getattr default keeps old tokens/tests safe pre-migration.
        "billing_status": getattr(user, "billing_status", "active") or "active",
        # Capability flags consumed by the frontend to gate UI. Keep
        # the shape stable — `features.<name>: bool` — so adding new
        # gates later doesn't churn the client.
        "features": {
            "prores_export": has_prores_access(user),
            "drive_export": has_drive_access(user),
            # Heartbeat de sesiones: el frontend solo manda pings cuando
            # el server lo habilita → kill-switch sin redeploy de Vercel.
            "telemetry": telemetry_enabled(),
        },
    }


def get_current_user_from_token_param(token: str, db: Session) -> dict:
    """Validate a legacy access token passed as query parameter.

    This path remains only for the temporary SSE compatibility window. It
    enforces the same auth-version and persisted-session checks as Bearer.
    """
    payload = decode_token(token)
    try:
        subject_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = get_user_by_id(db, subject_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    jti = _validate_access_claims(payload, user)
    _validate_login_session(db, user, jti)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
        "allow_overage": getattr(user, "allow_overage", False) or False,
    }


# ---------------------------------------------------------------------------
# Short-lived media tokens
# ---------------------------------------------------------------------------
#
# /download and /preview accept a token in the query string. Reusing the
# 24-hour login JWT there is unsafe: media URLs are saved in browser history,
# server access logs, and Referer headers when a redirect (R2 signed URL) is
# followed. Anyone who scrapes a URL gets a full account takeover for 24
# hours. We mint a *separate* token here, scoped to a single (job_id,
# file_type) and short-lived, so a leaked URL leaks nothing useful.

MEDIA_TOKEN_EXPIRE_SECONDS = int(os.environ.get("MEDIA_TOKEN_EXPIRE_SECONDS", "300"))
_MEDIA_TOKEN_TYPE = "media"


def create_media_token(user: User, job_id: str, file_type: str) -> str:
    """Mint a short-lived token scoped to a single job/file_type."""
    payload = {
        "sub": str(user.id),
        "tid": user.tenant_id,
        "jid": job_id,
        "ft": file_type,
        "tt": _MEDIA_TOKEN_TYPE,
        "av": int(getattr(user, "auth_version", 0) or 0),
        "exp": time.time() + MEDIA_TOKEN_EXPIRE_SECONDS,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_media_token(token: str, job_id: str, file_type: str, db: Session) -> dict:
    """Validate a media token and check it's scoped to (job_id, file_type)."""
    payload = decode_token(token)
    if payload.get("tt") != _MEDIA_TOKEN_TYPE:
        raise HTTPException(status_code=401, detail="Wrong token type for media URL")
    if payload.get("jid") != job_id or payload.get("ft") != file_type:
        raise HTTPException(status_code=401, detail="Token scope mismatch")
    try:
        subject_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = get_user_by_id(db, subject_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    # Pre-cutover five-minute media tokens had no av claim; treat them as v0
    # so a rolling API deploy does not break an already-open download URL.
    if int(payload.get("av", 0)) != int(getattr(user, "auth_version", 0) or 0):
        raise HTTPException(status_code=401, detail="Stale media token")
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_super_admin": is_super_admin(user.username, user.email, user.role),
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
    }


# ---------------------------------------------------------------------------
# Plan usage
# ---------------------------------------------------------------------------

def _as_utc(dt):
    """Normaliza un datetime a aware-UTC. SQLite devuelve naive en columnas
    DateTime(timezone=True) (Postgres devuelve aware); sin esto, comparar contra
    `datetime.now(timezone.utc)` tira TypeError offset-naive vs offset-aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def get_plan_usage(db: Session, user_id: int, tenant_id: str, plan_id: str,
                   billing_group: str = None) -> dict:
    """Get current month usage vs plan limit.

    Counts only APPROVED videos in the current month. A job's "approved"
    moment is `approved_at` — set by the /approve endpoint together with
    the status="done" flip. Filtering on `approved_at >= month_start`
    (instead of the prior `created_at >= month_start`) makes the
    pricing model align with the contract operators actually pay for:

      - Iterations (re-renders via /edit + /retry, bg_preview browsing,
        rejected drafts) do NOT consume quota.
      - A video approved on 2026-06-02 counts against June even if its
        underlying job row was created on 2026-05-31. Without the
        approved_at filter the operator gets billed for the wrong month
        when they push approvals across boundaries.

    Both filters are required (status="done" AND approved_at >=
    month_start): status alone would let a rejected-after-approve job
    slip into the count, since /reject leaves approved_at populated
    while flipping status away from "done".

    Cuota compartida (billing_group): cuando el usuario pertenece a una
    cuenta de facturación (ej. Universal Music con tenants separados para
    Argentina y Chile), la cuota se cuenta sobre TODOS los tenants cuyos
    usuarios estén en ese grupo — un solo pool mensual para toda la cuenta,
    aunque los equipos no se vean los videos entre sí. Sin billing_group,
    la cuota es por tenant (comportamiento histórico).
    """
    from database import Job, User, CreditGrant

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    # Scope de la CUENTA (idéntico para la cuota y para los créditos de regalo):
    # billing_group si existe (cuenta multi-tenant tipo Universal AR/CL), si no
    # el tenant. Un solo pool por cuenta.
    if billing_group:
        # Tenants que componen la cuenta: todos los que tengan al menos un
        # usuario en el grupo. La cantidad de usuarios es chica (decenas),
        # el subquery es trivial.
        group_tenants = (
            db.query(User.tenant_id)
            .filter(User.billing_group == billing_group)
            .distinct()
        )
        scope = Job.tenant_id.in_(group_tenants)
    else:
        scope = Job.tenant_id == tenant_id

    _cost = scenes_credit_cost()

    def _weighted(start, end):
        """Créditos consumidos (normal=1, Escenas=_cost) por videos APROBADOS
        en [start, end). Mismo criterio que la cuota (status="done" AND
        approved_at). El conteo de Escenas va en Python por portabilidad del
        índice JSONB (Postgres vs SQLite) — equivale a
        `scene_plan->'scenes' IS NOT NULL`. Cada job cuenta 1; +(N-1) si Escenas.
        """
        if end <= start:
            return 0
        q = db.query(Job).filter(
            scope,
            Job.status == "done",
            Job.approved_at >= start,
            Job.approved_at < end,
        )
        n = q.count()
        extra = 0
        if _cost > 1:
            extra = (_cost - 1) * sum(
                1
                for (_sp,) in q.with_entities(Job.scene_plan).all()
                if isinstance(_sp, dict) and _sp.get("scenes") is not None
            )
        return n + extra

    # Uso del mes (créditos consumidos este mes, antes de aplicar el regalo).
    _now_excl = now + timedelta(seconds=1)
    used = _weighted(month_start, _now_excl)

    # ── Créditos de regalo (promos, ej. lanzamiento de Escenas) ──────────────
    # Pool por cuenta que se consume ANTES del cupo del plan, con vencimiento.
    # Sin estado mutable: se calcula cuánto se consumió contando los aprobados
    # desde `granted_at` (reject/un-approve revierten solos). Si hay varios
    # grants activos para la cuenta, se tratan como un único pool (suma de
    # montos; ventana = desde el más viejo hasta el vto más lejano). El
    # lanzamiento emite UNO por cuenta.
    grant_q = db.query(CreditGrant).filter(CreditGrant.revoked.is_(False))
    if billing_group:
        grant_q = grant_q.filter(CreditGrant.billing_group == billing_group)
    else:
        grant_q = grant_q.filter(
            CreditGrant.tenant_id == tenant_id,
            CreditGrant.billing_group.is_(None),
        )
    grants = [
        g for g in grant_q.all()
        if g.expires_at is None or _as_utc(g.expires_at) > now
    ]

    bonus_total = sum(g.amount for g in grants)
    bonus_used = 0
    bonus_remaining = 0
    bonus_expires_at = None
    if grants:
        g_start = min(_as_utc(g.granted_at) for g in grants)
        _exps = [_as_utc(g.expires_at) for g in grants if g.expires_at is not None]
        bonus_expires_at = max(_exps) if _exps else None
        window_end = (min(now, bonus_expires_at) if bonus_expires_at else now) + timedelta(seconds=1)
        # Consumido en meses anteriores (dentro de la ventana del grant).
        prior = _weighted(g_start, month_start) if g_start < month_start else 0
        avail_start = max(0, bonus_total - prior)
        # Uso de ESTE mes elegible para el regalo (aprobado antes del vto).
        this_month_elig = _weighted(max(month_start, g_start), window_end)
        bonus_used = min(avail_start, this_month_elig)
        bonus_remaining = max(0, bonus_total - prior - bonus_used)

    # El regalo cubre primero → contra el plan pega sólo lo no cubierto.
    billable_used = max(0, used - bonus_used)

    plan = PLANS.get(plan_id, PLANS["100"])
    limit = plan["limit"]
    overage = max(0, billable_used - limit)
    overage_cost = overage * plan["price_per_video"] * plan["overage_rate"]

    plan_remaining = max(0, limit - billable_used)
    total_available = plan_remaining + bonus_remaining

    return {
        "plan": plan_id,
        "limit": limit,
        "used": billable_used,
        "remaining": plan_remaining,
        "overage": overage,
        "overage_cost_per_video": round(plan["price_per_video"] * plan["overage_rate"], 2),
        "overage_total": round(overage_cost, 2),
        "monthly_price": plan["monthly_price"],
        "percent": min(100, round((billable_used / limit) * 100)) if limit > 0 else 0,
        "alert_80": billable_used >= limit * 0.8,
        "alert_100": billable_used >= limit,
        # ── Créditos: costo por tipo + regalo (alimentan el medidor dinámico) ──
        "scenes_credit_cost": _cost,
        "bonus_total": bonus_total,
        "bonus_used": bonus_used,
        "bonus_remaining": bonus_remaining,
        "bonus_expires_at": bonus_expires_at.isoformat() if bonus_expires_at else None,
        "total_available": total_available,
        "projection": {
            "normal": total_available,
            "escenas": (total_available // _cost) if _cost > 0 else total_available,
        },
    }
