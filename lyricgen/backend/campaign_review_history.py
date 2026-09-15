"""Read-only evidence of saved editor changes, never inferred from updated_by."""
HUMAN_CHECKPOINTS = frozenset({"autosave", "manual", "draft", "restore", "conflict", "approve", "legacy_autosave"})


def saved_review_history(db, job_ids):
    from database import AuditLog, EditorVersion

    if not job_ids:
        return {}
    # Only metadata is loaded: no lyric snapshots or per-line audit payloads.
    audits = db.query(
        AuditLog.detail["job_id"].as_string().label("job_id"),
        AuditLog.user_id, AuditLog.created_at,
        AuditLog.detail["checkpoint"].as_string().label("checkpoint"),
        AuditLog.detail["author_kind"].as_string().label("author_kind"),
    ).filter(
        AuditLog.action.in_({"lyrics.segments_diff", "editor.review_saved"}),
        AuditLog.detail["job_id"].as_string().in_(job_ids),
        AuditLog.user_id.isnot(None),
    ).order_by(AuditLog.created_at, AuditLog.id).all()
    result = {}
    legacy_actors = set()
    for row in audits:
        if row.author_kind not in (None, "human"):
            continue
        if row.checkpoint in HUMAN_CHECKPOINTS:
            result[row.job_id] = {"user_id": row.user_id, "at": row.created_at}
        elif row.checkpoint is None:
            legacy_actors.add((row.job_id, row.user_id))
    # Older material-change audits omitted the checkpoint. Require an explicit
    # editor checkpoint by the same actor; migrations/initialization alone do
    # not establish human work. Unverifiable history stays unclassified.
    if legacy_actors:
        versions = db.query(
            EditorVersion.job_id, EditorVersion.created_by, EditorVersion.created_at,
        ).filter(
            EditorVersion.job_id.in_({job_id for job_id, _ in legacy_actors}),
            EditorVersion.reason.in_(HUMAN_CHECKPOINTS),
            EditorVersion.revision > 0,
        ).order_by(EditorVersion.created_at, EditorVersion.revision).all()
        for row in versions:
            if (row.job_id, row.created_by) not in legacy_actors:
                continue
            previous = result.get(row.job_id)
            if not previous or row.created_at > previous["at"]:
                result[row.job_id] = {"user_id": row.created_by, "at": row.created_at}
    return result
