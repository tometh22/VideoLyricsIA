"""In-memory job store for the MVP."""

import uuid
from typing import Optional

# Simple dict-based job store
_jobs: dict[str, dict] = {}


def create_job(artist: str, style: str, filename: str) -> str:
    """Create a new job and return its ID."""
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "job_id": job_id,
        "artist": artist,
        "style": style,
        "filename": filename,
        "status": "processing",
        "current_step": "whisper",
        "progress": 0,
        "files": {
            "video_url": None,
            "short_url": None,
            "thumbnail_url": None,
        },
        "error": None,
    }
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    """Return a job dict or None if not found."""
    return _jobs.get(job_id)


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job."""
    if job_id in _jobs:
        _jobs[job_id].update(kwargs)
