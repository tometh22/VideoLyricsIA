"""JWT authentication module for GenLy AI."""

import json
import os
import time
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

# --- Configuration (loaded from environment) ---
JWT_SECRET = os.environ.get("JWT_SECRET", "genly-default-secret-change-me")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "1440"))

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
        "created_at": time.time(),
    }
    _save_users(users)
    return {"username": username, "role": role, "tenant_id": tenant_id}


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
    }


def create_token(user: dict) -> str:
    """Create a JWT token for the given user."""
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "tenant_id": user.get("tenant_id", "default"),
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
    }


def get_current_user_from_token_param(token: str) -> dict:
    """Validate a token passed as query parameter (for media URLs)."""
    payload = decode_token(token)
    return {
        "username": payload["sub"],
        "role": payload.get("role", "user"),
        "tenant_id": payload.get("tenant_id", "default"),
    }
