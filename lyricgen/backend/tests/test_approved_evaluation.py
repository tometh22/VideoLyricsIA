"""Approval and audio binding regressions, using synthetic lyrics/audio."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('build_benchmark_dataset')
evaluation = load('evaluate_approved_corrections')


def segment(text='alpha beta', start=1., end=3.):
    return {'start': start, 'end': end, 'text': text}


def approved_job(db, *, current_revision=2, approved_revision=2):
    from database import EditorDocument, EditorVersion, Job
    job_id = uuid.uuid4().hex[:12]
    rows = [segment()]
    db.add(Job(job_id=job_id, user_id=1, tenant_id='test', artist='Synthetic',
               song_title='Test', filename='audio.wav',
               input_r2_key='inputs/test.wav', segments_json=rows,
               segments_revision=current_revision, status='done'))
    db.add(EditorDocument(job_id=job_id, tenant_id='test', revision=current_revision,
                          current_segments=rows, original_segments=rows))
    db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job_id, tenant_id='test',
                         revision=approved_revision, segments=rows, is_approved=True,
                         reason='approve'))
    db.flush()
    return job_id


def test_done_job_with_unapproved_autosave_is_not_a_benchmark_target(db):
    job_id = approved_job(db, current_revision=3, approved_revision=2)
    assert builder._fetch_job(db, job_id) is None


def test_approved_target_is_bound_to_version_and_current_document(db):
    from database import EditorDocument
    job_id = approved_job(db)
    result = builder._fetch_job(db, job_id)
    assert result['segments_revision'] == 2 and result['approved_version_id']
    document = db.query(EditorDocument).filter_by(job_id=job_id).one()
    document.current_segments = [segment('different words')]
    db.flush()
    assert builder._fetch_job(db, job_id) is None


def test_cached_audio_from_another_revision_is_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setitem(sys.modules, 'storage', SimpleNamespace(
        download_object=lambda *a: calls.append(a)))
    (tmp_path / 'audio.wav').write_bytes(b'old audio')
    assert builder._download_audio('input.wav', tmp_path,
                                   hashlib.sha256(b'new audio').hexdigest()) is None
    assert calls == []


def test_unrelated_text_has_unknown_timing_instead_of_zero_error():
    result = evaluation.evaluate([segment()], [segment('unrelated text')])
    assert result['lead_occurrence_recall'] == 0
    assert result['display_onset_mean_s'] is None
    assert result['display_end_mean_s'] is None


def test_missing_repeated_occurrence_cannot_get_full_recall():
    result = evaluation.evaluate([segment(), segment(start=90., end=92.)], [segment()])
    assert result['lead_occurrence_recall'] == .5
    assert result['lead_matched_rows'] == 1


def test_inline_backing_words_do_not_turn_lead_timing_into_backing_evidence():
    result = evaluation.evaluate([segment('alpha beta\n(gamma delta)')],
                                  [segment()], inline_backing=True)
    assert result['full_text_wer'] == .5
    assert result['lead_text_wer'] == 0
    assert result['target_inline_backing_entries'] == 1
    assert result['independent_backing_timing_scored'] is False


def test_backing_only_target_has_no_claim_of_perfect_lead_accuracy():
    result = evaluation.evaluate([segment('(gamma delta)')], [], inline_backing=True)
    assert result['lead_text_wer'] is None
    assert result['lead_occurrence_recall'] is None


def test_blank_target_is_not_a_valid_approval_comparator():
    with pytest.raises(ValueError, match='empty_approved_target'):
        evaluation.evaluate([segment('')], [segment()])


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True, '1'])
def test_nonfinite_or_non_numeric_times_are_rejected(bad):
    with pytest.raises(ValueError, match='invalid_segment'):
        evaluation.evaluate([segment()], [segment(start=bad)])


def test_case_rejects_modified_target_or_audio(tmp_path):
    job_id = 'aabbccddeeff'
    rows = [segment()]
    snapshot = {'job_id': job_id, 'audio_revision': 1,
                'approved_version_id': 'v2', 'revision': 2, 'segments': rows,
                'versions': [{'id': 'v2', 'revision': 2, 'is_approved': True, 'segments': rows}],
                'input_audio_sha256': hashlib.sha256(b'original audio').hexdigest()}
    source = tmp_path / (job_id + '.json')
    source.write_text(json.dumps(snapshot))
    case = tmp_path / job_id
    case.mkdir()
    (case / 'audio.wav').write_bytes(b'original audio')
    (case / 'ground_truth.json').write_text(json.dumps(rows))
    (case / 'metadata.json').write_text(json.dumps({
        'job_id': job_id, 'audio_revision': 1,
        'approved_version_id': 'v2', 'approved_revision': 2,
        'source_snapshot_sha256': evaluation.file_hash(source),
        'audio_file': 'audio.wav', 'audio_sha256': snapshot['input_audio_sha256'],
    }))
    evaluation.validate_case(tmp_path, job_id)
    (case / 'ground_truth.json').write_text(json.dumps([segment('changed target')]))
    with pytest.raises(ValueError, match='approved_snapshot_mismatch'):
        evaluation.validate_case(tmp_path, job_id)
    (case / 'ground_truth.json').write_text(json.dumps(rows))
    (case / 'audio.wav').write_bytes(b'changed audio')
    with pytest.raises(ValueError, match='audio_hash_mismatch'):
        evaluation.validate_case(tmp_path, job_id)
