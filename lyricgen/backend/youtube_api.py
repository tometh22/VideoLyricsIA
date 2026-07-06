"""YouTube channel connections API — self-service OAuth per tenant.

Flow: POST /youtube/channels/connect returns a Google consent URL; the
browser round-trips through Google and lands on GET /youtube/oauth/callback,
which exchanges the code, fetches the channel identity, stores the token
encrypted (token_crypto) and redirects back to the frontend settings page.
The callback is unauthenticated by nature (it's a redirect from Google) —
the caller's identity travels in the signed `state` JWT.
"""

import logging
import os
import secrets
from datetime import datetime, timezone

import requests as _requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from auth import get_current_user, JWT_SECRET, JWT_ALGORITHM
from database import AuditLog, YouTubeChannel, get_db
from token_crypto import encrypt_token

logger = logging.getLogger("genly.youtube")

router = APIRouter(prefix="/youtube", tags=["youtube"])

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# yt-analytics.readonly is requested from day one so the analytics phase
# never forces channel owners to reconnect.
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

_STATE_TTL_S = 600
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class YouTubeOAuthNotConfiguredError(RuntimeError):
    pass


def _oauth_client_config() -> dict:
    client_id = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise YouTubeOAuthNotConfiguredError(
            "YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET are not set."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _redirect_uri() -> str:
    uri = os.environ.get("YOUTUBE_OAUTH_REDIRECT_URI", "").strip()
    if not uri:
        raise YouTubeOAuthNotConfiguredError("YOUTUBE_OAUTH_REDIRECT_URI is not set.")
    return uri


def _make_flow():
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(
        _oauth_client_config(), scopes=OAUTH_SCOPES, redirect_uri=_redirect_uri(),
    )


def _sign_state(user_id: int, tenant_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "purpose": "yt_oauth",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "nonce": secrets.token_urlsafe(8),
        "iat": now,
        "exp": now.timestamp() + _STATE_TTL_S,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_state(state: str) -> dict:
    payload = jwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("purpose") != "yt_oauth":
        raise JWTError("wrong purpose")
    return payload


def _fetch_channel_info(credentials) -> dict:
    """Return {channel_id, title, thumbnail_url} for the authorized account."""
    from googleapiclient.discovery import build

    yt = build("youtube", "v3", credentials=credentials)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise ValueError("The Google account has no YouTube channel.")
    snippet = items[0].get("snippet", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url")
    return {
        "channel_id": items[0]["id"],
        "title": snippet.get("title", ""),
        "thumbnail_url": thumb,
    }


def _credentials_to_token_dict(creds) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or []),
        "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
    }


def _settings_redirect(**params) -> RedirectResponse:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{FRONTEND_URL}/?view=settings&{query}", status_code=302)


def _audit_row(db: Session, user_id, action: str, detail: dict) -> None:
    db.add(AuditLog(user_id=user_id, action=action, detail=detail))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the tenant's connected channels. Never serializes tokens."""
    rows = (
        db.query(YouTubeChannel)
        .filter(YouTubeChannel.tenant_id == current_user["tenant_id"])
        .order_by(YouTubeChannel.is_default.desc(), YouTubeChannel.created_at.asc())
        .all()
    )
    return [r.to_dict() for r in rows]


@router.post("/channels/connect")
async def connect_channel(
    current_user: dict = Depends(get_current_user),
):
    """Start the OAuth flow: returns the Google consent URL."""
    try:
        flow = _make_flow()
    except YouTubeOAuthNotConfiguredError as e:
        logger.warning("YouTube OAuth not configured: %s", e)
        raise HTTPException(
            status_code=503,
            detail="La conexión con YouTube no está configurada en este servidor.",
        )

    state = _sign_state(current_user["id"], current_user["tenant_id"])
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",       # force a refresh_token even on re-consent
        state=state,
        include_granted_scopes="true",
    )
    return {"auth_url": auth_url}


@router.get("/oauth/callback")
async def oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    """OAuth redirect target. Always redirects back to the frontend."""
    if error:
        # User canceled on Google's consent screen.
        return _settings_redirect(youtube_error="access_denied" if error == "access_denied" else "google")

    try:
        payload = _verify_state(state)
    except JWTError:
        return _settings_redirect(youtube_error="state")

    user_id = payload["user_id"]
    tenant_id = payload["tenant_id"]

    try:
        flow = _make_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        info = _fetch_channel_info(creds)
    except YouTubeOAuthNotConfiguredError:
        return _settings_redirect(youtube_error="not_configured")
    except ValueError:
        return _settings_redirect(youtube_error="no_channel")
    except Exception:
        logger.exception("YouTube OAuth callback failed (tenant %s)", tenant_id)
        return _settings_redirect(youtube_error="exchange")

    token_blob = encrypt_token(_credentials_to_token_dict(creds))

    existing = (
        db.query(YouTubeChannel)
        .filter(
            YouTubeChannel.tenant_id == tenant_id,
            YouTubeChannel.channel_id == info["channel_id"],
        )
        .first()
    )
    if existing:
        existing.token_encrypted = token_blob
        existing.channel_title = info["title"]
        existing.thumbnail_url = info["thumbnail_url"]
        existing.scopes = list(creds.scopes or [])
        existing.status = "active"
        existing.last_refresh_error = None
        existing.connected_by = user_id
        channel = existing
    else:
        has_channels = (
            db.query(YouTubeChannel.id)
            .filter(YouTubeChannel.tenant_id == tenant_id)
            .first()
            is not None
        )
        channel = YouTubeChannel(
            tenant_id=tenant_id,
            channel_id=info["channel_id"],
            channel_title=info["title"],
            thumbnail_url=info["thumbnail_url"],
            token_encrypted=token_blob,
            scopes=list(creds.scopes or []),
            connected_by=user_id,
            status="active",
            is_default=not has_channels,
        )
        db.add(channel)

    _audit_row(db, user_id, "youtube.channel_connected", {
        "tenant_id": tenant_id,
        "channel_id": info["channel_id"],
        "channel_title": info["title"],
    })
    db.commit()

    return _settings_redirect(youtube_connected="1")


@router.delete("/channels/{channel_pk}")
async def disconnect_channel(
    channel_pk: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnect a channel: best-effort token revoke at Google, then mark
    the row revoked (kept for the audit trail; reconnect revives it)."""
    channel = (
        db.query(YouTubeChannel)
        .filter(
            YouTubeChannel.id == channel_pk,
            YouTubeChannel.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found.")

    if channel.token_encrypted:
        try:
            from token_crypto import decrypt_token
            token = decrypt_token(channel.token_encrypted)
            refresh_token = token.get("refresh_token")
            if refresh_token:
                _requests.post(
                    _GOOGLE_REVOKE_URL, params={"token": refresh_token}, timeout=5,
                )
        except Exception as e:  # best-effort: Google-side revoke may fail
            logger.warning("Token revoke failed for channel %s: %s", channel.channel_id, e)

    channel.token_encrypted = None
    channel.status = "revoked"
    was_default = channel.is_default
    channel.is_default = False

    if was_default:
        replacement = (
            db.query(YouTubeChannel)
            .filter(
                YouTubeChannel.tenant_id == current_user["tenant_id"],
                YouTubeChannel.status == "active",
                YouTubeChannel.id != channel.id,
            )
            .order_by(YouTubeChannel.created_at.asc())
            .first()
        )
        if replacement:
            replacement.is_default = True

    _audit_row(db, current_user["id"], "youtube.channel_disconnected", {
        "tenant_id": current_user["tenant_id"],
        "channel_id": channel.channel_id,
        "channel_title": channel.channel_title,
    })
    db.commit()
    return {"ok": True}


@router.post("/channels/{channel_pk}/default")
async def set_default_channel(
    channel_pk: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = (
        db.query(YouTubeChannel)
        .filter(
            YouTubeChannel.id == channel_pk,
            YouTubeChannel.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found.")
    if channel.status != "active":
        raise HTTPException(status_code=400, detail="Channel is not active.")

    # Clear-then-set in one transaction (the partial unique index is the
    # DB-level guarantee on Postgres; this keeps SQLite correct too).
    db.query(YouTubeChannel).filter(
        YouTubeChannel.tenant_id == current_user["tenant_id"],
        YouTubeChannel.is_default.is_(True),
    ).update({YouTubeChannel.is_default: False}, synchronize_session=False)
    channel.is_default = True
    db.commit()
    return {"ok": True}
