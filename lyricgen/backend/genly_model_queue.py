"""Small staging-only Redis lease queue for a local authenticated model worker."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid

PREFIX = "staging:genly-local-model:v1:"
RECORD_TTL = 86400
WORKER_TTL = 90
LEASE_SECONDS = 300
_client = None


def client():
    global _client
    if _client is None:
        from redis import Redis
        url = os.getenv("REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("model_queue_unavailable")
        _client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
    return _client


def _valid_id(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("invalid_" + name)
    return value


def _run_key(run_id):
    return PREFIX + "run:" + _valid_id(run_id, "run_id")


def public(row):
    if not row:
        return None
    result = {key: row.get(key) for key in ("run_id", "job_id", "source", "status", "stage",
              "created_at", "updated_at", "lease_expires_at", "failure_reason")}
    if result["status"] == "processing" and float(result.get("lease_expires_at") or 0) < time.time():
        result["status"] = "worker_unavailable"
    return result


def worker_available():
    return bool(client().exists(PREFIX + "worker_alive"))


def enqueue(job_id, source, user_id):
    from redis.exceptions import WatchError
    connection = client()
    pointer = PREFIX + "job:" + _valid_id(job_id, "job_id")
    for _ in range(4):
        with connection.pipeline() as pipe:
            try:
                pipe.watch(pointer)
                existing_id = pipe.get(pointer)
                raw = pipe.get(_run_key(existing_id)) if existing_id else None
                existing = json.loads(raw) if raw else None
                if (existing and existing.get("source") == source and
                        (existing["status"] == "queued" or (existing["status"] == "processing"
                         and existing.get("lease_expires_at", 0) > time.time()))):
                    return public(existing)
                now, run_id = time.time(), str(uuid.uuid4())
                row = {"run_id": run_id, "job_id": job_id, "source": source, "requested_by": user_id,
                       "status": "queued", "stage": "queued", "created_at": now, "updated_at": now}
                pipe.multi()
                pipe.set(_run_key(run_id), json.dumps(row), ex=RECORD_TTL)
                pipe.set(pointer, run_id, ex=RECORD_TTL)
                pipe.zadd(PREFIX + "pending", {run_id: now})
                pipe.execute()
                return public(row)
            except WatchError:
                continue
    raise RuntimeError("model_queue_busy")


def status(job_id):
    connection = client()
    run_id = connection.get(PREFIX + "job:" + _valid_id(job_id, "job_id"))
    raw = connection.get(_run_key(run_id)) if run_id else None
    return {"request": public(json.loads(raw)) if raw else None, "worker_available": worker_available()}


def heartbeat(worker_id, *, run_id=None, lease_token=None, stage=None):
    from redis.exceptions import WatchError
    _valid_id(worker_id, "worker_id")
    connection = client()
    connection.set(PREFIX + "worker_alive", worker_id, ex=WORKER_TTL)
    if run_id is None:
        return {"worker_available": True}
    key = _run_key(run_id)
    for _ in range(4):
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                row = json.loads(raw) if raw else None
                _assert_lease(row, lease_token)
                if row.get("worker_id") != worker_id:
                    raise ValueError("model_lease_lost")
                row.update(updated_at=time.time(), lease_expires_at=time.time() + LEASE_SECONDS)
                if stage is not None:
                    row["stage"] = stage if stage in {"downloading", "recognizing", "aligning", "publishing", "processing"} else "processing"
                pipe.multi()
                pipe.set(key, json.dumps(row), ex=RECORD_TTL)
                pipe.execute()
                return {"worker_available": True, "lease_expires_at": row["lease_expires_at"]}
            except WatchError:
                continue
    raise RuntimeError("model_queue_busy")


def claim(worker_id):
    from redis.exceptions import WatchError
    heartbeat(worker_id)
    connection = client()
    for run_id in connection.zrange(PREFIX + "pending", 0, 63):
        key = _run_key(run_id)
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                row = json.loads(raw) if raw else None
                if not row or row["status"] not in {"queued", "processing"}:
                    connection.zrem(PREFIX + "pending", run_id)
                    continue
                if row["status"] == "processing" and row.get("lease_expires_at", 0) > time.time():
                    continue
                row.update(status="processing", stage="processing", worker_id=worker_id,
                           lease_token=secrets.token_urlsafe(32), updated_at=time.time(),
                           lease_expires_at=time.time() + LEASE_SECONDS)
                pipe.multi()
                pipe.set(key, json.dumps(row), ex=RECORD_TTL)
                pipe.execute()
                return {"worker_available": True, "request": {**public(row), "lease_token": row["lease_token"]}}
            except WatchError:
                continue
    return {"worker_available": True, "request": None}


def _assert_lease(row, token):
    if (not row or row.get("status") != "processing" or not isinstance(token, str)
            or not secrets.compare_digest(row.get("lease_token", ""), token)
            or row.get("lease_expires_at", 0) < time.time()):
        raise ValueError("model_lease_lost")


def verify_claim(run_id, lease_token, *, job_id=None, source=None):
    raw = client().get(_run_key(run_id))
    row = json.loads(raw) if raw else None
    _assert_lease(row, lease_token)
    if (job_id is not None and row["job_id"] != job_id) or (source is not None and row["source"] != source):
        raise ValueError("model_request_source_mismatch")
    return row


def finish(run_id, lease_token, *, status, reason=None):
    from redis.exceptions import WatchError
    if status not in {"complete", "partial", "failed", "stale"}:
        raise ValueError("invalid_model_completion_status")
    connection = client()
    key = _run_key(run_id)
    for _ in range(4):
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                row = json.loads(raw) if raw else None
                _assert_lease(row, lease_token)
                row.update(status=status, stage=status, updated_at=time.time())
                row.pop("lease_token", None)
                row["failure_reason"] = (reason if reason in {"source_changed", "audio_unavailable", "model_failed", "worker_stopped"}
                                         else "model_failed") if reason else None
                pipe.multi()
                pipe.set(key, json.dumps(row), ex=RECORD_TTL)
                pipe.zrem(PREFIX + "pending", run_id)
                pipe.execute()
                return public(row)
            except WatchError:
                continue
    raise RuntimeError("model_queue_busy")
