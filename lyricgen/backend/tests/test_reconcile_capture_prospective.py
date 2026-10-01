"""Capture reliability only: no recognizer, human edit, or paid service."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
import reconcile_capture as rc


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('RECONCILE_CAPTURE_ENABLED', '1')
    monkeypatch.setenv('RECONCILE_CAPTURE_COHORT', 'new-selection-preserve-old')
    monkeypatch.setenv('RECONCILE_CAPTURE_POLICY', rc.FRESH_POLICY)
    monkeypatch.setenv('RECONCILE_CAPTURE_WINDOW_SECONDS', '604800')
    monkeypatch.delenv('RECONCILE_CAPTURE_UNTIL', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    audio = tmp_path / 'fixture.wav'
    audio.write_bytes(b'local test only')
    metadata = {'policy': rc.FRESH_POLICY, 'campaign_id': 'allowed', 'attempt_id': 'first'}
    monkeypatch.setattr(rc, '_fresh_campaign_context', lambda job: deepcopy(metadata))
    calls = []
    monkeypatch.setattr(rc, 'archive_checkpoint', lambda payload, **kw: calls.append((deepcopy(payload), kw)))
    return audio, calls


def test_ineligible_or_unverifiable_does_not_reserve_or_start_clock(fresh, monkeypatch):
    audio, calls = fresh
    def forbidden(*args):
        raise AssertionError('ineligible attempt reserved a slot')
    monkeypatch.setattr(rc, '_fresh_campaign_context', lambda job: None)
    assert rc.begin('qa', audio, route_context={}, reserve=forbidden).snapshot() is None
    def unavailable(job):
        raise RuntimeError('metadata cannot be verified')
    monkeypatch.setattr(rc, '_fresh_campaign_context', unavailable)
    assert rc.begin('unknown', audio, route_context={}, reserve=forbidden).snapshot() is None
    assert not calls


def test_checkpoint_precedes_reconcile_and_survives_final_transport_removal(fresh):
    audio, calls = fresh
    trace = rc.begin('ordinary', audio, route_context={}, reserve=lambda *a: [1, 100, 604900])
    assert trace.snapshot()['admission']['attempt_id'] == 'first'
    assert trace.snapshot()['configuration']['RECONCILE_CAPTURE_POLICY'] == rc.FRESH_POLICY
    assert len(calls) == 1 and not calls[0][0]['stages']
    wx = [{'text': 'hola', 'words': [{'word': 'hola', 'start': 1., 'end': 2.}]}]
    trace.record('reconcile_input', {'wx_segs': wx, 'reference_text': 'hola'})
    assert calls[-1][0]['stages'][-1]['stage'] == 'reconcile_input'
    wx.clear()
    assert calls[-1][0]['stages'][-1]['value']['wx_segs']
    result = {'segments': [{'text': 'hola', 'start': 1., 'end': 2.}], '_reconcile_capture': trace.snapshot()}
    durable = rc.durable_capture(result)
    result.pop('_reconcile_capture')
    assert calls[-1][1] == {'complete': True}
    assert calls[-1][0] == durable
    assert durable['stages'][-2]['value'] == result['segments']
    # Persistence may preserve an existing human document instead. The archive
    # is already complete; it requires no editor-row save or replacement.
    assert 'admission' in durable


def test_policy_misconfiguration_does_not_admit(fresh, monkeypatch):
    audio, calls = fresh
    monkeypatch.setenv('RECONCILE_CAPTURE_POLICY', 'typo')
    def forbidden(*args):
        raise AssertionError('misconfiguration reserved')
    assert rc.begin('j', audio, route_context={}, reserve=forbidden).snapshot() is None
    assert not calls


def test_archive_first_attempt_final_immutable_and_bounded(monkeypatch):
    import fakeredis
    import redis
    client = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, 'from_url', lambda *a, **k: client)
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    payload = {'cohort': 'v2', 'job_id': 'j', 'ordinal': 1, 'stages': [],
               'admission': {'policy': rc.FRESH_POLICY, 'attempt_id': 'first'}}
    key = 'reconcile-capture:' + hashlib.sha256(b'v2').hexdigest()
    archive = key + ':result:j'
    # No write without a real pre-recognition reservation.
    rc.archive_checkpoint(payload)
    assert client.get(archive) is None
    client.hset(key + ':window', mapping={'job:j': 1, 'count': 1})
    rc.archive_checkpoint(payload)
    first = client.get(archive)
    assert json.loads(first)['complete'] is False
    retry = deepcopy(payload)
    retry['admission']['attempt_id'] = 'second'
    rc.archive_checkpoint(retry, complete=True)
    assert client.get(archive) == first
    payload['stages'] = [{'stage': 'final_machine_document', 'value': []}]
    rc.archive_checkpoint(payload, complete=True)
    final = client.get(archive)
    envelope = json.loads(final)
    assert envelope['trace'] == payload and envelope['complete']
    assert envelope['trace_sha256'] == hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()).hexdigest()
    assert 604799 <= client.ttl(archive) <= 604800
    payload['stages'].clear()
    rc.archive_checkpoint(payload)
    assert client.get(archive) == final
    monkeypatch.setattr(rc, 'MAX_BYTES', 1)
    rc.archive_checkpoint(payload)
    assert client.get(archive) == final
    monkeypatch.setenv('ENVIRONMENT', 'production')
    rc.archive_checkpoint(payload)
    assert client.get(archive) == final
    assert client.hget(key + ':window', 'count') == b'1'


def test_archive_failure_does_not_fail_transcription(monkeypatch):
    def unavailable(*a, **k):
        raise ConnectionError('fixture failure')
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(Redis=SimpleNamespace(from_url=unavailable)))
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    rc.archive_checkpoint({'cohort': 'v2', 'job_id': 'j', 'ordinal': 1,
                          'admission': {'policy': rc.FRESH_POLICY, 'attempt_id': 'first'}})

@pytest.mark.parametrize('change', [
    {'campaign_id': None}, {'campaign_id': 'not-allowed'},
    {'campaign_item_id': None}, {'workload_class': 'interactive'},
    {'approved_at': datetime.now(timezone.utc)}, {'has_segments': True},
    {'has_document': True}, {'active_transcription_attempt_id': 'newer-attempt'},
])
def test_server_owned_scope_excludes_smokes_previous_results_humans_and_stale_attempts(change):
    facts = dict(campaign_id='allowed', campaign_item_id='real-item', workload_class='batch',
                 approved_at=None, has_segments=False, has_document=False,
                 active_transcription_attempt_id='first', input_audio_sha256='a' * 64, audio_revision=1)
    assert rc._fresh_admission(SimpleNamespace(**facts), ('transcription', 'first'), {'allowed'})
    facts.update(change)
    assert rc._fresh_admission(SimpleNamespace(**facts), ('transcription', 'first'), {'allowed'}) is None


def test_current_attempt_required():
    assert rc._fresh_admission(None, None, {'allowed'}) is None
    assert rc._fresh_admission(None, ('pipeline', 'first'), {'allowed'}) is None


def test_readonly_export_preserves_missing_outcomes_and_rejects_tampering(monkeypatch):
    import fakeredis
    import redis
    from scripts.export_reconcile_checkpoints import collect
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis.Redis, 'from_url', lambda *a, **k: client)
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    key = 'reconcile-capture:' + hashlib.sha256(b'v2').hexdigest()
    client.hset(key + ':window', mapping={'job:first': 1, 'job:missing': 2, 'count': 2})
    payload = {'cohort': 'v2', 'job_id': 'first', 'ordinal': 1, 'stages': [],
               'admission': {'policy': rc.FRESH_POLICY, 'attempt_id': 'original'}}
    rc.archive_checkpoint(payload)
    report = collect('v2')
    assert [s['state'] for s in report['slots']] == ['incomplete', 'missing_checkpoint'] + ['not_admitted'] * 4
    assert report['candidate_executed'] is False
    assert report['human_documents_read'] is False
    # No broad scan or replacement of the missing second execution.
    assert len(client.keys()) == 2
    archive = key + ':result:first'
    tampered = json.loads(client.get(archive))
    tampered['trace']['job_id'] = 'different'
    client.set(archive, json.dumps(tampered))
    with pytest.raises(ValueError, match='identity_mismatch'):
        collect('v2')


def test_metadata_query_is_readonly_and_does_not_load_lyrics(monkeypatch):
    from contextvars import ContextVar
    from sqlalchemy import Column, Table, MetaData, String, Integer, DateTime
    metadata = MetaData()
    jobs = Table('jobs', metadata, *[Column(name, String) for name in (
        'job_id', 'campaign_id', 'campaign_item_id', 'workload_class',
        'active_transcription_attempt_id', 'input_audio_sha256', 'segments_json')],
        Column('audio_revision', Integer), Column('approved_at', DateTime))
    docs = Table('editor_documents', metadata, Column('job_id', String))
    row = SimpleNamespace(campaign_id='allowed', campaign_item_id='item', workload_class='batch',
                          approved_at=None, active_transcription_attempt_id='first',
                          input_audio_sha256='a' * 64, audio_revision=1, has_segments=False, has_document=False)
    queries = []
    class Session:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, query):
            queries.append(str(query))
            return SimpleNamespace(first=lambda: row)
    monkeypatch.setitem(sys.modules, 'database', SimpleNamespace(
        Job=SimpleNamespace(**{c.name: c for c in jobs.c}),
        EditorDocument=SimpleNamespace(job_id=docs.c.job_id), SessionLocal=Session))
    current = ContextVar('fixture_attempt', default=('transcription', 'first'))
    monkeypatch.setitem(sys.modules, 'jobs', SimpleNamespace(_CURRENT_JOB_ATTEMPT=current))
    monkeypatch.setenv('RECONCILE_CAPTURE_CAMPAIGN_IDS', 'allowed')
    assert rc._fresh_campaign_context('j')['attempt_id'] == 'first'
    assert queries[:2] == ['SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout='1000'"]
    assert 'current_segments' not in queries[2] and 'original_segments' not in queries[2]
    assert 'has_segments' in queries[2] and 'EXISTS' in queries[2]
