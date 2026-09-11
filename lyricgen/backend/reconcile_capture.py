"""Bounded, prospective staging evidence; never runs a recognizer or candidate.

Payloads travel through the existing private machine-evidence transaction.
The Redis set reserves at most six ordinary jobs before their outputs exist.
An expired/disabled/unavailable collector is a no-op, including on failures.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 'reconcile-stage-capture-v1'
MAX_BYTES = 4 * 1024 * 1024
MAX_STAGES = 64
MAX_JOBS = 6
CONFIG_KEYS = (
    'ANCHOR_TEXT_GATE_ENABLED', 'RECONCILE_GAP_RECOVERY_ENABLED',
    'RECONCILE_MIN_AUDIO_COVERAGE', 'POST_RECONCILE_CLEANUP_ENABLED',
    'BEAT_SNAP_ENABLED', 'BEAT_SNAP_WINDOW_MS', 'LYRIC_LEAD_IN_S',
    'LYRIC_HOLD_S', 'WHISPERX_MAX_LINE_S', 'WHISPERX_MIN_SPLIT_GAP_S',
    'LINE_TEXT_CORRECT_ENABLED', 'LARGE_GAP_CLUSTER_FIX_ENABLED',
)
CODE_FILES = (
    'main.py', 'whisperx_reconcile.py', 'forced_align.py', 'post_reconcile.py',
    'transcribe_postprocess.py', 'whisperx_transcribe.py', 'beat_snap.py',
    'chorus_trim.py', 'lead_in.py', 'line_evidence.py', 'segment_timing.py',
    'transcription_worker.py', 'reconcile_capture.py', 'reconcile_replay.py',
)
_RESERVE = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 0 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return redis.call('SCARD', KEYS[1])
"""


def _clone(value):
    # Reject lossy snapshots rather than inventing values for replay.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Capture:
    def __init__(self, payload=None):
        self.payload = payload

    def record(self, stage, value):
        if self.payload is None or self.payload.get('incomplete'):
            return
        try:
            row = {'stage': stage, 'value': _clone(value)}
            if len(self.payload['stages']) >= MAX_STAGES:
                raise ValueError('stage_limit')
            if len(json.dumps(self.payload).encode()) + len(json.dumps(row).encode()) > MAX_BYTES:
                raise ValueError('byte_limit')
            self.payload['stages'].append(row)
        except Exception as exc:
            # Preserve a small explicit failure marker; transcription survives.
            self.payload['incomplete'] = type(exc).__name__

    def audio(self, role, path):
        if self.payload is None or role in self.payload['audio']:
            return
        try:
            self.payload['audio'][role] = {
                'sha256': _digest(path), 'bytes': Path(path).stat().st_size,
            }
        except Exception:
            self.payload['incomplete'] = 'audio_identity_unavailable'

    def beats(self, value):
        self.record('beat_context', value)

    def snapshot(self):
        try:
            return _clone(self.payload)
        except Exception:
            return None


def begin(job_id, audio_path, *, route_context, now=None, reserve=None):
    """Fail open for transcription, fail closed for capture eligibility."""
    empty = Capture()
    try:
        if os.environ.get('ENVIRONMENT') != 'staging' or os.environ.get('RECONCILE_CAPTURE_ENABLED') != '1':
            return empty
        now = now or datetime.now(timezone.utc)
        until = datetime.fromisoformat(os.environ.get('RECONCILE_CAPTURE_UNTIL', '').replace('Z', '+00:00'))
        ttl = int((until - now).total_seconds())
        cohort = os.environ.get('RECONCILE_CAPTURE_COHORT', '')
        if not cohort or not job_id or not 0 < ttl <= 7 * 86400:
            return empty
        if reserve is None:
            from redis import Redis
            client = Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=1, socket_timeout=1)
            try:
                ordinal = client.eval(_RESERVE, 1, 'reconcile-capture:' + hashlib.sha256(cohort.encode()).hexdigest(), job_id, MAX_JOBS, ttl)
            finally:
                client.close()
        else:
            ordinal = reserve(cohort, job_id, MAX_JOBS, ttl)
        if not 1 <= int(ordinal) <= MAX_JOBS:
            return empty
        from transcription_quality import runtime_identity, _PIPELINE_CONFIG_KEYS
        keys = set(CONFIG_KEYS) | set(_PIPELINE_CONFIG_KEYS)
        root = Path(__file__).resolve().parent
        payload = {
            'schema': SCHEMA, 'job_id': job_id, 'cohort': cohort,
            'ordinal': int(ordinal), 'selected_at': now.isoformat(),
            'selection': 'first-six-ordinary-jobs-before-recognition',
            'runtime': runtime_identity(),
            'code_sha256': {name: _digest(root / name) for name in CODE_FILES},
            'configuration': {key: os.environ.get(key) for key in sorted(keys)},
            'route_context': _clone(route_context), 'audio': {}, 'stages': [],
        }
        result = Capture(payload)
        result.audio('uploaded_input', audio_path)
        return result
    except Exception:
        return empty


def durable_capture(result):
    """Called only while building the normal immutable machine snapshot."""
    try:
        payload = _clone(result.get('_reconcile_capture'))
        if not isinstance(payload, dict) or payload.get('schema') != SCHEMA:
            return None
        trace = Capture(payload)
        trace.record('final_machine_document', result.get('segments'))
        trace.record('post_presentation_decisions', {
            key: value for key, value in result.items()
            if key in ('timing_source', 'ctc_retime', 'anchor_alignment',
                       'phrase_segmentation', 'repetition_reconcile', 'coverage_warning')
        })
        return trace.snapshot()
    except Exception:
        return None


def record_result(result, stage):
    """Observe a normal post-pass; only its private transport payload changes."""
    try:
        payload = result.get('_reconcile_capture')
        if isinstance(payload, dict) and payload.get('schema') == SCHEMA:
            Capture(payload).record(stage, {
                'segments': result.get('segments'),
                'timing_source': result.get('timing_source'),
            })
    except Exception:
        pass
