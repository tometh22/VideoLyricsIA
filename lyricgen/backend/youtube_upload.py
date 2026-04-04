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

_OAUTH_PATH = os.environ.get(
    "YOUTUBE_OAUTH_PATH",
    os.path.join(os.path.dirname(__file__), "youtube_oauth.json"),
)
if not os.path.isabs(_OAUTH_PATH):
    _OAUTH_PATH = os.path.join(os.path.dirname(__file__), _OAUTH_PATH)


def _get_youtube_client():
    """Get authenticated YouTube API client."""
    with open(_TOKEN_PATH) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )

    return build("youtube", "v3", credentials=creds)


_LANG_NAMES = {
    "es": "Spanish", "en": "English", "pt": "Portuguese",
    "fr": "French", "it": "Italian", "de": "German",
}


def _get_language_name() -> str:
    settings = _load_settings()
    code = settings.get("metadataLanguage", "es")
    return _LANG_NAMES.get(code, "Spanish")


def _load_settings() -> dict:
    """Load client settings for YouTube template."""
    settings_path = os.path.join(os.path.dirname(__file__), "..", "outputs", "_settings.json")
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            return json.load(f)
    return {}


def generate_youtube_metadata(artist: str, song: str, lyrics_text: str = "") -> dict:
    """Use Gemini to generate optimized YouTube metadata."""
    from pipeline import _get_genai_client
    from google import genai

    client = _get_genai_client()

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"Artist: {artist}\nSong: {song}\n"
            f"Lyrics preview: {lyrics_text[:300]}\n\n"
            f"Generate YouTube video metadata for a lyric video following YouTube SEO best practices. "
            f"Write ALL metadata in {_get_language_name()} (title, description, tags). "
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
        ),
        config=genai.types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=1000,
            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        ),
    )

    import re
    text = response.text.strip()
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    metadata = None
    if json_match:
        try:
            metadata = json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    if not metadata:
        metadata = {
            "title": f"{artist} - {song} (Lyrics)",
            "description": f"{artist} - {song} (Lyric Video)\n\n#lyrics #letra",
            "tags": [artist, song, "lyrics", "letra", "lyric video", "music"],
            "category": "10",
        }

    # Apply client settings (template overrides)
    settings = _load_settings()

    # Title from template
    title_fmt = settings.get("titleFormat", "")
    if title_fmt:
        metadata["title"] = title_fmt.replace("{artista}", artist).replace("{cancion}", song)

    # Description: header + AI description + footer + hashtags
    desc_parts = []
    header = settings.get("descriptionHeader", "")
    if header:
        desc_parts.append(header.replace("{artista}", artist).replace("{cancion}", song))
    desc_parts.append(metadata["description"])
    footer = settings.get("descriptionFooter", "")
    if footer:
        desc_parts.append(footer.replace("{artista}", artist).replace("{cancion}", song))
    hashtags = settings.get("hashtags", "")
    if hashtags:
        desc_parts.append(hashtags.replace("{artista}", artist).replace("{cancion}", song))
    metadata["description"] = "\n\n".join(desc_parts)

    # Add mandatory tags
    mandatory = settings.get("mandatoryTags", "")
    if mandatory:
        extra_tags = [t.strip() for t in mandatory.split(",") if t.strip()]
        metadata["tags"] = extra_tags + metadata.get("tags", [])

    return metadata


def upload_to_youtube(
    video_path: str,
    thumbnail_path: str,
    artist: str,
    song: str,
    lyrics_text: str = "",
    privacy: str = "unlisted",
) -> dict:
    """Upload video + thumbnail to YouTube with AI-generated metadata.

    Returns dict with video_id and url.
    """
    print(f"[YOUTUBE] Generating metadata for '{artist} - {song}'...")
    metadata = generate_youtube_metadata(artist, song, lyrics_text)
    print(f"[YOUTUBE] Title: {metadata['title']}")

    youtube = _get_youtube_client()

    # Upload video
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata.get("tags", []),
            "categoryId": metadata.get("category", "10"),
            "defaultLanguage": "es",
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
        status, response = request.next_chunk()
        if status:
            print(f"[YOUTUBE] Upload progress: {int(status.progress() * 100)}%")

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

    return {
        "video_id": video_id,
        "url": f"https://youtube.com/watch?v={video_id}",
        "title": metadata["title"],
        "privacy": privacy,
    }
