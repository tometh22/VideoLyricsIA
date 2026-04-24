"""FastAPI application for GenLy AI."""

import json
import os
import shutil
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, File, Form, Query, UploadFile, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from auth import (
    authenticate_user,
    create_token,
    create_user,
    get_current_user,
    get_current_user_from_token_param,
    get_plan_usage,
    PLANS,
)
import storage
from jobs import create_job, get_job, get_all_jobs, update_job
from pipeline import run_pipeline, transcribe
from queue_jobs import enqueue_pipeline, queue_depth
from render_spec import umg_catalog, validate_umg_config

OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

app = FastAPI(title="GenLy AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth endpoints (public)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    tenant_id: str = "default"


@app.post("/auth/login")
async def login(body: LoginRequest):
    """Authenticate and return a JWT token."""
    user = authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user)
    return {"token": token, "user": user}


@app.post("/auth/register")
async def register(body: RegisterRequest, current_user: dict = Depends(get_current_user)):
    """Create a new user (admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        user = create_user(
            username=body.username,
            password=body.password,
            role=body.role,
            tenant_id=body.tenant_id,
        )
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Return current user info."""
    return current_user


@app.get("/usage")
async def usage(current_user: dict = Depends(get_current_user)):
    """Return current plan usage with overage info."""
    return get_plan_usage(current_user["tenant_id"], current_user.get("plan", "100"))


# ---------------------------------------------------------------------------
# Protected endpoints
# ---------------------------------------------------------------------------

def _enforce_plan_quota(current_user: dict) -> None:
    """Raise 402 if the tenant reached its monthly limit without overage allowed."""
    plan = current_user.get("plan", "100")
    tenant_id = current_user["tenant_id"]
    usage = get_plan_usage(tenant_id, plan)
    if usage["remaining"] <= 0 and plan != "unlimited":
        if not current_user.get("allow_overage", False):
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Plan '{plan}' monthly limit reached "
                    f"({usage['used']}/{usage['limit']}). "
                    "Upgrade the plan or enable overage to continue."
                ),
            )


def _parse_umg_params(
    delivery_profile: str,
    umg_frame_size: str,
    umg_fps: str,
    umg_prores_profile: str,
) -> dict | None:
    """Parse and validate UMG delivery params. Returns umg_spec dict or None."""
    if delivery_profile not in ("youtube", "umg", "both"):
        raise HTTPException(
            status_code=400,
            detail="delivery_profile must be one of: youtube, umg, both",
        )
    if delivery_profile == "youtube":
        return None
    if not (umg_frame_size and umg_fps and umg_prores_profile):
        raise HTTPException(
            status_code=400,
            detail="umg_frame_size, umg_fps and umg_prores_profile are required "
                   "when delivery_profile includes UMG",
        )
    try:
        fps_val = float(umg_fps)
        profile_val = int(umg_prores_profile)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="umg_fps must be a number and umg_prores_profile an integer",
        )
    errors = validate_umg_config(umg_frame_size, fps_val, profile_val)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "frame_size": umg_frame_size,
        "fps": fps_val,
        "prores_profile": profile_val,
    }


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    artist: str = Form(...),
    style: str = Form("oscuro"),
    language: str = Form(""),
    delivery_profile: str = Form("youtube"),
    umg_frame_size: str = Form(""),
    umg_fps: str = Form(""),
    umg_prores_profile: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """Receive an MP3 and start processing."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    _enforce_plan_quota(current_user)

    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile)

    tenant_id = current_user["tenant_id"]
    job_id = create_job(
        artist=artist, style=style, filename=file.filename, tenant_id=tenant_id,
        delivery_profile=delivery_profile, umg_spec=umg_spec,
    )
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    lang = language.strip() if language.strip() else None

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=current_user.get("plan", "100"),
        language=lang,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
    )

    return {"job_id": job_id}


@app.post("/transcribe")
async def transcribe_endpoint(
    file: UploadFile = File(...),
    language: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """Transcribe an MP3 and return segments for review/editing."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    import tempfile
    import asyncio

    # Save with original filename so transcribe() can extract artist/song
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, file.filename)
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        lang = language.strip() if language.strip() else None
        loop = asyncio.get_event_loop()
        segments = await loop.run_in_executor(None, transcribe, tmp_path, lang)

        # Fetch reference lyrics for the review screen
        from pipeline import _fetch_lyrics_from_sources
        basename = os.path.splitext(file.filename)[0]
        artist_hint, song_hint = "", basename
        if " - " in basename:
            artist_hint, song_hint = basename.split(" - ", 1)
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(En Vivo)", "(Live)", "(Lyrics)",
                     "- River Plate", "- Luna Park", "- En Vivo"]:
            song_hint = song_hint.replace(sfx, "").strip()

        sources = await loop.run_in_executor(None, _fetch_lyrics_from_sources, artist_hint, song_hint)
        # Use the longest source as reference
        reference = max(sources, key=len) if sources else ""

        return {"segments": segments, "reference_lyrics": reference}
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


class GenerateRequest(BaseModel):
    artist: str
    style: str = "oscuro"
    language: str = ""
    segments: list[dict]  # [{start, end, text}]


@app.post("/generate")
async def generate_with_segments(
    file: UploadFile = File(...),
    artist: str = Form(...),
    style: str = Form("oscuro"),
    language: str = Form(""),
    segments_json: str = Form(...),
    delivery_profile: str = Form("youtube"),
    umg_frame_size: str = Form(""),
    umg_fps: str = Form(""),
    umg_prores_profile: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    """Generate video using user-edited segments (skips Whisper)."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    _enforce_plan_quota(current_user)

    segments = json.loads(segments_json)
    umg_spec = _parse_umg_params(delivery_profile, umg_frame_size, umg_fps, umg_prores_profile)

    tenant_id = current_user["tenant_id"]
    job_id = create_job(
        artist=artist, style=style, filename=file.filename, tenant_id=tenant_id,
        delivery_profile=delivery_profile, umg_spec=umg_spec,
    )
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    enqueue_pipeline(
        job_id=job_id,
        mp3_path=mp3_path,
        artist=artist,
        style=style,
        plan=current_user.get("plan", "100"),
        segments_override=segments,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
    )

    return {"job_id": job_id}


@app.get("/admin/queue")
async def admin_queue(current_user: dict = Depends(get_current_user)):
    """Return queue depth per priority. Admin only."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return queue_depth()


@app.get("/delivery-profiles")
async def get_delivery_profiles(current_user: dict = Depends(get_current_user)):
    """Return the catalog of accepted UMG specs for frontend dropdowns."""
    return {
        "profiles": ["youtube", "umg", "both"],
        "umg": umg_catalog(),
    }


@app.get("/status/{job_id}")
async def status(job_id: str, current_user: dict = Depends(get_current_user)):
    tenant_id = current_user["tenant_id"]
    job = get_job(job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "current_step": job["current_step"],
        "progress": job["progress"],
        "files": job["files"],
        "error": job.get("error"),
        "artist": job.get("artist"),
        "filename": job.get("filename"),
        "created_at": job.get("created_at"),
    }


@app.get("/jobs")
async def list_jobs(current_user: dict = Depends(get_current_user)):
    tenant_id = current_user["tenant_id"]
    jobs = get_all_jobs(tenant_id=tenant_id)
    return [
        {
            "job_id": j["job_id"],
            "status": j["status"],
            "artist": j.get("artist", ""),
            "filename": j.get("filename", ""),
            "created_at": j.get("created_at"),
        }
        for j in jobs
    ]


FILE_MAP = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
}

MEDIA_TYPES = {
    "video": "video/mp4",
    "short": "video/mp4",
    "thumbnail": "image/jpeg",
    "umg_master": "video/quicktime",
}

# File types that can't be previewed in-browser (ProRes is not browser-playable).
NON_PREVIEWABLE = {"umg_master"}


@app.get("/download/{job_id}/{file_type}")
async def download(job_id: str, file_type: str, token: str = Query(...)):
    # Auth via query param (needed for <a href> downloads)
    current_user = get_current_user_from_token_param(token)
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    tenant_id = current_user["tenant_id"]
    job = get_job(job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")

    # Prefer a pre-signed URL to R2 so the uvicorn worker isn't tied up
    # streaming multi-GB ProRes masters.
    s3_key = (job.get("s3_keys") or {}).get(file_type)
    if s3_key and storage.is_enabled():
        url = storage.generate_signed_url(s3_key, expiry_seconds=3600)
        if url:
            return RedirectResponse(url, status_code=302)

    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, filename=FILE_MAP[file_type], media_type="application/octet-stream")


@app.get("/preview/{job_id}/{file_type}")
async def preview(job_id: str, file_type: str, token: str = Query(...)):
    # Auth via query param (needed for <video src> and <img src>)
    current_user = get_current_user_from_token_param(token)
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    if file_type in NON_PREVIEWABLE:
        raise HTTPException(
            status_code=415,
            detail=f"{file_type} is a delivery master and cannot be previewed in-browser. "
                   f"Use /download/{job_id}/{file_type} instead.",
        )
    tenant_id = current_user["tenant_id"]
    job = get_job(job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")
    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, media_type=MEDIA_TYPES[file_type])


def _settings_path(tenant_id: str = "default") -> str:
    """Return settings file path for a tenant."""
    if tenant_id == "default":
        return os.path.join(OUTPUTS_DIR, "_settings.json")
    return os.path.join(OUTPUTS_DIR, f"_settings_{tenant_id}.json")


@app.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)):
    """Return app settings for the current tenant."""
    path = _settings_path(current_user["tenant_id"])
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


@app.post("/settings")
async def save_settings(body: dict, current_user: dict = Depends(get_current_user)):
    """Save app settings for the current tenant."""
    path = _settings_path(current_user["tenant_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(body, f, indent=2)
    return {"ok": True}


@app.post("/youtube/upload/{job_id}")
async def youtube_upload(job_id: str, privacy: str = "unlisted", current_user: dict = Depends(get_current_user)):
    """Upload a completed job's video to YouTube with AI-generated metadata."""
    tenant_id = current_user["tenant_id"]
    job = get_job(job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")

    video_path = os.path.join(OUTPUTS_DIR, job_id, "lyric_video.mp4")
    thumb_path = os.path.join(OUTPUTS_DIR, job_id, "thumbnail.jpg")

    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video file not found.")

    # Extract song name
    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    artist = job.get("artist", "")

    import asyncio
    from youtube_upload import upload_to_youtube
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, upload_to_youtube, video_path, thumb_path, artist, song, "", privacy,
    )

    # Save YouTube info to job
    update_job(job_id, youtube=result)

    return result


@app.post("/youtube/metadata/{job_id}")
async def youtube_metadata_preview(job_id: str, current_user: dict = Depends(get_current_user)):
    """Preview the AI-generated YouTube metadata without uploading."""
    tenant_id = current_user["tenant_id"]
    job = get_job(job_id, tenant_id=tenant_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    filename = job.get("filename", "")
    song = filename.replace(".mp3", "")
    if " - " in song:
        song = song.split(" - ", 1)[1]
    for sfx in ["(Official Video)", "(Official Audio)", "(En Vivo)", "(Live)", "(Lyrics)"]:
        song = song.replace(sfx, "").strip()

    from youtube_upload import generate_youtube_metadata
    import asyncio
    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(
        None, generate_youtube_metadata, job.get("artist", ""), song, "",
    )
    return metadata
