"""Persistent JSON-backed job store with multi-tenant support."""

import json
import os
import time
import uuid
from typing import Optional

_OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

# Legacy store path (for migration)
_LEGACY_STORE_PATH = os.path.join(_OUTPUTS_DIR, "_jobs.json")

# In-memory cache: tenant_id -> {job_id -> job_dict}
_tenant_jobs: dict[str, dict[str, dict]] = {}


def _store_path(tenant_id: str) -> str:
    """Return the JSON file path for a given tenant."""
    if tenant_id == "default":
        return os.path.join(_OUTPUTS_DIR, "_jobs.json")
    return os.path.join(_OUTPUTS_DIR, f"_jobs_{tenant_id}.json")


def _load_tenant(tenant_id: str) -> dict[str, dict]:
    """Load jobs for a tenant from disk."""
    if tenant_id in _tenant_jobs:
        return _tenant_jobs[tenant_id]

    path = _store_path(tenant_id)
    jobs = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                jobs = json.load(f)
        except (json.JSONDecodeError, OSError):
            jobs = {}

    _tenant_jobs[tenant_id] = jobs
    return jobs


def _save_tenant(tenant_id: str) -> None:
    """Persist jobs for a tenant to disk."""
    jobs = _tenant_jobs.get(tenant_id, {})
    try:
        os.makedirs(os.path.dirname(_store_path(tenant_id)), exist_ok=True)
        with open(_store_path(tenant_id), "w") as f:
            json.dump(jobs, f, indent=2)
    except OSError:
        pass


# Load default tenant on import (backwards compat)
_load_tenant("default")


def create_job(
    artist: str,
    style: str,
    filename: str,
    tenant_id: str = "default",
    delivery_profile: str = "youtube",
    umg_spec: Optional[dict] = None,
) -> str:
    """Create a new job and return its ID."""
    jobs = _load_tenant(tenant_id)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "job_id": job_id,
        "artist": artist,
        "style": style,
        "filename": filename,
        "tenant_id": tenant_id,
        "delivery_profile": delivery_profile,
        "umg_spec": umg_spec,
        "status": "processing",
        "current_step": "whisper",
        "progress": 0,
        "files": {
            "video_url": None,
            "short_url": None,
            "thumbnail_url": None,
            "umg_master_url": None,
        },
        "error": None,
        "created_at": time.time(),
    }
    _save_tenant(tenant_id)
    return job_id


def get_job(job_id: str, tenant_id: str = None) -> Optional[dict]:
    """Return a job dict or None if not found.

    If tenant_id is given, only search that tenant's jobs.
    Otherwise search all loaded tenants (backwards compat).
    """
    if tenant_id:
        jobs = _load_tenant(tenant_id)
        return jobs.get(job_id)

    # Search all loaded tenants
    for tid in list(_tenant_jobs.keys()):
        jobs = _tenant_jobs[tid]
        if job_id in jobs:
            return jobs[job_id]

    # Fallback: check default tenant from disk
    jobs = _load_tenant("default")
    return jobs.get(job_id)


def get_all_jobs(tenant_id: str = "default") -> list[dict]:
    """Return all jobs for a tenant, sorted by creation time (newest first)."""
    jobs = _load_tenant(tenant_id)
    return sorted(
        jobs.values(),
        key=lambda j: j.get("created_at", 0),
        reverse=True,
    )


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job."""
    # Find the job in any tenant
    for tid in list(_tenant_jobs.keys()):
        if job_id in _tenant_jobs[tid]:
            _tenant_jobs[tid][job_id].update(kwargs)
            _save_tenant(tid)
            return

    # Try default tenant from disk
    jobs = _load_tenant("default")
    if job_id in jobs:
        jobs[job_id].update(kwargs)
        _save_tenant("default")
