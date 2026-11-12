"""Connect acoustic evidence to the exact encoded asset and revision."""
import tempfile
from pathlib import Path
from full_audio_review import finding
from transcription_quality import segments_hash


def source_binding(job, segments):
    return {'revision': int(job.segments_revision or 0), 'segments_hash': segments_hash(segments),
            'audio_revision': int(job.audio_revision or 0), 'audio_sha256': str(job.input_audio_sha256 or '')}


def check_rendered_audio(video_path, job):
    """Conservative decoded waveform comparison. Uncertainty needs a human.

    Match the whole mix, not only duration or an energy envelope. Lossy audio
    may fail this deliberately strict comparison; that is REVIEW, not proof
    the wrong recording was used.
    """
    import numpy as np
    import storage
    from reviewer_shadow_audio import pcm, file_sha
    try:
        with tempfile.TemporaryDirectory(prefix='render-audio-qc-') as temp:
            master = str(Path(temp) / 'master.audio')
            if not job.input_r2_key or not storage.download_object(job.input_r2_key, master):
                return {'status': 'NOT_RUN', 'reason': 'master_unavailable'}
            if file_sha(master) != job.input_audio_sha256:
                return {'status': 'REVIEW', 'reason': 'master_identity_changed'}
            original, rendered = pcm(master, rate=16000), pcm(video_path, rate=16000)
            if abs(len(original) - len(rendered)) > 1600 or len(original) < 1600:
                return {'status': 'REVIEW', 'reason': 'decoded_duration_differs'}
            n = min(len(original), len(rendered))
            x, y = original[:n], rendered[:n]
            if np.dot(x, x) < 1e-8 or np.dot(y, y) < 1e-8:
                return {'status': 'REVIEW', 'reason': 'silent_or_unusable_audio'}
            gain = float(np.dot(x, y) / np.dot(x, x))
            residual = float(np.dot(y - gain*x, y - gain*x) / np.dot(y, y))
            return {'status': 'PASS' if gain > 0 and residual <= .02 else 'REVIEW',
                    'method': 'whole_mix_normalized_residual_v1', 'residual_ratio': residual,
                    'master_sha256': job.input_audio_sha256}
    except Exception as exc:
        return {'status': 'NOT_RUN', 'reason': type(exc).__name__}


def build_acoustic_checks(job, segments, video_path):
    from quality_rollout import review_required
    if not review_required(job):
        return {}, []
    review = getattr(job, 'acoustic_review', None) or {}
    expected = source_binding(job, segments)
    issues = []
    if review.get('binding') != expected or review.get('status') != 'COMPLETE':
        issues.append(finding('ACOUSTIC_NOT_RUN', 0, 0, 'La escucha completa falta o corresponde a otra revisión'))
    else:
        issues.extend(review.get('issues') or [])
        if set(review.get('coverage', {})) != {'openai', 'google'}:
            issues.append(finding('ACOUSTIC_NOT_RUN', 0, 0, 'Falta evidencia de cobertura de ambas escuchas'))
    audio = check_rendered_audio(video_path, job)
    if audio['status'] != 'PASS':
        issues.append(finding('RENDER_AUDIO_UNVERIFIED', 0, 0, 'Verificar que el audio del video corresponde al master'))
    return {'required': True, 'binding': expected, 'coverage': review.get('coverage', {}),
            'render_audio': audio, 'render_edit_count': int(getattr(job, 'edit_count', 0) or 0), 'metadata': {'artist': job.artist, 'song_title': job.song_title}}, issues


def require_current_evidence(job):
    """Validate decisions against the source and the currently stored MP4."""
    from quality_rollout import review_required
    if not review_required(job):
        return
    report = job.delivery_qc or {}
    acoustic = report.get('acoustic') or {}
    if (not acoustic.get('required') or acoustic.get('binding') != source_binding(job, job.segments_json or [])
            or acoustic.get('metadata') != {'artist': job.artist, 'song_title': job.song_title}):
        raise ValueError('fresh_delivery_acoustic_review_required')
    if acoustic.get('render_edit_count') != int(job.edit_count or 0):
        raise ValueError('delivery_render_changed')
    asset = report.get('render_asset') or {}
    import storage
    key = (getattr(job, 's3_keys', None) or {}).get('video')
    if (not key or asset.get('key') != key or not asset.get('etag')
            or asset.get('sha256') != (report.get('render_identity') or {}).get('sha256')
            or not asset.get('sha256') or storage.object_etag(key) != asset['etag']):
        raise ValueError('delivery_render_asset_unverified_or_changed')
    approved_render = report.get('approved_render_fingerprint')
    if approved_render:
        from delivery_freshness import render_fingerprint
        if approved_render != render_fingerprint(job):
            raise ValueError('delivery_approved_render_changed')
    return report


def require_delivery_review(job):
    """One guard for approval, direct publication and campaign publication."""
    report = require_current_evidence(job)
    if report is None:
        return
    from delivery_qc_runtime import approval_gate
    gate = approval_gate(report, 'enforce')
    if gate['blocked']:
        raise ValueError('delivery_review_pending:' + gate['reason'])


def bind_uploaded_render(job_id, local_path, key, *, expected_etag=None):
    """Seal the inspected local bytes to the object just uploaded/downloaded.

    Rechecks supply the ETag read before downloading. A replacement during
    inspection cannot inherit decisions from the earlier video.
    """
    from database import Job, SessionLocal
    from quality_rollout import review_required
    from campaign_render_evidence import file_hash
    import storage
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).with_for_update().first()
        if job is None or not review_required(job):
            return None
        report = dict(job.delivery_qc or {})
        identity = report.get('render_identity') or {}
        etag = storage.object_etag(key)
        if (not etag or (expected_etag is not None and etag != expected_etag)
                or file_hash(local_path) != identity.get('sha256')):
            return None
        report['render_asset'] = {'key': key, 'etag': etag, 'sha256': identity['sha256']}
        job.delivery_qc = report
        db.commit()
        return report
