"""Persistent JSON-backed job store."""

import json
import os
import time
import uuid
from typing import Optional

_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "outputs", "_jobs.json")
_jobs: dict[str, dict] = {}


def _load():
    """Load jobs from disk on startup."""
    global _jobs
    if os.path.exists(_STORE_PATH):
        try:
            with open(_STORE_PATH) as f:
                _jobs = json.load(f)
        except (json.JSONDecodeError, OSError):
            _jobs = {}


def _save():
    """Persist jobs to disk."""
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        with open(_STORE_PATH, "w") as f:
            json.dump(_jobs, f, indent=2)
    except OSError:
        pass


# Load on import
_load()


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
        "created_at": time.time(),
    }
    _save()
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    """Return a job dict or None if not found."""
    return _jobs.get(job_id)


def get_all_jobs() -> list[dict]:
    """Return all jobs sorted by creation time (newest first)."""
    return sorted(
        _jobs.values(),
        key=lambda j: j.get("created_at", 0),
        reverse=True,
    )


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job."""
    if job_id in _jobs:
        _jobs[job_id].update(kwargs)
        _save()
