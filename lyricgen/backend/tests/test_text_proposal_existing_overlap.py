"""Text correction must preserve existing clocks without approving overlaps."""
from copy import deepcopy
import pytest
from editor import _validate_review_proposal_against_document, _strict_timeline


def fixture_proposal():
    rows = [
        {'start': 0., 'end': 2.16, 'text': 'first'},
        {'start': 2., 'end': 3., 'text': 'second'},
        {'start': 5., 'end': 7., 'text': 'wrong language'},
    ]
    proposed = {**rows[2], 'text': 'letra corregida'}
    return rows, {
        'kind': 'operator_review_proposal', 'schema': 'operator-review-proposal-v1',
        'review_only': True, 'operator_suggestion_only': True,
        'automatic_apply_allowed': False,
        'windows': [{'id': 'correction', 'start': 5., 'end': 7.,
                     'suggestion_type': 'text', 'confidence': 'medium',
                     'automatic_apply_allowed': False,
                     'current_segments': [deepcopy(rows[2])],
                     'proposed_segments': [proposed]}],
    }


def test_existing_overlap_does_not_block_unrelated_text_correction():
    rows, proposal = fixture_proposal()
    before = deepcopy(rows)
    result = _validate_review_proposal_against_document(proposal, rows)
    assert result['windows'][0]['proposed_segments'][0]['text'] == 'letra corregida'
    assert rows == before
    # The ordinary timeline gate remains strict; this is not approval or repair.
    with pytest.raises(ValueError, match='overlapping'):
        _strict_timeline(rows, label='approval')


@pytest.mark.parametrize('mutation', ['end', 'start', 'split', 'timing', 'automatic', 'not_operator'])
def test_text_label_cannot_exempt_structural_or_timing_changes(mutation):
    rows, proposal = fixture_proposal()
    window = proposal['windows'][0]
    if mutation in {'start', 'end'}:
        window['proposed_segments'][0][mutation] += .1
    elif mutation == 'split':
        window['proposed_segments'].append(deepcopy(window['proposed_segments'][0]))
    elif mutation == 'timing':
        window['suggestion_type'] = 'timing'
    elif mutation == 'automatic':
        proposal['automatic_apply_allowed'] = True
    else:
        proposal['operator_suggestion_only'] = False
    with pytest.raises(ValueError, match='overlapping'):
        _validate_review_proposal_against_document(proposal, rows)


def test_text_proposal_still_requires_exact_current_snapshot():
    rows, proposal = fixture_proposal()
    proposal['windows'][0]['current_segments'][0]['text'] = 'stale text'
    with pytest.raises(ValueError, match='do not match'):
        _validate_review_proposal_against_document(proposal, rows)


def test_publish_and_apply_text_keeps_every_timestamp_and_approval_guard(monkeypatch):
    from database import SessionLocal, Job, EditorDocument
    from editor import (persist_operator_review_proposal_if_current,
                        apply_quality_proposal, validate_approval_snapshot)
    from transcription_quality import segments_hash
    from tests.test_editor_documents import _users_and_job
    monkeypatch.setenv('QUALITY_OPERATOR_SUGGESTIONS_ENABLED', '1')
    first, _, job_id = _users_and_job('text_existing_overlap')
    rows, proposal = fixture_proposal()
    before = deepcopy(rows)
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        job.input_audio_sha256 = 'a' * 64
        job.audio_revision = 1
        job.segments_json = deepcopy(rows)
        document.current_segments = deepcopy(rows)
        document.original_segments = deepcopy(rows)
        db.commit()
        assert persist_operator_review_proposal_if_current(
            db, job_id=job_id, expected_revision=document.revision,
            expected_segments_hash=segments_hash(rows), expected_audio_revision=1,
            expected_audio_sha256='a' * 64, proposal=proposal)
        db.commit()
        stored = document.quality_proposal
        document, version, applied = apply_quality_proposal(
            db, job, document, first.id, proposal_id=stored['id'],
            base_revision=document.revision, window_ids=['correction'],
            idempotency_key='text-overlap-apply-0001')
        db.commit()
        assert applied and version is not None
        assert document.current_segments[:2] == before[:2]
        assert [(r['start'], r['end']) for r in document.current_segments] == [
            (r['start'], r['end']) for r in before]
        assert document.current_segments[2]['text'] == 'letra corregida'
        assert job.status == 'transcribed_pending'
        with pytest.raises(ValueError, match='approval_overlap_requires_explicit_edit'):
            validate_approval_snapshot(document.current_segments)
