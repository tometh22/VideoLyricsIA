"""Evidence-based states for the review -> render -> publish workflow."""
from datetime import datetime, timezone


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def latest_overwrite(job):
    dates = [timestamp(row.get('archived_at')) for row in (job.previous_versions or [])
             if isinstance(row, dict)]
    return max((value for value in dates if value), default=None)


def render_state(job, document, request):
    revision = int(document.revision if document is not None else (job.segments_revision or 0))
    params = job.render_params or {}
    finished = job.status in {'done', 'pending_review'}
    recorded_revision = params.get('_rendered_segments_revision')
    rendered_at = timestamp(params.get('_rendered_at'))
    requested_at = timestamp(request.submitted_at)
    if recorded_revision is not None:
        rendered = bool(finished and recorded_revision == revision and rendered_at
                        and (not requested_at or rendered_at >= requested_at))
    else:
        # Legacy rows have no render revision. An archived overwrite AFTER
        # the last durable edit and request is evidence; status alone isn't.
        rendered_at = latest_overwrite(job)
        saved_at = timestamp(document.updated_at) if document is not None else None
        rendered = bool(finished and rendered_at and saved_at
                        and rendered_at >= saved_at
                        and (not requested_at or rendered_at >= requested_at))
    return {
        'editor_revision': revision,
        'render_matches_editor': rendered,
        'rendered_at': rendered_at.isoformat() if rendered_at else None,
        'can_render': job.status in {'done', 'pending_review', 'rejected'},
    }
