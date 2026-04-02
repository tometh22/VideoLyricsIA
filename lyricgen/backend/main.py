"""FastAPI application for LyricGen."""

import json
import os
import shutil
import threading

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jobs import create_job, get_job, get_all_jobs, update_job
from pipeline import run_pipeline, transcribe

OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

app = FastAPI(title="LyricGen API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    artist: str = Form(...),
    style: str = Form("oscuro"),
    language: str = Form(""),
):
    """Receive an MP3 and start processing."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    job_id = create_job(artist=artist, style=style, filename=file.filename)
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    lang = language.strip() if language.strip() else None

    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, mp3_path, artist, style),
        kwargs={"language": lang},
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.post("/transcribe")
async def transcribe_endpoint(
    file: UploadFile = File(...),
    language: str = Form(""),
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
):
    """Generate video using user-edited segments (skips Whisper)."""
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are accepted.")

    segments = json.loads(segments_json)

    job_id = create_job(artist=artist, style=style, filename=file.filename)
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    mp3_path = os.path.join(job_dir, file.filename)
    with open(mp3_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, mp3_path, artist, style),
        kwargs={"segments_override": segments},
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = get_job(job_id)
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
async def list_jobs():
    jobs = get_all_jobs()
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
}

MEDIA_TYPES = {
    "video": "video/mp4",
    "short": "video/mp4",
    "thumbnail": "image/jpeg",
}


@app.get("/download/{job_id}/{file_type}")
async def download(job_id: str, file_type: str):
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")
    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, filename=FILE_MAP[file_type], media_type="application/octet-stream")


@app.get("/preview/{job_id}/{file_type}")
async def preview(job_id: str, file_type: str):
    if file_type not in FILE_MAP:
        raise HTTPException(status_code=400, detail="Invalid file type.")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Job is not done yet.")
    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP[file_type])
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, media_type=MEDIA_TYPES[file_type])
