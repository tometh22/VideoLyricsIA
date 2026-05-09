"""JWT authentication module for GenLy AI — PostgreSQL backed."""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import User, PasswordResetToken, EmailVerificationToken, get_db, utcnow

# --- Plan definitions ---
PLANS = {
    "free": {"limit": 5, "price_per_video": 0, "overage_rate": 0, "monthly_price": 0,
             "stripe_price_id": None},
    "100": {"limit": 100, "price_per_video": 9.00, "overage_rate": 1.30, "monthly_price": 900,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_100")},
    # Plan "250": $8/video included in $2000/mo, with overage at $12/video
    # ($8 × 1.5). UMG-style B2B accounts opt into allow_overage so they
    # never get blocked at 250 — extra videos invoice out-of-band by
    # transfer.
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.50, "monthly_price": 2000,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_250")},
    "500": {"limit": 500, "price_per_video": 7.00, "overage_rate": 1.30, "monthly_price": 3500,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_500")},
    "1000": {"limit": 1000, "price_per_video": 6.00, "overage_rate": 1.30, "monthly_price": 6000,
             "stripe_price_id": os.environ.get("STRIPE_PRICE_1000")},
    "unlimited": {"limit": 999999, "price_per_video": 0, "overage_rate": 1.0, "monthly_price": 0,
                  "stripe_price_id": None},
}

# --- Configuration (loaded from environment) ---
_DEFAULT_INSECURE_SECRET = "genly-default-secret-change-me"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEFAULT_INSECURE_SECRET)
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "1440"))

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
    return (tenant_id or "").lower() in PRORES_TENANTS

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

security = HTTPBearer()


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id).first()


def create_user(
    db: Session,
    username: str,
    password: str,
    email: str = None,
    role: str = "user",
    tenant_id: str = None,
    plan: str = "free",
) -> User:
    """Create a new user. Raises ValueError if username/email exists or
    the password fails baseline strength checks."""
    validate_password_strength(password)
    if get_user_by_username(db, username):
        raise ValueError(f"User '{username}' already exists")
    if email and get_user_by_email(db, email):
        raise ValueError(f"Email '{email}' already registered")

    # Auto-generate tenant_id from username if not provided
    if not tenant_id:
        tenant_id = username.lower().replace(" ", "_")

    user = User(
        username=username,
        email=email,
        hashed_password=pwd_context.hash(password),
        role=role,
        tenant_id=tenant_id,
        plan_id=plan,
        ai_authorized=(role == "admin"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
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


def verify_password_reset_token(db: Session, token: str) -> Optional[User]:
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
        db.commit()
        return None
    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == token)
        .first()
    )
    if record is None:
        db.commit()
        return None
    # Invalidate every other live token for this user.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == record.user_id,
        PasswordResetToken.token != token,
        PasswordResetToken.used == False,  # noqa: E712
    ).update({PasswordResetToken.used: True}, synchronize_session=False)
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

def create_token(user: User) -> str:
    """Create a JWT token for the given user."""
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
        "exp": time.time() + JWT_EXPIRE_MINUTES * 60,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("exp", 0) < time.time():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
            )
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency — extracts and validates the current user from Bearer token."""
    payload = decode_token(credentials.credentials)
    # Refresh user data from DB to get latest plan etc.
    user = get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
        "allow_overage": getattr(user, "allow_overage", False) or False,
        "stripe_customer_id": user.stripe_customer_id,
        # Capability flags consumed by the frontend to gate UI. Keep
        # the shape stable — `features.<name>: bool` — so adding new
        # gates later doesn't churn the client.
        "features": {
            "prores_export": has_prores_access(user),
        },
    }


def get_current_user_from_token_param(token: str, db: Session) -> dict:
    """Validate a token passed as query parameter (for media URLs)."""
    payload = decode_token(token)
    user = get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
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
    user = get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "plan": user.plan_id,
    }


# ---------------------------------------------------------------------------
# Plan usage
# ---------------------------------------------------------------------------

def get_plan_usage(db: Session, user_id: int, tenant_id: str, plan_id: str) -> dict:
    """Get current month usage vs plan limit."""
    from database import Job

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    used = db.query(Job).filter(
        Job.tenant_id == tenant_id,
        Job.status == "done",
        Job.created_at >= month_start,
    ).count()

    plan = PLANS.get(plan_id, PLANS["100"])
    limit = plan["limit"]
    overage = max(0, used - limit)
    overage_cost = overage * plan["price_per_video"] * plan["overage_rate"]

    return {
        "plan": plan_id,
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "overage": overage,
        "overage_cost_per_video": round(plan["price_per_video"] * plan["overage_rate"], 2),
        "overage_total": round(overage_cost, 2),
        "monthly_price": plan["monthly_price"],
        "percent": min(100, round((used / limit) * 100)) if limit > 0 else 0,
        "alert_80": used >= limit * 0.8,
        "alert_100": used >= limit,
    }
