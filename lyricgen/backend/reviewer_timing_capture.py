"""Prospective operational timing evidence. Not blind/perceptual gold."""
import hashlib
import math
import os


def timing_capture(previous, current, *, job, user_id, checkpoint, from_revision, to_revision):
    if os.getenv('REVIEWER_TIMING_CAPTURE_ENABLED', '0') != '1' or checkpoint not in {'draft', 'autosave', 'manual', 'approve'}:
        return None
    from reviewer_assist_scope import campaign_in_scope
    if not campaign_in_scope(getattr(job, 'campaign_id', None)):
        return None
    if user_id is None or len(previous) != len(current):
        return None  # structural edits need explicit correspondence
    changed = []
    for i, (old, new) in enumerate(zip(previous, current)):
        if old.get('_id') != new.get('_id'):
            continue
        values = [old.get('start'), old.get('end'), new.get('start'), new.get('end')]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            continue
        if old['start'] == new['start'] and old['end'] == new['end']:
            continue
        changed.append({'line_index': i, 'line_id': new.get('_id'),
            'association': 'stable_id' if new.get('_id') is not None else 'position_only_unverified',
            'baseline': {k: old[k] for k in ['start', 'end']},
            'human_submitted': {k: new[k] for k in ['start', 'end']},
            'start_delta': new['start']-old['start'], 'end_delta': new['end']-old['end'],
            'baseline_text_sha256': hashlib.sha256(str(old.get('text','')).encode()).hexdigest(),
            'submitted_text_sha256': hashlib.sha256(str(new.get('text','')).encode()).hexdigest()})
    if not changed:
        return None
    return {'schema': 'prospective-editor-timing-v1', 'job_id': job.job_id,
        'tenant_id': job.tenant_id, 'actor_user_id': user_id,
        'audio_sha256': getattr(job, 'input_audio_sha256', None),
        'audio_revision': getattr(job, 'audio_revision', None),
        'from_revision': from_revision, 'to_revision': to_revision, 'checkpoint': checkpoint,
        'server_commit': os.getenv('RAILWAY_GIT_COMMIT_SHA', 'unknown'),
        'capture_epoch': os.getenv('REVIEWER_TIMING_CAPTURE_EPOCH', 'unverified'),
        'changed': changed, 'authorship': 'authenticated_editor_request_not_per_line_intent',
        'old_client_auto_trim_excluded': False, 'blind_annotation': False,
        'exact_perceptual_interval': False, 'clean_gold': False}
