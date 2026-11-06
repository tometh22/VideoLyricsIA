from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from change_request_workflow import case_state, published_answer, render_state
from delivery_snapshots import copy_snapshot, portal_key

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize('proposal_status', ['ready', 'partial', 'needs_input', 'stale', 'dismissed'])
@pytest.mark.parametrize('job_status', ['done', 'pending_review'])
@pytest.mark.parametrize('pending_master', [False, True])
def test_manually_corrected_current_cut_can_progress_without_reapplying_proposal(
        proposal_status, job_status, pending_master):
    publication = dict(job_status=job_status, render_matches_editor=True,
                       needs_publish=True, can_render=True, editor_revision=8,
                       render_fingerprint='current-render',
                       prores_pending=['umg_master'] if pending_master else [])
    before = publication.copy()
    result = case_state(publication=publication, proposal_status=proposal_status,
                        pending_manual=2)
    assert result['key'] == 'publish'
    assert result['activeStep'] == 3  # Final review, not applied/published proof.
    assert ('prepare_master' if pending_master else 'publish') in result['allowed_actions']
    assert ('publish' if pending_master else 'prepare_master') not in result['allowed_actions']
    assert 'review_proposal' in result['allowed_actions']
    assert 'propuesta sigue pendiente' in result['detail']
    assert 'sin comprobación automática' in result['detail']
    assert result['pending_manual'] == 2
    assert publication == before


@pytest.mark.parametrize('proposal_status', ['ready', 'partial', 'needs_input', 'stale', 'dismissed'])
@pytest.mark.parametrize('missing_evidence', [
    {'render_matches_editor': False}, {'render_matches_editor': None},
    {'render_matches_editor': 1}, {'needs_publish': False}, {'needs_publish': None},
    {'render_fingerprint': None}, {'render_fingerprint': ''}, {'render_fingerprint': ' '},
    {'editor_revision': None}, {'editor_revision': '8'}, {'editor_revision': True},
    {'editor_revision': -1}, {'job_status': 'editing'}, {'job_status': 'error'},
    {'job_status': None},
])
def test_pending_proposal_never_unlocks_publication_without_current_render_identity(
        proposal_status, missing_evidence):
    publication = dict(job_status='done', render_matches_editor=True,
                       needs_publish=True, can_render=True, editor_revision=8,
                       render_fingerprint='current-render', prores_pending=[])
    publication.update(missing_evidence)
    result = case_state(publication=publication, proposal_status=proposal_status)
    assert not {'publish', 'prepare_master'} & set(result['allowed_actions'])


def test_legacy_request85_finished_render_is_not_sent_back_to_editor():
    job = NS(status='pending_review', segments_revision=58, render_params={},
             previous_versions=[{'archived_at': '2026-09-18T01:00:00Z'}])
    document = NS(revision=58, updated_at=NOW)
    assert render_state(job, document, NS(submitted_at=NOW))['render_matches_editor']
    document.updated_at = datetime(2026, 9, 18, 2, tzinfo=timezone.utc)
    assert not render_state(job, document, NS(submitted_at=NOW))['render_matches_editor']


@pytest.mark.parametrize('status,revision,expected', [('pending_review', 8, True), ('done', 8, True), ('editing', 8, False), ('error', 8, False), ('done', 9, False)])
def test_completed_render_is_bound_to_exact_editor_revision(status, revision, expected):
    job = NS(status=status, segments_revision=revision,
             render_params={'_rendered_segments_revision': 8, '_rendered_at': NOW.isoformat()})
    assert render_state(job, NS(revision=revision), NS(submitted_at=NOW))['render_matches_editor'] is expected


def test_snapshot_preserves_published_bytes_across_working_rerender(monkeypatch):
    objects = {'tenant/job/lyric_video.mp4': b'old video'}
    def copy(source, target, *, expected_etag):
        assert expected_etag == objects[source].hex()
        objects[target] = objects[source]
        return True
    monkeypatch.setattr('storage.copy_object', copy)
    monkeypatch.setattr('storage.object_identity', lambda key: {
        'status': 'exists', 'etag': objects[key].hex(), 'size': len(objects[key])})
    keys = copy_snapshot('tenant', 'job', ['video'])
    delivery = NS(published_file_keys=keys)
    objects['tenant/job/lyric_video.mp4'] = b'corrected video'
    assert objects[portal_key(delivery, 'video')] == b'old video'
    updated = copy_snapshot('tenant', 'job', ['video'])
    assert updated != keys
    assert objects[updated['video']] == b'corrected video'
    assert portal_key(delivery, 'umg_master') is None


def test_snapshot_fails_closed_without_complete_copy(monkeypatch):
    monkeypatch.setattr('storage.object_identity', lambda _: {'status': 'exists', 'etag': 'e', 'size': 1})
    monkeypatch.setattr('storage.copy_object', lambda *_, **__: False)
    with pytest.raises(RuntimeError):
        copy_snapshot('tenant', 'job', ['video'])


def test_legacy_missing_master_is_not_a_permanent_render_blocker(monkeypatch):
    monkeypatch.setattr('storage.object_identity', lambda key: {'status': 'missing'} if key.endswith('.mov') else {'status': 'exists', 'etag': 'e', 'size': 1})
    monkeypatch.setattr('storage.copy_object', lambda *_, **__: True)
    assert list(copy_snapshot('t', 'j', ['video', 'umg_master'], allow_missing=True)) == ['video']
    monkeypatch.setattr('storage.object_identity', lambda _: {'status': 'unavailable'})
    with pytest.raises(RuntimeError):
        copy_snapshot('t', 'j', ['video'], allow_missing=True)


def _published(**overrides):
    publication = dict(job_status='done', render_matches_editor=True, needs_publish=False,
                       can_render=True, editor_revision=8, render_fingerprint='current-render',
                       prores_pending=[], revision=2)
    publication.update(overrides)
    return publication


def test_cut_already_published_after_the_fix_offers_one_click_close():
    result = case_state(publication=_published(answers_request=True))
    assert result['key'] == 'review'
    assert 'confirm_publication' in result['allowed_actions']


@pytest.mark.parametrize('answers', [False, None, 'true'])
def test_publication_close_needs_explicit_evidence(answers):
    result = case_state(publication=_published(answers_request=answers))
    assert result['key'] == 'review'
    assert 'confirm_publication' not in result['allowed_actions']


def test_campaign_song_renders_through_the_editor_approval_not_the_panel():
    # The panel render answers 409 lyrics_and_timing_approval_missing for
    # campaign songs (Cuando Miro a Tus Ojos, 2026-10-03).
    campaign = case_state(publication=_published(render_matches_editor=False, campaign_job=True))
    assert campaign['key'] == 'render'
    assert 'approve_in_editor' in campaign['allowed_actions']
    assert 'review_render' not in campaign['allowed_actions']
    plain = case_state(publication=_published(render_matches_editor=False))
    assert 'review_render' in plain['allowed_actions']


def _answer_case(**changes):
    from datetime import timedelta
    submitted = NOW
    values = dict(
        job=NS(job_id='j', status='done', segments_revision=8, edit_count=1, previous_versions=[],
               completed_at=NOW, render_params={'_rendered_segments_revision': 8,
                                                '_rendered_at': (NOW + timedelta(hours=2)).isoformat()}),
        document=NS(revision=8, updated_at=NOW + timedelta(hours=1)),
        request=NS(resolved_at=None, submitted_at=submitted),
    )
    from delivery_freshness import render_fingerprint
    values['delivery'] = NS(removed_at=None, published_render_fingerprint=render_fingerprint(values['job']),
                            content_updated_at=NOW + timedelta(hours=3), stale_since=None, stale_reason=None,
                            added_at=NOW)
    for key, value in changes.items():
        target, attr = key.split('__')
        setattr(values[target], attr, value)
    return published_answer(values['job'], values['document'], values['delivery'], values['request'])


def test_published_answer_accepts_fix_saved_rendered_and_published_after_request():
    assert _answer_case() == (True, 'ok')


@pytest.mark.parametrize('change,reason', [
    ({'document__updated_at': datetime(2026, 9, 17, tzinfo=timezone.utc)}, 'no_fix_after_request'),
    ({'document__revision': 9}, 'render_not_current'),
    ({'delivery__published_render_fingerprint': 'older-cut'}, 'not_published'),
    ({'delivery__content_updated_at': datetime(2026, 9, 17, tzinfo=timezone.utc)}, 'published_before_request'),
    ({'job__status': 'editing'}, 'not_applicable'),
    ({'request__resolved_at': NOW}, 'not_applicable'),
    ({'delivery__removed_at': NOW}, 'not_applicable'),
])
def test_published_answer_refuses_without_evidence(change, reason):
    assert _answer_case(**change) == (False, reason)
