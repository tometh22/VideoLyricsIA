"""Background YouTube publish task — runs on RQ workers (queue "publish").

Deliberately does NOT import main.py (workers must not pull FastAPI /
moviepy / the whole API surface). Everything it needs lives in
youtube_upload, storage, database, jobs and emails.

Lifecycle of a PublishJob row:

    queued ──claim──> uploading ──> published
       │                  │
       │                  └──> failed   (claim released; user re-publishes)
       └──> canceled (only before claim)

The claim is a conditional UPDATE (status queued/scheduled → uploading),
so N workers racing on the same row resolve to exactly one uploader. The
partial unique index uq_publish_active guarantees no second active row
exists per (job_id, kind).
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone

from database import SessionLocal, AuditLog, Job, PublishJob, User, YouTubeChannel
from jobs import update_job

_S3_KEY_BY_KIND = {"video": "video", "short": "short"}
_LOCAL_FILE_BY_KIND = {"video": "lyric_video.mp4", "short": "short.mp4"}
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")


def _now():
    return datetime.now(timezone.utc)


def _update_publish_job(publish_job_id: int, **kwargs) -> None:
    """Persist PublishJob fields on a fresh short-lived session (the task
    holds no long-lived session across the multi-minute upload)."""
    s = SessionLocal()
    try:
        s.query(PublishJob).filter(PublishJob.id == publish_job_id).update(
            kwargs, synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()


def _audit(user_id, action: str, detail: dict) -> None:
    s = SessionLocal()
    try:
        s.add(AuditLog(user_id=user_id, action=action, detail=detail))
        s.commit()
    finally:
        s.close()


def _resolve_source_files(job_row, kind: str, tmp_dir: str):
    """Return (video_path, thumb_path). Prefers the local outputs dir;
    falls back to downloading from R2 (the pipeline deletes local files
    after uploading deliverables — the API container may not have them)."""
    import storage

    job_dir = os.path.join(OUTPUTS_DIR, job_row.job_id)
    filename = _LOCAL_FILE_BY_KIND[kind]

    video_path = os.path.join(job_dir, filename)
    if not os.path.exists(video_path):
        s3_keys = job_row.s3_keys or {}
        key = s3_keys.get(_S3_KEY_BY_KIND[kind])
        if not key:
            raise FileNotFoundError(
                f"No local {filename} and no R2 key for job {job_row.job_id}."
            )
        video_path = os.path.join(tmp_dir, filename)
        if not storage.download_object(key, video_path):
            raise RuntimeError(f"R2 download failed for {key}.")

    thumb_path = os.path.join(job_dir, "thumbnail.jpg")
    if not os.path.exists(thumb_path):
        thumb_key = (job_row.s3_keys or {}).get("thumbnail")
        if thumb_key:
            candidate = os.path.join(tmp_dir, "thumbnail.jpg")
            if storage.download_object(thumb_key, candidate):
                thumb_path = candidate

    return video_path, thumb_path


def _sanitize_error(e: Exception) -> str:
    """Exception class + YouTube reason only — raw messages can carry
    token paths / API URLs and this string reaches the browser."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(e, HttpError):
            reason = getattr(e, "reason", None) or f"HTTP {getattr(getattr(e, 'resp', None), 'status', '?')}"
            return f"YouTube API error: {reason}"
    except ImportError:  # pragma: no cover
        pass
    from youtube_upload import YouTubeNotConfiguredError
    if isinstance(e, YouTubeNotConfiguredError):
        return str(e)
    return f"{type(e).__name__}"


def _notify(publish_job, job_row, result: dict | None, error: str | None) -> None:
    """Email the requester. Best-effort — a mail failure never fails the publish."""
    try:
        import emails

        s = SessionLocal()
        try:
            user = s.query(User).filter(User.id == publish_job.created_by).first()
        finally:
            s.close()
        if not user or not user.email:
            return
        song = job_row.song_title or job_row.filename
        if error is None:
            emails.send_video_published(
                user.email, user.username, job_row.artist, song,
                result.get("url", ""), publish_job.privacy, publish_job.kind,
            )
        else:
            emails.send_video_publish_failed(
                user.email, user.username, job_row.artist, song, error,
            )
    except Exception as e:  # pragma: no cover
        print(f"[PUBLISH] notification email failed: {e}")


def publish_to_youtube_task(publish_job_id: int) -> dict:
    """RQ task: upload one PublishJob's asset to YouTube."""
    from youtube_upload import (
        upload_to_youtube, resolve_channel, job_song_title, settings_for_job,
    )

    # ── Claim (race-free across workers) ─────────────────────────────
    s = SessionLocal()
    try:
        claimed = (
            s.query(PublishJob)
            .filter(
                PublishJob.id == publish_job_id,
                PublishJob.status.in_(("queued", "scheduled")),
            )
            .update(
                {
                    PublishJob.status: "uploading",
                    PublishJob.started_at: _now(),
                    PublishJob.attempts: PublishJob.attempts + 1,
                },
                synchronize_session=False,
            )
        )
        s.commit()
        if not claimed:
            return {"skipped": True, "reason": "not claimable (canceled/duplicate?)"}

        publish_job = s.query(PublishJob).filter(PublishJob.id == publish_job_id).one()
        job_row = s.query(Job).filter(Job.job_id == publish_job.job_id).one()
        job_dict = job_row.to_dict()

        # Idempotency pre-check: a crash-replayed enqueue after the video
        # already published must not upload a duplicate.
        if publish_job.kind == "video" and (job_row.youtube_data or {}).get("video_id"):
            existing = job_row.youtube_data
            publish_job.status = "published"
            publish_job.video_id = existing.get("video_id")
            publish_job.video_url = existing.get("url")
            publish_job.progress = 100
            publish_job.completed_at = _now()
            s.commit()
            return {"video_id": existing.get("video_id"), "deduped": True}

        try:
            channel = resolve_channel(s, publish_job.tenant_id, publish_job.channel_id)
        except Exception as e:
            _fail(publish_job_id, _sanitize_error(e))
            _notify(publish_job, job_row, None, _sanitize_error(e))
            return {"error": _sanitize_error(e)}

        settings = settings_for_job(s, publish_job.job_id, publish_job.created_by)
        metadata = publish_job.metadata_json
        privacy = publish_job.privacy
        kind = publish_job.kind
    finally:
        s.close()

    # ── Metadata for the Short ────────────────────────────────────────
    if kind == "short" and metadata and metadata.get("title") and "#shorts" not in metadata["title"].lower():
        # Deterministic Shorts classification (the <60s vertical format
        # already qualifies; the tag removes ambiguity).
        metadata = {**metadata, "title": f"{metadata['title']} #Shorts"}

    song = job_song_title(job_dict)
    artist = job_dict.get("artist", "")

    def _on_progress(percent: int, _last=[-10]):
        # Throttle: persist every >=5-point change only.
        if percent - _last[0] >= 5 or percent >= 100:
            _last[0] = percent
            _update_publish_job(publish_job_id, progress=percent)

    tmp_dir = tempfile.mkdtemp(prefix=f"ytpub_{publish_job.job_id}_")
    try:
        video_path, thumb_path = _resolve_source_files(job_row, kind, tmp_dir)

        result = upload_to_youtube(
            video_path, thumb_path, artist, song, "",
            privacy, publish_job.job_id,
            metadata=metadata, settings=settings, channel=channel,
            progress_callback=_on_progress,
        )
    except Exception as e:
        error = _sanitize_error(e)
        print(f"[PUBLISH] job {publish_job.job_id} ({kind}) failed: {e}")
        _fail(publish_job_id, error)
        _notify(publish_job, job_row, None, error)
        return {"error": error}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Success ───────────────────────────────────────────────────────
    _update_publish_job(
        publish_job_id,
        status="published",
        progress=100,
        video_id=result.get("video_id"),
        video_url=result.get("url"),
        completed_at=_now(),
        error=None,
    )
    if kind == "video":
        # Mirror for the legacy UI / status payloads / sync endpoint.
        update_job(publish_job.job_id, youtube=result)

    _audit(publish_job.created_by, "job.youtube_publish", {
        "job_id": publish_job.job_id,
        "publish_job_id": publish_job_id,
        "kind": kind,
        "video_id": result.get("video_id"),
        "url": result.get("url"),
        "privacy": privacy,
        "title": result.get("title"),
    })
    _notify(publish_job, job_row, result, None)
    return result


def _fail(publish_job_id: int, error: str) -> None:
    _update_publish_job(
        publish_job_id,
        status="failed",
        error=error,
        completed_at=_now(),
    )
