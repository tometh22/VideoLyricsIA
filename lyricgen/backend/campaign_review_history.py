"""Saved review requires a material change witnessed by active editor telemetry."""
import os
from bisect import bisect_left
from datetime import timedelta, timezone

# The batch/shared account cannot establish personal review attribution, even
# when its scripts use "manual" or "restore". Do not guess from name prefixes.
KNOWN_AUTOMATION_ACCOUNTS = frozenset({"batch-universal-staging"})
HUMAN_CHECKPOINTS = frozenset({
    "autosave", "manual", "draft", "restore", "conflict", "approve",
    "legacy_autosave", "change_request",
})
ACTIVITY_WINDOW = timedelta(seconds=45)


def automation_accounts():
    return KNOWN_AUTOMATION_ACCOUNTS | {
        name.strip().lower()
        for name in os.environ.get("REVIEW_AUTOMATION_USERNAMES", "").split(",")
        if name.strip()
    }


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def saved_review_history(db, job_ids):
    from database import AuditLog, EditorVersion, ProductEvent, User
    from sqlalchemy import func

    if not job_ids:
        return {}
    # These events are server-timestamped and require the actor's active editor
    # lock at ingestion. Revision and time bind activity to this particular save;
    # a login, document owner, or an earlier visit does not prove an edit.
    activity = {}
    beats = db.query(
        ProductEvent.job_id, ProductEvent.user_id, ProductEvent.created_at,
        ProductEvent.properties["revision"].as_integer().label("revision"),
    ).filter(
        ProductEvent.name == "editor_activity_heartbeat",
        ProductEvent.job_id.in_(job_ids),
    ).order_by(ProductEvent.created_at).all()
    for beat in beats:
        if beat.created_at and beat.revision is not None:
            activity.setdefault((beat.job_id, beat.user_id), []).append((_utc(beat.created_at), beat.revision))
    times = {key: [at for at, _ in values] for key, values in activity.items()}

    def witnessed(job_id, actor, when, revisions):
        when = _utc(when)
        key = (job_id, actor)
        if not when or key not in times or not revisions:
            return False
        index = bisect_left(times[key], when - ACTIVITY_WINDOW)
        for position in range(index, len(activity[key])):
            at, revision = activity[key][position]
            if at > when + ACTIVITY_WINDOW:
                break
            if revision in revisions:
                return True
        return False

    # Metadata only: no lyric snapshots or per-line audit payloads are loaded.
    audits = db.query(
        AuditLog.detail["job_id"].as_string().label("job_id"),
        AuditLog.user_id, AuditLog.created_at,
        AuditLog.detail["checkpoint"].as_string().label("checkpoint"),
        AuditLog.detail["author_kind"].as_string().label("author_kind"),
        AuditLog.detail["from_revision"].as_integer().label("from_revision"),
        AuditLog.detail["to_revision"].as_integer().label("to_revision"),
    ).join(User, User.id == AuditLog.user_id).filter(
        func.lower(User.username).notin_(automation_accounts()),
        AuditLog.action.in_({"lyrics.segments_diff", "editor.review_saved"}),
        AuditLog.detail["job_id"].as_string().in_(job_ids),
    ).order_by(AuditLog.created_at, AuditLog.id).all()
    result = {}
    legacy = {}
    for row in audits:
        if row.author_kind not in (None, "human"):
            continue
        if row.checkpoint in HUMAN_CHECKPOINTS:
            revisions = {r for r in (row.from_revision, row.to_revision) if r is not None}
            if witnessed(row.job_id, row.user_id, row.created_at, revisions):
                result[row.job_id] = {"user_id": row.user_id, "at": row.created_at}
            elif not revisions and row.created_at:
                legacy.setdefault((row.job_id, row.user_id), []).append(_utc(row.created_at))
        elif row.checkpoint is None and row.created_at:
            legacy.setdefault((row.job_id, row.user_id), []).append(_utc(row.created_at))
    # Older bounded diffs omit revision/checkpoint. Require a nearby material
    # diff AND an explicit saved version with nearby activity on that revision.
    # Missing telemetry/history stays unclassified, never silently backfilled.
    if legacy:
        versions = db.query(
            EditorVersion.job_id, EditorVersion.created_by, EditorVersion.created_at, EditorVersion.revision,
        ).join(User, User.id == EditorVersion.created_by).filter(
            func.lower(User.username).notin_(automation_accounts()),
            EditorVersion.job_id.in_({job_id for job_id, _ in legacy}),
            EditorVersion.reason.in_(HUMAN_CHECKPOINTS),
            EditorVersion.revision > 0,
        ).order_by(EditorVersion.created_at, EditorVersion.revision).all()
        for row in versions:
            at = _utc(row.created_at)
            dates = legacy.get((row.job_id, row.created_by), [])
            index = bisect_left(dates, at - ACTIVITY_WINDOW) if at else len(dates)
            if index == len(dates) or dates[index] > at + ACTIVITY_WINDOW:
                continue
            if not witnessed(row.job_id, row.created_by, at, {row.revision, row.revision - 1}):
                continue
            previous = result.get(row.job_id)
            if not previous or at > _utc(previous["at"]):
                result[row.job_id] = {"user_id": row.created_by, "at": row.created_at}
    return result
