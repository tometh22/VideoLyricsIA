"""YouTube analytics sync — daily per-video metrics into VideoStats.

Primary source: YouTube Analytics API v2 (per-day rows; needs the
yt-analytics.readonly scope, which channel connections request since
phase 1). Fallback: Data API videos.list(part=statistics) cumulative
snapshots (works with youtube.readonly) for channels connected before
the scope existed — minutes-watched stays NULL there.

Sync runs as a nightly daemon (main.py startup, reaper pattern,
advisory lock ...103 — reaper holds ...101, the publish scheduler
...102) and on demand via POST /admin/analytics/sync-now.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from database import SessionLocal, PublishJob, VideoStats, YouTubeChannel

logger = logging.getLogger("genly.ytanalytics")

_ANALYTICS_ADVISORY_LOCK_KEY = 9118364455199103

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
BACKFILL_DAYS = int(os.environ.get("ANALYTICS_BACKFILL_DAYS", "30"))
INCREMENTAL_DAYS = int(os.environ.get("ANALYTICS_INCREMENTAL_DAYS", "3"))
RETENTION_DAYS = int(os.environ.get("ANALYTICS_RETENTION_DAYS", "400"))
SYNC_HOUR_UTC = int(os.environ.get("ANALYTICS_SYNC_HOUR_UTC", "3"))
SYNC_ENABLED = os.environ.get("ANALYTICS_SYNC_ENABLED", "1").lower() not in ("0", "false", "no")


# ─── Google fetchers (the seams tests monkeypatch) ───────────────────

def fetch_analytics_rows(channel, video_ids: list, start: str, end: str) -> list:
    """YouTube Analytics v2 per-video, per-day rows.
    Returns [{video_id, stat_date, views, likes, comments,
    estimated_minutes_watched}]."""
    from googleapiclient.discovery import build
    from youtube_upload import _credentials_from_channel

    creds = _credentials_from_channel(channel)
    yta = build("youtubeAnalytics", "v2", credentials=creds)

    rows = []
    for i in range(0, len(video_ids), 200):
        chunk = video_ids[i:i + 200]
        resp = yta.reports().query(
            ids="channel==MINE",
            startDate=start,
            endDate=end,
            metrics="views,likes,comments,estimatedMinutesWatched",
            dimensions="video,day",
            filters=f"video=={','.join(chunk)}",
        ).execute()
        for r in resp.get("rows") or []:
            rows.append({
                "video_id": r[0],
                "stat_date": r[1],
                "views": int(r[2]),
                "likes": int(r[3]),
                "comments": int(r[4]),
                "estimated_minutes_watched": int(r[5]),
            })
    return rows


def fetch_data_api_stats(channel, video_ids: list) -> list:
    """Data API cumulative snapshot fallback: one row per video for today."""
    from youtube_upload import _get_youtube_client
    import youtube_quota

    yt = _get_youtube_client(channel)
    today = datetime.now(timezone.utc).date().isoformat()
    rows = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        youtube_quota.record_usage("videos.list")
        resp = yt.videos().list(part="statistics", id=",".join(chunk)).execute()
        for item in resp.get("items") or []:
            stats = item.get("statistics", {})
            rows.append({
                "video_id": item["id"],
                "stat_date": today,
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "estimated_minutes_watched": None,
            })
    return rows


# ─── Sync ────────────────────────────────────────────────────────────

def _upsert_rows(s, tenant_id: str, rows: list, source: str, publish_job_by_video: dict) -> int:
    """Insert-or-update daily rows. Never overwrite a non-zero row with
    zeros — the Analytics API returns empty/zero rows for the most recent
    24–72 h before the data settles."""
    written = 0
    for r in rows:
        existing = (
            s.query(VideoStats)
            .filter(VideoStats.video_id == r["video_id"], VideoStats.stat_date == r["stat_date"])
            .first()
        )
        if existing:
            if r["views"] == 0 and existing.views > 0:
                continue
            existing.views = r["views"]
            existing.likes = r["likes"]
            existing.comments = r["comments"]
            if r.get("estimated_minutes_watched") is not None:
                existing.estimated_minutes_watched = r["estimated_minutes_watched"]
            existing.source = source
            existing.fetched_at = datetime.now(timezone.utc)
        else:
            s.add(VideoStats(
                video_id=r["video_id"],
                publish_job_id=publish_job_by_video.get(r["video_id"]),
                tenant_id=tenant_id,
                stat_date=r["stat_date"],
                views=r["views"],
                likes=r["likes"],
                comments=r["comments"],
                estimated_minutes_watched=r.get("estimated_minutes_watched"),
                source=source,
            ))
        written += 1
    return written


def sync_tenant(s, tenant_id: str) -> dict:
    """Sync every published video of a tenant. Uses the tenant's default
    active channel credential (analytics scope → Analytics API, else
    Data API fallback)."""
    published = (
        s.query(PublishJob)
        .filter(PublishJob.tenant_id == tenant_id, PublishJob.status == "published",
                PublishJob.video_id.isnot(None))
        .all()
    )
    if not published:
        return {"tenant_id": tenant_id, "videos": 0, "rows": 0}

    video_ids = sorted({p.video_id for p in published})
    publish_job_by_video = {p.video_id: p.id for p in published}

    channel = (
        s.query(YouTubeChannel)
        .filter(YouTubeChannel.tenant_id == tenant_id, YouTubeChannel.status == "active")
        .order_by(YouTubeChannel.is_default.desc())
        .first()
    )
    if channel is None:
        return {"tenant_id": tenant_id, "videos": len(video_ids), "rows": 0, "skipped": "no_active_channel"}

    has_history = (
        s.query(VideoStats.id)
        .filter(VideoStats.tenant_id == tenant_id)
        .first()
        is not None
    )
    days = INCREMENTAL_DAYS if has_history else BACKFILL_DAYS
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    has_analytics_scope = ANALYTICS_SCOPE in (channel.scopes or [])
    try:
        if has_analytics_scope:
            rows = fetch_analytics_rows(channel, video_ids, start.isoformat(), end.isoformat())
            source = "analytics"
        else:
            rows = fetch_data_api_stats(channel, video_ids)
            source = "data_api"
    except Exception as e:
        logger.warning("analytics sync failed for tenant %s: %s", tenant_id, e)
        return {"tenant_id": tenant_id, "videos": len(video_ids), "rows": 0, "error": type(e).__name__}

    written = _upsert_rows(s, tenant_id, rows, source, publish_job_by_video)
    s.commit()
    return {"tenant_id": tenant_id, "videos": len(video_ids), "rows": written, "source": source}


def sync_all() -> dict:
    """One full pass over every tenant with published videos. Owns its
    session; advisory-locked so one replica runs it."""
    s = SessionLocal()
    try:
        if s.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            got = s.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _ANALYTICS_ADVISORY_LOCK_KEY},
            ).scalar()
            if not got:
                return {"skipped": "lock_held"}

        tenants = [
            t for (t,) in (
                s.query(PublishJob.tenant_id)
                .filter(PublishJob.status == "published")
                .distinct()
                .all()
            )
        ]
        results = [sync_tenant(s, t) for t in tenants]

        # Retention prune.
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)).isoformat()
        pruned = (
            s.query(VideoStats)
            .filter(VideoStats.stat_date < cutoff)
            .delete(synchronize_session=False)
        )
        s.commit()
        return {"tenants": len(tenants), "results": results, "pruned": pruned}
    finally:
        try:
            if s.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                s.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ANALYTICS_ADVISORY_LOCK_KEY})
                s.commit()
        except Exception:  # pragma: no cover
            pass
        s.close()


def last_synced_at():
    s = SessionLocal()
    try:
        return s.query(VideoStats.fetched_at).order_by(VideoStats.fetched_at.desc()).limit(1).scalar()
    finally:
        s.close()


# ─── Query helpers for the API ───────────────────────────────────────

def video_series(s, tenant_id: str, video_id: str, days: int = 28) -> dict:
    """Totals + daily series for one video, hiding the SUM-vs-MAX split."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = (
        s.query(VideoStats)
        .filter(
            VideoStats.tenant_id == tenant_id,
            VideoStats.video_id == video_id,
            VideoStats.stat_date >= cutoff,
        )
        .order_by(VideoStats.stat_date.asc())
        .all()
    )
    if not rows:
        return {"video_id": video_id, "source": None, "last_synced_at": None,
                "totals": None, "series": []}

    source = rows[-1].source
    if source == "analytics":
        totals = {
            "views": sum(r.views for r in rows),
            "likes": sum(r.likes for r in rows),
            "comments": sum(r.comments for r in rows),
            "estimated_minutes_watched": sum(r.estimated_minutes_watched or 0 for r in rows),
        }
    else:
        last = rows[-1]
        totals = {
            "views": last.views,
            "likes": last.likes,
            "comments": last.comments,
            "estimated_minutes_watched": None,
        }
    return {
        "video_id": video_id,
        "source": source,
        "last_synced_at": max(
            (r.fetched_at for r in rows if r.fetched_at), default=None,
        ).isoformat() if any(r.fetched_at for r in rows) else None,
        "totals": totals,
        "series": [
            {
                "date": r.stat_date,
                "views": r.views,
                "likes": r.likes,
                "comments": r.comments,
                "estimated_minutes_watched": r.estimated_minutes_watched,
            }
            for r in rows
        ],
    }


def tenant_summary(s, tenant_id: str, days: int = 30) -> dict:
    """Tenant-level totals + top videos for the dashboard card."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = (
        s.query(VideoStats)
        .filter(VideoStats.tenant_id == tenant_id, VideoStats.stat_date >= cutoff)
        .all()
    )
    per_video = {}
    for r in rows:
        agg = per_video.setdefault(r.video_id, {"views": 0, "likes": 0, "comments": 0, "source": r.source})
        if r.source == "analytics":
            agg["views"] += r.views
            agg["likes"] += r.likes
            agg["comments"] += r.comments
            agg["source"] = "analytics"
        else:
            agg["views"] = max(agg["views"], r.views)
            agg["likes"] = max(agg["likes"], r.likes)
            agg["comments"] = max(agg["comments"], r.comments)
    top = sorted(per_video.items(), key=lambda kv: kv[1]["views"], reverse=True)[:10]
    return {
        "days": days,
        "videos_tracked": len(per_video),
        "totals": {
            "views": sum(v["views"] for v in per_video.values()),
            "likes": sum(v["likes"] for v in per_video.values()),
            "comments": sum(v["comments"] for v in per_video.values()),
        },
        "top_videos": [{"video_id": vid, **agg} for vid, agg in top],
    }
