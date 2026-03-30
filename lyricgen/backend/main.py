"""FastAPI application for LyricGen."""

import os
import shutil
import threading

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from jobs import create_job, get_job, get_all_jobs, update_job
from pipeline import run_pipeline

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

    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, mp3_path, artist, style),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def status(job_id: str):
    """Return the current status of a job."""
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
    """Return all jobs (newest first) for the history sidebar."""
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
    """Download a generated file."""
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

    return FileResponse(
        file_path,
        filename=FILE_MAP[file_type],
        media_type="application/octet-stream",
    )


@app.get("/preview/{job_id}/{file_type}")
async def preview(job_id: str, file_type: str):
    """Serve a file with proper media type for inline preview."""
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

    return FileResponse(
        file_path,
        media_type=MEDIA_TYPES[file_type],
    )
