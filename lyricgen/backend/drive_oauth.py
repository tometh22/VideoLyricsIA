"""Google Drive OAuth 2.0 flow + token encryption.

Scope: `drive.file` (limitado a archivos creados por la app). Google
NO requiere app verification con este scope, por eso podemos shippear
sin pasar por review humano (~2-6 semanas).

Flujo:
  1. Frontend pide GET /drive/auth-url → backend devuelve URL Google
     con state token (HMAC-signed, contiene user_id + nonce + exp).
  2. User autoriza en Google → callback a /drive/callback?code&state.
  3. Backend valida state → exchange code → guarda refresh_token
     Fernet-encrypted en user_drive_tokens.
  4. Cualquier worker que necesite acceso a Drive del user llama
     `get_fresh_access_token(user_id)` que refresca y devuelve un
     access_token short-lived.

Tokens encriptados con Fernet (symmetric AES-128-CBC + HMAC). La key
viene de DRIVE_TOKEN_ENCRYPTION_KEY env var. Si la rotás, los tokens
viejos quedan ilegibles → users deben reconectar.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import jwt
import requests
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("genly.drive_oauth")


# --- Config ---

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
DRIVE_TOKEN_ENCRYPTION_KEY = os.environ.get("DRIVE_TOKEN_ENCRYPTION_KEY", "")

# Scope mínimo — la app solo puede ver/escribir archivos que CREÓ.
# No puede listar el resto del Drive del user. Esto es deliberado:
# evita Google app verification y mantiene el blast radius chico.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# State token: HMAC-signed JWT con user_id + nonce + exp. Sin signing,
# un atacante podría forgear callbacks para conectar Drive a otro user.
STATE_TOKEN_TTL_S = 600  # 10 min entre que pedís auth-url y completás


# --- Errors ---

class DriveOAuthError(Exception):
    """OAuth flow error que debe surface al frontend con detail descriptivo."""
    pass


class DriveTokenDecryptError(Exception):
    """No se pudo decrypt el refresh_token guardado.

    Causas típicas:
    - DRIVE_TOKEN_ENCRYPTION_KEY rotada → tokens viejos ilegibles.
    - Row corrupta en DB.
    Handler debería pedirle al user que reconecte Drive.
    """
    pass


# --- Encryption helpers (Fernet) ---

def _fernet() -> Fernet:
    """Singleton-ish Fernet instance. Falla rápido si la key falta o
    no es Fernet-compatible (debe ser 32 url-safe bytes base64-encoded)."""
    if not DRIVE_TOKEN_ENCRYPTION_KEY:
        raise DriveOAuthError(
            "DRIVE_TOKEN_ENCRYPTION_KEY no está seteada. Generala con: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(DRIVE_TOKEN_ENCRYPTION_KEY.encode())


def encrypt_token(plaintext: str) -> str:
    """Encrypta un refresh_token para guardarlo en DB. Devuelve string
    para que entre en VARCHAR cómodo (output Fernet ≈ 100-200 chars
    para un Google refresh token típico de ~200 chars plaintext)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Reverso de encrypt_token. Raise DriveTokenDecryptError si la
    key rotó o el ciphertext está corrupto — el handler convierte a
    401 con `action: reconnect_drive` para el frontend."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as e:
        raise DriveTokenDecryptError(
            f"No se pudo decrypt el refresh_token (key rotada o data corrupta): {e}"
        ) from e


# --- State token (CSRF + user binding) ---

def _state_jwt_secret() -> str:
    """Reusa JWT_SECRET de auth.py. Importamos lazy para evitar circular."""
    from auth import JWT_SECRET, JWT_ALGORITHM
    return JWT_SECRET, JWT_ALGORITHM


def build_state_token(user_id: int) -> str:
    """Mint un state token signed que liga la sesión OAuth al user
    actual. El callback verifica que el state esté firmado y no expiró,
    sino un atacante podría redirigir un código de OAuth de su cuenta
    al callback de la víctima."""
    secret, alg = _state_jwt_secret()
    payload = {
        "user_id": user_id,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + STATE_TOKEN_TTL_S,
        "iat": int(time.time()),
        "type": "drive_oauth_state",
    }
    return jwt.encode(payload, secret, algorithm=alg)


def verify_state_token(state: str) -> int:
    """Devuelve user_id si state es válido y no expiró. Sino raise."""
    secret, alg = _state_jwt_secret()
    try:
        payload = jwt.decode(state, secret, algorithms=[alg])
    except jwt.ExpiredSignatureError:
        raise DriveOAuthError("State token expirado. Reintentá el OAuth flow.")
    except jwt.InvalidTokenError as e:
        raise DriveOAuthError(f"State token inválido: {e}")
    if payload.get("type") != "drive_oauth_state":
        raise DriveOAuthError("State token de otro flow.")
    user_id = payload.get("user_id")
    if not isinstance(user_id, int):
        raise DriveOAuthError("State token sin user_id válido.")
    return user_id


# --- OAuth flow ---

def build_authorization_url(user_id: int) -> str:
    """URL a la que el frontend redirige al user para que autorice
    Drive. Incluye state HMAC-signed."""
    if not GOOGLE_OAUTH_CLIENT_ID:
        raise DriveOAuthError(
            "GOOGLE_OAUTH_CLIENT_ID no configurada. Setteá en Railway env vars."
        )
    if not GOOGLE_OAUTH_REDIRECT_URI:
        raise DriveOAuthError(
            "GOOGLE_OAUTH_REDIRECT_URI no configurada."
        )
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",         # → Google emite refresh_token
        "prompt": "consent",              # → refresh_token aunque ya autorizó antes
        "include_granted_scopes": "true",
        "state": build_state_token(user_id),
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Cambia el code de Google por {access_token, refresh_token,
    expires_in, scope, token_type, ...}. Solo se usa una vez por flow."""
    res = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not res.ok:
        raise DriveOAuthError(
            f"Google rechazó el code exchange ({res.status_code}): {res.text[:300]}"
        )
    data = res.json()
    if "refresh_token" not in data:
        # Google omite refresh_token si el user ya había autorizado
        # antes y no usamos prompt=consent. build_authorization_url usa
        # prompt=consent justo para evitar esto, pero defendamos.
        raise DriveOAuthError(
            "Google no devolvió refresh_token. Asegurate de revocar acceso "
            "previo y reintentar (myaccount.google.com → security → third-party)."
        )
    return data


def refresh_access_token(refresh_token: str) -> dict:
    """Usa el refresh_token guardado para obtener un access_token nuevo.
    Devuelve {access_token, expires_in, scope, token_type}."""
    res = requests.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if not res.ok:
        # invalid_grant suele significar: user revocó acceso en Google,
        # refresh expiró (6 meses inactivo), o la app fue eliminada del
        # Google Cloud project. Handler debería marcar el row como
        # disconnected y pedir reconectar.
        raise DriveOAuthError(
            f"Google rechazó refresh ({res.status_code}): {res.text[:300]}"
        )
    return res.json()


def fetch_userinfo(access_token: str) -> dict:
    """Devuelve {email, name, picture, ...} del user dueño del token.
    Lo usamos solo para mostrar 'Conectado como X' en Settings."""
    res = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not res.ok:
        # No es crítico — el flow funciona sin userinfo, solo perdemos
        # el display name. Log + devolver vacío.
        logger.warning(
            "[drive_oauth] userinfo fetch failed: %s %s",
            res.status_code, res.text[:200],
        )
        return {}
    return res.json()


def revoke_refresh_token(refresh_token: str) -> bool:
    """Llama al endpoint revoke de Google. Best-effort — si falla, el
    handler igual borra la row local (el user ya no quiere conexión).
    Devuelve True si Google confirmó revoke, False si falló."""
    try:
        res = requests.post(
            REVOKE_URL,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return res.ok
    except requests.RequestException as e:
        logger.warning("[drive_oauth] revoke failed: %s", e)
        return False


# --- High-level helpers usados por endpoints + workers ---

def get_fresh_access_token(db, user_id: int) -> str:
    """Devuelve un access_token válido para `user_id`. Lee el
    refresh_token encriptado de la DB, lo decrypta, refresca contra
    Google.

    Raise DriveOAuthError si no hay tokens (user no conectó Drive)
    o si Google rechaza el refresh (user revocó).
    Raise DriveTokenDecryptError si la encryption key rotó.
    """
    from database import UserDriveTokens

    row = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == user_id).first()
    if row is None:
        raise DriveOAuthError(
            "Este usuario no tiene Drive conectado. Pedile al frontend "
            "que muestre el botón Conectar Drive."
        )

    refresh_token = decrypt_token(row.encrypted_refresh_token)
    data = refresh_access_token(refresh_token)

    # Actualizar last_used_at para que veamos qué users usan la integración.
    row.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return data["access_token"]
