import json

from scripts import finish_reviewer_campaign as completion
from shadow_reference_import import digest


def fixture(tmp_path, monkeypatch):
    source = {'job_id': 'example', 'audio_revision': 1}
    song = {**source, 'segments': [{'text': 'Example'}]}
    snapshot = {'jobs': [song]}
    manifest = {'songs': [{'job_id': 'example', 'source': source, 'status': 'blocked'}]}
    folder = tmp_path / 'campaign-300' / 'example'
    (folder / 'requests').mkdir(parents=True)
    failed = {'source': source, 'provider': 'google', 'tool_status': 'invalid_response',
              'received_audio': True,
              'prompt_version': 'blind-vocal-events-shadow-v2-bounded-schema',
              'window': {'start': 0., 'end': 24., 'offset_seconds': 0.}}
    path = folder / 'requests' / 'original.json'
    path.write_text(json.dumps(failed))
    monkeypatch.setattr(completion, 'cached_receipts', lambda *a, **kw: {'receipts': []})
    monkeypatch.setattr(completion, 'covered', lambda *a: False)
    return snapshot, manifest, folder, failed, path


def test_one_subdivision_per_parent_prefers_retry(tmp_path, monkeypatch):
    snapshot, manifest, folder, failed, _ = fixture(tmp_path, monkeypatch)
    retry = folder / 'retry-1' / 'requests' / 'retry.json'
    retry.parent.mkdir(parents=True)
    retry.write_text(json.dumps(failed))
    plan = completion.recovery_plan(tmp_path, snapshot, manifest, [])
    assert len(plan) == 1 and plan[0]['failed'] == str(retry)


def test_unknown_completion_and_protected_current_source_not_retried(tmp_path, monkeypatch):
    snapshot, manifest, folder, failed, path = fixture(tmp_path, monkeypatch)
    failed['tool_status'] = 'unknown_completion'
    path.write_text(json.dumps(failed))
    assert not completion.recovery_plan(tmp_path, snapshot, manifest, [])
    failed['tool_status'] = 'invalid_response'
    failed['source'] = {**failed['source'], 'audio_revision': 2}
    path.write_text(json.dumps(failed))
    assert not completion.recovery_plan(tmp_path, snapshot, manifest, [])


def test_existing_recovery_attempt_never_repeated(tmp_path, monkeypatch):
    snapshot, manifest, folder, failed, _ = fixture(tmp_path, monkeypatch)
    existing = folder / 'bounded-recovery' / digest(failed['window']) / 'requests'
    existing.mkdir(parents=True)
    (existing / 'unknown.attempt.json').write_text('{}')
    assert not completion.recovery_plan(tmp_path, snapshot, manifest, [])


def test_successful_coverage_not_purchased_again(tmp_path, monkeypatch):
    snapshot, manifest, _, _, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(completion, 'covered', lambda *a: True)
    assert not completion.recovery_plan(tmp_path, snapshot, manifest, [])
