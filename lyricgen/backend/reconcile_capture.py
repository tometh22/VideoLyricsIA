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
    'ADLIB_CONSENSUS_ENABLED', 'CHORUS_SNAP_ENABLED', 'WORD_VOTE_ENABLED',
    'REPETITION_RECONCILE_ENABLED', 'TIMING_CONSISTENCY_ENABLED',
    'KARAOKE_FA_RETIME_ENABLED', 'LYRICS_FORMAT_ENABLED', 'CTC_ALIGN_COMPUTE_STEM',
    'PHRASE_SEGMENTER_ENABLED', 'PHRASE_SEG_TARGET_LEN', 'PHRASE_SEG_MAX_LEN',
    'PHRASE_SEG_MIN_LEN', 'PHRASE_SEG_MAX_DUR_S', 'PHRASE_SEG_GAP_CAP_S',
    'GAP_RESCUE_ENABLED', 'GAP_RESCUE_MIN_GAP_S', 'GAP_RESCUE_CLIP_MAX_S',
    'GAP_RESCUE_CONTEXT_S', 'GAP_RESCUE_MAX_GAPS',
)
CODE_FILES = (
    'main.py', 'whisperx_reconcile.py', 'forced_align.py', 'post_reconcile.py',
    'transcribe_postprocess.py', 'whisperx_transcribe.py', 'beat_snap.py',
    'chorus_trim.py', 'lead_in.py', 'line_evidence.py', 'segment_timing.py',
    'transcription_worker.py', 'reconcile_capture.py', 'reconcile_replay.py',
    'ctc_align.py', 'lyrics_format.py', 'phrase_segmenter.py', 'chorus_snap.py',
    'word_vote.py', 'gap_rescue.py', 'repetition_reconcile.py', 'karaoke_align.py',
)
_RESERVE = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 0 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return redis.call('SCARD', KEYS[1])
"""

# A bounded cohort starts on its first ordinary admission. Keep the small
# closed-cohort ledger: expiring it would silently permit another six jobs.
_RESERVE_WINDOW = """
local now = tonumber(redis.call('TIME')[1])
local deadline = tonumber(redis.call('HGET', KEYS[1], 'deadline') or '0')
if deadline == 0 then
  deadline = now + tonumber(ARGV[3])
  redis.call('HSET', KEYS[1], 'started_at', now, 'deadline', deadline, 'count', 0)
end
local started = tonumber(redis.call('HGET', KEYS[1], 'started_at'))
if now >= deadline or redis.call('HEXISTS', KEYS[1], 'job:' .. ARGV[1]) == 1 then
  return {0, started, deadline}
end
local count = tonumber(redis.call('HGET', KEYS[1], 'count'))
if count >= tonumber(ARGV[2]) then return {0, started, deadline} end
count = count + 1
redis.call('HSET', KEYS[1], 'count', count, 'job:' .. ARGV[1], count)
return {count, started, deadline}
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
        cohort = os.environ.get('RECONCILE_CAPTURE_COHORT', '')
        window = int(os.environ.get('RECONCILE_CAPTURE_WINDOW_SECONDS', '0'))
        until_value = os.environ.get('RECONCILE_CAPTURE_UNTIL', '')
        if not cohort or not job_id or (window and until_value):
            return empty
        if window:
            ttl = window
        else:
            until = datetime.fromisoformat(until_value.replace('Z', '+00:00'))
            ttl = int((until - now).total_seconds())
        if not 0 < ttl <= 7 * 86400:
            return empty
        window_metadata = None
        if reserve is None:
            from redis import Redis
            client = Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=1, socket_timeout=1)
            try:
                key = 'reconcile-capture:' + hashlib.sha256(cohort.encode()).hexdigest()
                admission = client.eval(_RESERVE_WINDOW if window else _RESERVE, 1,
                                        key + ':window' if window else key, job_id, MAX_JOBS, ttl)
            finally:
                client.close()
        else:
            admission = reserve(cohort, job_id, MAX_JOBS, ttl)
        if window:
            ordinal, started, deadline = map(int, admission)
            window_metadata = {'mode': 'first-ordinary-admission', 'started_at_epoch': started,
                               'deadline_epoch': deadline, 'seconds': deadline - started}
        else:
            ordinal = admission
        if not 1 <= int(ordinal) <= MAX_JOBS:
            return empty
        from transcription_quality import runtime_identity, _PIPELINE_CONFIG_KEYS
        keys = set(CONFIG_KEYS) | set(_PIPELINE_CONFIG_KEYS)
        root = Path(__file__).resolve().parent
        payload = {
            'schema': SCHEMA, 'job_id': job_id, 'cohort': cohort,
            'ordinal': int(ordinal), 'selected_at': now.isoformat(),
            'window': window_metadata,
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
                'postpass_stats': result.get('postpass_stats'),
                'anchor_alignment': result.get('anchor_alignment'),
            })
    except Exception:
        pass
