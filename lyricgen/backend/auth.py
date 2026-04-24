"""JWT authentication module for GenLy AI."""

import json
import os
import time
from typing import Optional

# --- Plan definitions ---
PLANS = {
    "100": {"limit": 100, "price_per_video": 9.00, "overage_rate": 1.30},
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.30},
    "500": {"limit": 500, "price_per_video": 7.00, "overage_rate": 1.30},
    "1000": {"limit": 1000, "price_per_video": 6.00, "overage_rate": 1.30},
    "unlimited": {"limit": 999999, "price_per_video": 0, "overage_rate": 1.0},
}

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

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

# --- User store ---
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
_USERS_PATH = os.path.join(OUTPUTS_DIR, "_users.json")

security = HTTPBearer()


def _load_users() -> dict:
    """Load users from JSON file."""
    if os.path.exists(_USERS_PATH):
        try:
            with open(_USERS_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_users(users: dict) -> None:
    """Save users to JSON file."""
    os.makedirs(os.path.dirname(_USERS_PATH), exist_ok=True)
    with open(_USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)


def _ensure_default_admin():
    """Create default admin user if no users exist."""
    users = _load_users()
    if not users:
        users["admin"] = {
            "username": "admin",
            "hashed_password": pwd_context.hash("genly2026"),
            "role": "admin",
            "tenant_id": "default",
            "plan": "100",
            "created_at": time.time(),
        }
        _save_users(users)


# Ensure default admin on import
_ensure_default_admin()


def create_user(
    username: str,
    password: str,
    role: str = "user",
    tenant_id: str = "default",
    plan: str = "100",
) -> dict:
    """Create a new user. Returns the user dict (without password)."""
    users = _load_users()
    if username in users:
        raise ValueError(f"User '{username}' already exists")

    users[username] = {
        "username": username,
        "hashed_password": pwd_context.hash(password),
        "role": role,
        "tenant_id": tenant_id,
        "plan": plan,
        "created_at": time.time(),
    }
    _save_users(users)
    return {"username": username, "role": role, "tenant_id": tenant_id, "plan": plan}


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """Verify credentials. Returns user dict or None."""
    users = _load_users()
    user = users.get(username)
    if not user:
        return None
    if not pwd_context.verify(password, user["hashed_password"]):
        return None
    return {
        "username": user["username"],
        "role": user["role"],
        "tenant_id": user.get("tenant_id", "default"),
        "plan": user.get("plan", "100"),
    }


def create_token(user: dict) -> str:
    """Create a JWT token for the given user."""
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "tenant_id": user.get("tenant_id", "default"),
        "plan": user.get("plan", "100"),
        "exp": time.time() + JWT_EXPIRE_MINUTES * 60,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Returns payload or raises."""
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
) -> dict:
    """FastAPI dependency — extracts and validates the current user from Bearer token."""
    payload = decode_token(credentials.credentials)
    return {
        "username": payload["sub"],
        "role": payload.get("role", "user"),
        "tenant_id": payload.get("tenant_id", "default"),
        "plan": payload.get("plan", "100"),
    }


def get_current_user_from_token_param(token: str) -> dict:
    """Validate a token passed as query parameter (for media URLs)."""
    payload = decode_token(token)
    return {
        "username": payload["sub"],
        "role": payload.get("role", "user"),
        "tenant_id": payload.get("tenant_id", "default"),
        "plan": payload.get("plan", "100"),
    }


def get_plan_usage(tenant_id: str, plan_id: str) -> dict:
    """Get current month usage vs plan limit."""
    from jobs import get_all_jobs
    import calendar
    from datetime import datetime

    now = datetime.now()
    month_start = datetime(now.year, now.month, 1).timestamp()

    all_jobs = get_all_jobs(tenant_id=tenant_id)
    monthly_done = [j for j in all_jobs if j.get("status") == "done" and j.get("created_at", 0) >= month_start]
    used = len(monthly_done)

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
        "percent": min(100, round((used / limit) * 100)) if limit > 0 else 0,
        "alert_80": used >= limit * 0.8,
        "alert_100": used >= limit,
    }
