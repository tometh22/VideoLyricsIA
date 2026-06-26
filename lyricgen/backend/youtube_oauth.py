"""YouTube OAuth 2.0 flow + token persistence (system-wide, single account).

A diferencia de drive_oauth.py (token por user), YouTube usa UNA cuenta
central del sistema a la que suben los videos de todos los tenants. Por eso
el token vive en una fila singleton (`system_youtube_token`), no en una
tabla por user.

Por qué este módulo existe (y no alcanzaba con youtube_token.json):
  El filesystem de Railway es efímero — se borra en cada deploy. Guardar
  el token en un archivo significaba reconectar YouTube después de cada
  deploy. Persistirlo en la DB (Fernet-encrypted) lo hace sobrevivir.

Reuso deliberado de drive_oauth.py:
  - encrypt_token / decrypt_token (misma DRIVE_TOKEN_ENCRYPTION_KEY).
  - El mismo OAuth client de Google (GOOGLE_OAUTH_CLIENT_ID/SECRET) sirve
    para Drive y YouTube; solo cambian el scope y la redirect URI. Se puede
    overridear con YOUTUBE_OAUTH_CLIENT_ID/SECRET si se quiere separar.

Flujo:
  1. Frontend pide GET /youtube/auth-url → URL de Google con state firmado.
  2. Admin autoriza en Google → callback a /youtube/callback?code&state.
  3. Backend valida state → exchange code → guarda el token completo
     Fernet-encrypted en system_youtube_token + cachea channel info.
  4. youtube_upload._get_youtube_client() lee el token de la DB, lo
     refresca si expiró, y persiste el access_token nuevo.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

# Reuso de los helpers Fernet de Drive — misma key, mismo formato.
from drive_oauth import encrypt_token, decrypt_token, DriveTokenDecryptError

logger = logging.getLogger("genly.youtube_oauth")


# --- Config (reusa el OAuth client de Google/Drive salvo override) ---

def _client_id() -> str:
    return os.environ.get("YOUTUBE_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    # No cae al de Drive: el callback de YouTube es una ruta distinta.
    return os.environ.get("YOUTUBE_OAUTH_REDIRECT_URI", "")


# youtube.upload para subir; youtube.readonly para leer el canal conectado
# (channels.list mine=true) y mostrar "Conectado como X" en Settings.
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

STATE_TOKEN_TTL_S = 600  # 10 min entre pedir auth-url y completar el callback


class YoutubeOAuthError(Exception):
    """Error del flujo OAuth que debe surface al frontend con detail claro."""
    pass


# --- State token (CSRF + binding al admin que inicia) ---

def _state_secret():
    from auth import JWT_SECRET, JWT_ALGORITHM
    return JWT_SECRET, JWT_ALGORITHM


def build_state_token(user_id: int) -> str:
    secret, alg = _state_secret()
    payload = {
        "user_id": user_id,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + STATE_TOKEN_TTL_S,
        "iat": int(time.time()),
        "type": "youtube_oauth_state",
    }
    return jwt.encode(payload, secret, algorithm=alg)


def verify_state_token(state: str) -> int:
    secret, alg = _state_secret()
    try:
        payload = jwt.decode(state, secret, algorithms=[alg])
    except ExpiredSignatureError:
        raise YoutubeOAuthError("State token expirado. Reintentá conectar YouTube.")
    except JWTError as e:
        raise YoutubeOAuthError(f"State token inválido: {e}")
    if payload.get("type") != "youtube_oauth_state":
        raise YoutubeOAuthError("State token de otro flow.")
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, int) else None


# --- OAuth flow ---

def build_authorization_url(user_id: int) -> str:
    if not _client_id():
        raise YoutubeOAuthError(
            "GOOGLE_OAUTH_CLIENT_ID (o YOUTUBE_OAUTH_CLIENT_ID) no configurada en Railway."
        )
    if not _redirect_uri():
        raise YoutubeOAuthError(
            "YOUTUBE_OAUTH_REDIRECT_URI no configurada en Railway (ej: "
            "https://api-staging-9b82.up.railway.app/youtube/callback)."
        )
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",     # → Google emite refresh_token
        "prompt": "consent",          # → refresh_token aunque ya hubiera autorizado
        "include_granted_scopes": "true",
        "state": build_state_token(user_id),
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Cambia el code por un token dict compatible con
    google.oauth2.credentials.Credentials (mismo formato que el viejo
    youtube_token.json)."""
    res = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not res.ok:
        raise YoutubeOAuthError(
            f"Google rechazó el code exchange ({res.status_code}): {res.text[:300]}"
        )
    data = res.json()
    if "refresh_token" not in data:
        raise YoutubeOAuthError(
            "Google no devolvió refresh_token. Revocá el acceso previo en "
            "myaccount.google.com → seguridad → apps de terceros y reintentá."
        )
    return {
        "token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "token_uri": TOKEN_URL,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "scopes": data.get("scope", " ".join(YOUTUBE_SCOPES)).split(),
    }


def fetch_channel_info(access_token: str) -> dict:
    """Devuelve {channel_id, channel_name, thumbnail} de la cuenta dueña
    del token. Best-effort: si falla, el flow sigue sin display name."""
    try:
        res = requests.get(
            CHANNELS_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if not res.ok:
            logger.warning("[youtube_oauth] channels.list failed: %s %s", res.status_code, res.text[:200])
            return {}
        items = res.json().get("items", [])
        if not items:
            return {}
        sn = items[0]["snippet"]
        return {
            "channel_id": items[0]["id"],
            "channel_name": sn.get("title"),
            "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url"),
        }
    except requests.RequestException as e:
        logger.warning("[youtube_oauth] channel info fetch failed: %s", e)
        return {}


def revoke_token(refresh_token: str) -> bool:
    try:
        res = requests.post(
            REVOKE_URL,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return res.ok
    except requests.RequestException as e:
        logger.warning("[youtube_oauth] revoke failed: %s", e)
        return False


# --- DB persistence (singleton row) ---

def _encryption_available() -> bool:
    return bool(os.environ.get("DRIVE_TOKEN_ENCRYPTION_KEY"))


def save_system_token(db, token_data: dict, channel: dict | None = None, user_id: int | None = None) -> None:
    """Guarda (o reemplaza) el token de la cuenta de YouTube del sistema."""
    from database import SystemYoutubeToken

    enc = encrypt_token(json.dumps(token_data))
    channel = channel or {}
    row = db.query(SystemYoutubeToken).first()
    if row is None:
        row = SystemYoutubeToken(encrypted_token_json=enc)
        db.add(row)
    else:
        row.encrypted_token_json = enc
    if channel:
        row.channel_id = channel.get("channel_id")
        row.channel_name = channel.get("channel_name")
        row.channel_thumbnail = channel.get("thumbnail")
    if user_id is not None:
        row.connected_by_user_id = user_id
    db.commit()


def load_system_token(db) -> dict | None:
    """Devuelve el token dict descifrado, o None si no hay cuenta conectada."""
    from database import SystemYoutubeToken

    row = db.query(SystemYoutubeToken).first()
    if row is None:
        return None
    return json.loads(decrypt_token(row.encrypted_token_json))


def delete_system_token(db) -> bool:
    """Borra la fila singleton. Best-effort revoke contra Google primero."""
    from database import SystemYoutubeToken

    row = db.query(SystemYoutubeToken).first()
    if row is None:
        return False
    try:
        token_data = json.loads(decrypt_token(row.encrypted_token_json))
        if token_data.get("refresh_token"):
            revoke_token(token_data["refresh_token"])
    except Exception:
        pass  # revoke es best-effort; igual borramos la fila
    db.delete(row)
    db.commit()
    return True


def get_channel_meta(db) -> dict | None:
    """Channel info cacheada (sin descifrar el token). Para el status display."""
    from database import SystemYoutubeToken

    row = db.query(SystemYoutubeToken).first()
    if row is None:
        return None
    return {
        "channel_id": row.channel_id,
        "channel_name": row.channel_name,
        "thumbnail": row.channel_thumbnail,
        "channel_url": f"https://youtube.com/channel/{row.channel_id}" if row.channel_id else None,
    }


# --- Standalone helpers para youtube_upload.py (corre en run_in_executor,
#     thread aparte → crea su propia sesión en vez de reusar la de FastAPI) ---

def load_system_token_standalone() -> dict | None:
    """Carga el token desde la DB con una sesión propia. Devuelve None si
    no hay cuenta conectada o si la encriptación no está disponible (dev)."""
    if not _encryption_available():
        return None
    try:
        from database import SessionLocal
        db = SessionLocal()
        try:
            return load_system_token(db)
        finally:
            db.close()
    except DriveTokenDecryptError:
        raise
    except Exception as e:
        logger.warning("[youtube_oauth] load_system_token_standalone failed: %s", e)
        return None


def update_access_token_standalone(token_data: dict) -> None:
    """Persiste un access_token refrescado (sesión propia). Best-effort."""
    if not _encryption_available():
        return
    try:
        from database import SessionLocal
        db = SessionLocal()
        try:
            save_system_token(db, token_data)
        finally:
            db.close()
    except Exception as e:
        logger.warning("[youtube_oauth] update_access_token_standalone failed: %s", e)
