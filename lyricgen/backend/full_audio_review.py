"""Complete, blind acoustic audit. Evidence and review findings, never edits."""
from dataclasses import replace
from pathlib import Path
import os
import math
import tempfile
import time
from types import SimpleNamespace

from reviewer_integral import windows, union_seconds
from reviewer_shadow import ShadowPolicy, tokens
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, probe, check_sync
from shadow_reference_import import digest
from transcription_quality import segments_hash

VERSION = 'full-audio-review-v1'


def binding(snapshot):
    return {k: snapshot[k] for k in ('revision', 'audio_revision', 'audio_sha256')} | {
        'segments_hash': segments_hash(snapshot['segments'])}


def missing_intervals(duration, successful):
    result, end = [], 0.
    for a, b in sorted(successful):
        if a > end:
            result.append([end, a])
        end = max(end, b)
    if end < duration:
        result.append([end, duration])
    return result


def finding(code, start, end, summary, **extra):
    return {'code': code, 'start': start, 'end': end, 'seconds': [start],
            'summary': summary, 'category': 'acoustic_review', 'severity': 'WARN',
            'result_status': 'REVIEW', 'status': 'OPEN', 'blocking': True,
            'requires_recorded_review': True, 'automatic_apply_allowed': False, **extra}


def usable_response(response, provider, duration):
    if response.get('tool_status') != 'ok' or response.get('received_audio') is not True or response.get('conditioning_texts') != []:
        return False
    payload = response.get('response') or {}
    if provider == 'google':
        from reviewer_shadow_audio import valid_blind_response
        return valid_blind_response(payload) and all(e['end'] <= duration + .05 for e in payload['events'])
    words = payload.get('words')
    if not isinstance(words, list) or (not words and str(payload.get('text') or '').strip()):
        return False
    return all(isinstance(w, dict) and isinstance(w.get('word'), str)
               and all(isinstance(w.get(k), (int, float)) and math.isfinite(w[k]) for k in ('start', 'end'))
               and 0 <= w['start'] <= w['end'] <= duration + .05 for w in words)


def compare_window(window, response, segments, provider):
    payload = response.get('response') or {}
    events = payload.get('events', []) if provider == 'google' else payload.get('words', [])
    issues = []
    for event in events:
        a, b = event.get('start'), event.get('end')
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= window['end'] - window['start'] + .05:
            continue
        a, b = a + window['start'], b + window['start']
        text = event.get('text', event.get('word', ''))
        overlapping = [s for s in segments if s['start'] < b and s['end'] > a]
        if text.strip() and not overlapping:
            issues.append(finding('ACOUSTIC_POSSIBLE_OMISSION', a, b, 'Posible voz sin letra visible', heard_text=text, provider=provider))
    # Compare a whole phrase against all overlapping ASR words/events, never
    # against one isolated token. Both text and clocks remain hypotheses.
    if provider in {'google', 'openai'}:
        for segment in segments:
            if not window['start'] <= segment['start'] < segment['end'] <= window['end']:
                continue
            heard = [e for e in events if isinstance(e.get('start'), (int, float)) and isinstance(e.get('end'), (int, float))
                     and e['start'] + window['start'] < segment['end'] and e['end'] + window['start'] > segment['start']]
            text = ' '.join(e.get('text', e.get('word', '')) for e in heard)
            if tokens(text) != tokens(segment['text']):
                issues.append(finding('ACOUSTIC_TEXT_DISCREPANCY', segment['start'], segment['end'],
                    'La segunda escucha difiere de la letra', expected_text=segment['text'], heard_text=text, provider=provider))
            elif heard and (provider == 'openai' or len(heard) == 1) and all(e['end'] > e['start'] for e in heard):
                for field, tolerance in [('start', .5), ('end', .75)]:
                    candidate = (min(e['start'] for e in heard) if field == 'start' else max(e['end'] for e in heard)) + window['start']
                    if abs(candidate - segment[field]) > tolerance:
                        issues.append(finding('ACOUSTIC_' + field.upper(), segment['start'], segment['end'],
                            'Revisar entrada' if field == 'start' else 'Revisar salida', proposed_time=candidate, provider=provider))
        if payload.get('editorial_ambiguity'):
            issues.append(finding('ACOUSTIC_AMBIGUITY', window['start'], window['end'], 'Voces superpuestas o límite incierto', provider=provider))
    return issues


def deduplicate_findings(rows):
    """Merge witnesses of one occurrence, never text matches in other choruses."""
    unique = []
    for row in rows:
        matched = None
        for old in unique:
            if (row['code'] != old['code'] or tokens(row.get('heard_text', '')) != tokens(old.get('heard_text', ''))
                    or row.get('expected_text') != old.get('expected_text')):
                continue
            overlap = max(0, min(row['end'], old['end']) - max(row['start'], old['start']))
            span = max(row['end'], old['end']) - min(row['start'], old['start'])
            same_occurrence = row['start'] == old['start'] and row['end'] == old['end'] or (span > 0 and overlap / span >= .65)
            if same_occurrence and abs(row.get('proposed_time', 0) - old.get('proposed_time', 0)) <= .25:
                matched = old
                break
        witness = {k: row[k] for k in ('provider', 'analysis_window', 'start', 'end', 'proposed_time') if k in row}
        if matched is None:
            identity = digest([row['code'], round(row['start'], 3), round(row['end'], 3), row.get('heard_text'), row.get('proposed_time')])[:24]
            matched = {**row, 'issue_id': identity, 'providers': [], 'witnesses': []}
            unique.append(matched)
        if row.get('provider') and row['provider'] not in matched['providers']:
            matched['providers'].append(row['provider'])
        if witness not in matched['witnesses']:
            matched['witnesses'].append(witness)
    return unique


def audit(snapshot, mix_path, stem_path, cache_dir, *, listener_factory=BlindAudioTools):
    started = time.monotonic()
    duration = probe(mix_path)
    report = {'schema_version': VERSION, 'binding': binding(snapshot), 'duration': duration,
              'status': 'COMPLETE', 'issues': [], 'coverage': {}, 'calls': 0,
              'automatic_apply_allowed': False, 'observed_cost_usd': None}
    if file_sha(mix_path) != snapshot['audio_sha256']:
        return {**report, 'status': 'FAILED', 'reason': 'audio_identity_changed'}
    if not math.isfinite(duration) or duration <= 0:
        return {**report, 'status': 'NOT_RUN', 'reason': 'invalid_audio_duration'}
    planned = windows(duration)
    policy = replace(ShadowPolicy(), version=VERSION, max_windows_per_song=len(planned), max_calls_per_song=2 * len(planned))
    root = Path(cache_dir) / digest(snapshot.get('tenant_id', 'unknown')) / snapshot['audio_sha256'] / VERSION
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    listener = listener_factory(root / 'requests', policy=policy)
    sync = {'mix_stem_sync_verified': False}
    if stem_path:
        try:
            sync = check_sync(mix_path, stem_path)
        except Exception:
            pass
    report['stem_sync'] = sync
    source_hashes = {'mix': snapshot['audio_sha256']}
    if sync['mix_stem_sync_verified']:
        source_hashes['stem'] = file_sha(stem_path)
    source = {'audio_sha256': snapshot['audio_sha256'], 'tenant': digest(snapshot.get('tenant_id', 'unknown')), 'audit_version': VERSION}
    observations = []
    successful = {'openai': [], 'google': []}
    for index, window in enumerate(planned):
        for provider in successful:
            use_stem = provider == 'google' and sync['mix_stem_sync_verified']
            view = 'stem' if use_stem else 'mix'
            source_path = stem_path if use_stem else mix_path
            clip = root / f'{index}-{provider}-{view}-{source_hashes[view][:16]}.wav'
            try:
                # Content-address the stem as well as the mix. ffmpeg uses -n.
                if not clip.exists():
                    extract_clip(source_path, window, clip)
                    clip.chmod(0o600)
                response = listener.listen(clip, provider=provider, view=view, source=source, window=window)
                if usable_response(response, provider, window['end'] - window['start']):
                    successful[provider].append([window['start'], window['end']])
                    report['issues'].extend({**issue, 'analysis_window': window}
                        for issue in compare_window(window, response, snapshot['segments'], provider))
                observations.append({'provider': provider, 'view': view, 'window': window,
                    'status': response.get('tool_status'), 'usage': response.get('usage'),
                    'cache_hit': response.get('cache_hit', False), 'latency_seconds': response.get('latency_seconds')})
            except Exception as exc:
                observations.append({'provider': provider, 'window': window, 'status': 'failed', 'error_type': type(exc).__name__})
    for provider, intervals in successful.items():
        gaps = missing_intervals(duration, intervals)
        report['coverage'][provider] = {'seconds': union_seconds(intervals), 'fraction': min(1., union_seconds(intervals) / duration), 'missing': gaps}
        for a, b in gaps:
            report['issues'].append(finding('ACOUSTIC_NOT_RUN', a, b, 'Tramo sin segunda escucha completa', provider=provider))
    report.update(issues=deduplicate_findings(report['issues']), observations=observations, calls=listener.calls,
                  elapsed_seconds=round(time.monotonic() - started, 3))
    return report


def run_full_review_job(job_id, expected_binding):
    from database import Job, SessionLocal
    from quality_jobs import _snapshot
    from quality_rollout import full_review
    import storage
    snapshot = _snapshot(job_id)
    if snapshot is None or binding(snapshot) != expected_binding:
        return {'status': 'stale'}
    if not full_review(SimpleNamespace(job_id=job_id)):
        return {'status': 'disabled'}
    report = {'schema_version': VERSION, 'status': 'NOT_RUN', 'binding': expected_binding, 'coverage': {}, 'issues': []}
    cache = os.getenv('REVIEWER_ASSIST_CACHE_DIR')
    try:
        if not cache or not snapshot.get('input_r2_key'):
            report['reason'] = 'cache_or_source_unavailable'
        else:
            with tempfile.TemporaryDirectory(prefix='full-review-') as temp:
                audio = str(Path(temp) / 'master.audio')
                if not storage.download_object(snapshot['input_r2_key'], audio):
                    raise RuntimeError('audio_download_failed')
                # A separated voice is optional. The mix fallback is explicit.
                import vocal_sep
                stem = vocal_sep.separate_vocals(audio, cache_only=True)
                report = audit(snapshot, audio, stem, cache)
    except Exception as exc:
        report.update(status='FAILED', reason=type(exc).__name__)
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).with_for_update().first()
        current = _snapshot(job_id)
        if not job or current is None or binding(current) != expected_binding:
            return {'status': 'stale'}
        job.acoustic_review = report
        from delivery_qc_runtime import mark_delivery_qc_stale
        if job.delivery_qc:
            job.delivery_qc = mark_delivery_qc_stale(job.delivery_qc, revision=expected_binding['revision'], reason='acoustic_review_completed')
        db.commit()
    return {'status': report['status'], 'calls': report.get('calls', 0)}
