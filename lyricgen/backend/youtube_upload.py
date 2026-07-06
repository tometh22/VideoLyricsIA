"""YouTube upload module — generates metadata with AI and uploads to YouTube."""

import json
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

_TOKEN_PATH = os.environ.get(
    "YOUTUBE_TOKEN_PATH",
    os.path.join(os.path.dirname(__file__), "youtube_token.json"),
)
# Make relative paths relative to backend dir
if not os.path.isabs(_TOKEN_PATH):
    _TOKEN_PATH = os.path.join(os.path.dirname(__file__), _TOKEN_PATH)

class YouTubeNotConfiguredError(RuntimeError):
    """No usable YouTube credential (no channel connected / token broken)."""


_TITLE_NOISE_SUFFIXES = (
    "(Official Video)", "(Official Audio)", "(Lyric Video)",
    "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
    "(Live)", "(Lyrics)",
)


def parse_filename_artist_title(filename: str) -> tuple:
    """Best-effort artist/title extraction from a bare filename. Handles two
    naming conventions the operator commonly uploads under:

      "Artist - Title.ext"   → ("Artist", "Title")
      "Title_Artist.ext"     → ("Artist", "Title")   ← Suno/YouTube export form

    Falls back to ("", basename) when neither separator is present so the
    caller can decide whether to insist on a manual entry. Studio-version
    suffixes like "(Official Video)" are stripped from the title in either
    case so the lrclib lookup matches.
    """
    if not filename:
        return "", ""
    base = filename
    for ext in (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    artist, title = "", base.strip()
    if " - " in base:
        head, _, tail = base.partition(" - ")
        artist, title = head.strip(), tail.strip()
    elif "_" in base:
        head, _, tail = base.partition("_")
        title, artist = head.strip(), tail.strip()
    for sfx in _TITLE_NOISE_SUFFIXES:
        title = title.replace(sfx, "").strip()
    return artist, title


def job_song_title(job: dict) -> str:
    """Song title for YouTube metadata: prefer the title the user supplied
    (or the upload-time parse) over re-deriving it from the raw filename."""
    return job.get("song_title") or parse_filename_artist_title(job.get("filename", ""))[1]


def load_user_settings(db, user_id: int):
    """Per-user settings from the DB; None when the user has none saved so
    the legacy outputs/_settings.json fallback still applies."""
    from database import UserSettings

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    return (settings.settings_json or None) if settings else None


def settings_for_job(db, job_id: str, fallback_user_id: int):
    """The YouTube template to apply to a job: the job owner's settings, so a
    teammate publishing someone else's job still gets the configured branding."""
    from database import Job

    owner_id = db.query(Job.user_id).filter(Job.job_id == job_id).scalar()
    settings = load_user_settings(db, owner_id) if owner_id else None
    if settings is None and owner_id != fallback_user_id:
        settings = load_user_settings(db, fallback_user_id)
    return settings


def resolve_channel(db, tenant_id: str, channel_pk: int = None):
    """Pick the channel to publish with: explicit id → tenant default →
    None (legacy file-token fallback). Raises 404-shaped ValueError for an
    explicit id that isn't the tenant's or isn't active."""
    from database import YouTubeChannel

    if channel_pk is not None:
        channel = (
            db.query(YouTubeChannel)
            .filter(YouTubeChannel.id == channel_pk, YouTubeChannel.tenant_id == tenant_id)
            .first()
        )
        if channel is None:
            raise ValueError("Channel not found.")
        if channel.status != "active":
            raise YouTubeNotConfiguredError(f"Channel {channel.channel_title} needs reconnection.")
        return channel

    return (
        db.query(YouTubeChannel)
        .filter(
            YouTubeChannel.tenant_id == tenant_id,
            YouTubeChannel.status == "active",
            YouTubeChannel.is_default.is_(True),
        )
        .first()
    )


def _mark_channel_error(channel_pk: int, error: str) -> None:
    from database import SessionLocal, YouTubeChannel
    from datetime import datetime, timezone

    s = SessionLocal()
    try:
        row = s.query(YouTubeChannel).filter(YouTubeChannel.id == channel_pk).first()
        if row:
            row.status = "error"
            row.last_refresh_error = error[:1000]
            row.last_refresh_at = datetime.now(timezone.utc)
            s.commit()
    finally:
        s.close()


def _persist_refreshed_token(channel_pk: int, token_data: dict) -> None:
    """Save the rotated access token after a refresh. Last-writer-wins is
    fine: Google keeps the refresh_token stable across refreshes."""
    from database import SessionLocal, YouTubeChannel
    from token_crypto import encrypt_token
    from datetime import datetime, timezone

    s = SessionLocal()
    try:
        row = s.query(YouTubeChannel).filter(YouTubeChannel.id == channel_pk).first()
        if row:
            row.token_encrypted = encrypt_token(token_data)
            row.last_refresh_at = datetime.now(timezone.utc)
            row.last_refresh_error = None
            s.commit()
    finally:
        s.close()


def _credentials_from_channel(channel) -> Credentials:
    from token_crypto import decrypt_token

    if not channel.token_encrypted:
        raise YouTubeNotConfiguredError("Channel has no stored token (disconnected?).")
    token_data = decrypt_token(channel.token_encrypted)
    return Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", ""),
        scopes=token_data.get("scopes"),
    )


def _get_youtube_client(channel=None):
    """Authenticated YouTube API client.

    channel=None → legacy single-token file (backward compatible with
    deployments that predate self-service connections). channel row →
    decrypt from DB, proactively refresh if expired, persist the rotated
    access token.
    """
    if channel is None:
        try:
            with open(_TOKEN_PATH) as f:
                token_data = json.load(f)
        except FileNotFoundError:
            raise YouTubeNotConfiguredError(
                f"YouTube token file not found at {_TOKEN_PATH}. "
                "Connect a channel in Settings or place a token file there."
            )
        except json.JSONDecodeError as e:
            raise YouTubeNotConfiguredError(f"YouTube token file is not valid JSON: {e}")

        try:
            creds = Credentials(
                token=token_data["token"],
                refresh_token=token_data["refresh_token"],
                token_uri=token_data["token_uri"],
                client_id=token_data["client_id"],
                client_secret=token_data["client_secret"],
                scopes=token_data["scopes"],
            )
        except KeyError as e:
            raise YouTubeNotConfiguredError(f"YouTube token file is missing key: {e}")

        return build("youtube", "v3", credentials=creds)

    creds = _credentials_from_channel(channel)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError

        try:
            creds.refresh(Request())
        except RefreshError as e:
            _mark_channel_error(channel.id, str(e))
            raise YouTubeNotConfiguredError(
                f"Channel {channel.channel_title} token was revoked/expired; reconnect it."
            )
        _persist_refreshed_token(channel.id, {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "scopes": list(creds.scopes or []),
            "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
        })

    return build("youtube", "v3", credentials=creds)


_LANG_NAMES = {
    "es": "Spanish", "en": "English", "pt": "Portuguese",
    "fr": "French", "it": "Italian", "de": "German",
}


def _load_settings() -> dict:
    """Legacy fallback: client settings from the pre-DB JSON file.

    Settings now live per-user in the DB (UserSettings) and callers pass
    them in explicitly; this file is only read when no settings are given.
    """
    settings_path = os.path.join(os.path.dirname(__file__), "..", "outputs", "_settings.json")
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            return json.load(f)
    return {}


def apply_settings_template(metadata: dict, artist: str, song: str, settings: dict) -> dict:
    """Apply the client's YouTube template settings on top of AI metadata.

    Pure function: mutates and returns `metadata`. Handles titleFormat,
    description header/footer, hashtags and mandatory tags with the
    {artista}/{cancion} placeholders.
    """
    def fill(tpl: str) -> str:
        return tpl.replace("{artista}", artist).replace("{cancion}", song)

    title_fmt = settings.get("titleFormat", "")
    if title_fmt:
        metadata["title"] = fill(title_fmt)

    desc_parts = []
    header = settings.get("descriptionHeader", "")
    if header:
        desc_parts.append(fill(header))
    desc_parts.append(metadata.get("description", ""))
    footer = settings.get("descriptionFooter", "")
    if footer:
        desc_parts.append(fill(footer))
    hashtags = settings.get("hashtags", "")
    if hashtags:
        desc_parts.append(fill(hashtags))
    metadata["description"] = "\n\n".join(desc_parts)

    mandatory = settings.get("mandatoryTags", "")
    if mandatory:
        extra_tags = [t.strip() for t in mandatory.split(",") if t.strip()]
        metadata["tags"] = extra_tags + metadata.get("tags", [])

    return metadata


def generate_youtube_metadata(
    artist: str,
    song: str,
    lyrics_text: str = "",
    job_id: str = None,
    settings: dict = None,
) -> dict:
    """Use Gemini to generate optimized YouTube metadata."""
    from pipeline import _get_genai_client
    from google import genai
    from provenance import record_ai_call

    client = _get_genai_client()

    if settings is None:
        settings = _load_settings()
    lang_name = _LANG_NAMES.get(settings.get("metadataLanguage", "es"), "Spanish")

    prompt_content = (
        f"Artist: {artist}\nSong: {song}\n"
        f"Lyrics preview: {lyrics_text[:300]}\n\n"
        f"Generate YouTube video metadata for a lyric video following YouTube SEO best practices. "
        f"Write ALL metadata in {lang_name} (title, description, tags). "
        f"Respond ONLY with JSON: {{\"title\":\"...\",\"description\":\"...\",\"tags\":[\"...\"]}}\n\n"
        f"TITLE rules (YouTube SEO):\n"
        f"- Format: '{artist} - {song} (Letra/Lyrics)'\n"
        f"- Max 70 chars (YouTube truncates longer titles in search)\n"
        f"- Put the most important keywords first (artist + song name)\n"
        f"- Include (Letra/Lyrics) at the end for bilingual discovery\n\n"
        f"DESCRIPTION rules (YouTube SEO):\n"
        f"- First 2 lines are critical (shown before 'Show more') — include artist, song, and 'lyric video'\n"
        f"- Include a brief song description or quote from the lyrics\n"
        f"- Add timestamps: 0:00 {song}\n"
        f"- Credit line: '{artist} - {song}'\n"
        f"- 3 relevant hashtags at the very end (YouTube shows first 3 above title)\n"
        f"- Include '#lyrics #letra #{{artist without spaces}}' as the 3 hashtags\n"
        f"- Total description: 800-1500 chars (longer descriptions rank better)\n"
        f"- Include related search phrases naturally: 'lyric video', 'letra completa', 'con letra'\n\n"
        f"TAGS rules (YouTube SEO):\n"
        f"- 15-20 tags, mix of broad and specific\n"
        f"- Start with exact match: '{artist} {song} lyrics', '{artist} {song} letra'\n"
        f"- Include variations: '{song} lyrics', '{artist} lyrics', '{artist} letra'\n"
        f"- Include genre tags\n"
        f"- Include 'lyric video', 'letra', 'con letra', 'lyrics video', 'official lyrics'\n"
        f"- Include '{artist}' alone as a tag\n"
        f"- Include '{song}' alone as a tag\n"
        f"- Include Spanish and English versions of key terms\n"
    )

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="yt_metadata",
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        prompt=prompt_content,
        input_data_types=["artist_name", "song_name", "lyrics_preview_300chars"],
    ) if job_id else None

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt_content,
        config=genai.types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=1000,
            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        ),
    )

    import re
    text = response.text.strip()
    if recorder:
        recorder.finish(response_summary=text[:500])
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    metadata = None
    if json_match:
        try:
            metadata = json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # The model may return parseable JSON that is still missing keys, so
    # merge over the fallback instead of only using it on a total parse
    # failure — apply_settings_template and the upload body need all four.
    fallback = {
        "title": f"{artist} - {song} (Lyrics)",
        "description": f"{artist} - {song} (Lyric Video)\n\n#lyrics #letra",
        "tags": [artist, song, "lyrics", "letra", "lyric video", "music"],
        "category": "10",
    }
    if isinstance(metadata, dict):
        metadata = {**fallback, **{k: v for k, v in metadata.items() if v}}
    else:
        metadata = fallback

    # Apply client settings (template overrides)
    return apply_settings_template(metadata, artist, song, settings)


def upload_to_youtube(
    video_path: str,
    thumbnail_path: str,
    artist: str,
    song: str,
    lyrics_text: str = "",
    privacy: str = "unlisted",
    job_id: str = None,
    metadata: dict = None,
    settings: dict = None,
    channel=None,
    progress_callback=None,
) -> dict:
    """Upload video + thumbnail to YouTube.

    If `metadata` is given (e.g. the exact metadata the user previewed and
    approved in the UI) it is used as-is; otherwise it is AI-generated.
    `channel` is a YouTubeChannel row (None → legacy file token).
    `progress_callback(percent)` is invoked as resumable-upload chunks land.
    Returns dict with video_id and url.
    """
    if settings is None:
        settings = _load_settings()

    if metadata is None:
        print(f"[YOUTUBE] Generating metadata for '{artist} - {song}'...")
        metadata = generate_youtube_metadata(artist, song, lyrics_text, job_id=job_id, settings=settings)
    print(f"[YOUTUBE] Title: {metadata['title']}")

    youtube = _get_youtube_client(channel)

    default_lang = settings.get("metadataLanguage", "es")

    # Upload video
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata.get("tags", []),
            "categoryId": metadata.get("category", "10"),
            "defaultLanguage": default_lang,
        },
        "status": {
            "privacyStatus": privacy,  # unlisted for testing, public for production
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"[YOUTUBE] Uploading video ({os.path.getsize(video_path)/1024/1024:.1f} MB)...")
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        # num_retries: exponential-backoff retry on transient 5xx / connection
        # errors so a network blip mid-upload doesn't abort the whole video.
        status, response = request.next_chunk(num_retries=5)
        if status:
            percent = int(status.progress() * 100)
            print(f"[YOUTUBE] Upload progress: {percent}%")
            if progress_callback:
                try:
                    progress_callback(percent)
                except Exception:
                    pass  # progress reporting must never kill the upload

    video_id = response["id"]
    print(f"[YOUTUBE] Video uploaded: https://youtube.com/watch?v={video_id}")

    # Upload thumbnail
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            print("[YOUTUBE] Setting thumbnail...")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
            ).execute()
            print("[YOUTUBE] Thumbnail set!")
        except Exception as e:
            print(f"[YOUTUBE] Thumbnail failed (needs verified account): {e}")

    result = {
        "video_id": video_id,
        "url": f"https://youtube.com/watch?v={video_id}",
        "title": metadata["title"],
        "privacy": privacy,
    }
    if channel is not None:
        result["channel_id"] = channel.channel_id
        result["channel_title"] = channel.channel_title
    return result


def update_video_privacy(video_id: str, privacy: str, channel=None) -> None:
    """Change the privacy status of an already-published video."""
    youtube = _get_youtube_client(channel)
    youtube.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        },
    ).execute()
    print(f"[YOUTUBE] Privacy of {video_id} changed to {privacy}")
