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
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.30, "monthly_price": 2000,
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

# Anyone who knows the default secret can forge admin tokens, so running with
# it in production is unacceptable. Fail fast at import time.
_ENV = os.environ.get("ENV", "dev").lower()
if _ENV in ("prod", "production") and (
    not JWT_SECRET or JWT_SECRET == _DEFAULT_INSECURE_SECRET
):
    raise RuntimeError(
        "JWT_SECRET must be set to a strong value when ENV=prod. "
        "Generate one with: openssl rand -base64 32"
    )

# --- Password hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
    """Create a new user. Raises ValueError if username/email exists."""
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
    """Create default admin user if no users exist."""
    if db.query(User).count() == 0:
        admin_pw = os.environ.get("ADMIN_PASSWORD", "genly2026")
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
    record = db.query(PasswordResetToken).filter(
        PasswordResetToken.token == token,
        PasswordResetToken.used == False,
        PasswordResetToken.expires_at > datetime.now(timezone.utc),
    ).first()
    if not record:
        return None
    record.used = True
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
    record = db.query(EmailVerificationToken).filter(
        EmailVerificationToken.token == token,
        EmailVerificationToken.used == False,
        EmailVerificationToken.expires_at > datetime.now(timezone.utc),
    ).first()
    if not record:
        return None
    record.used = True
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
        "stripe_customer_id": user.stripe_customer_id,
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
