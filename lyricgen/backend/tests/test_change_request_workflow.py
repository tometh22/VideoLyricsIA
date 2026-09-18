from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from change_request_workflow import render_state
from delivery_snapshots import copy_snapshot, portal_key

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


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
    def copy(source, target):
        objects[target] = objects[source]
        return True
    monkeypatch.setattr('storage.copy_object', copy)
    keys = copy_snapshot('tenant', 'job', ['video'])
    delivery = NS(published_file_keys=keys)
    objects['tenant/job/lyric_video.mp4'] = b'corrected video'
    assert objects[portal_key(delivery, 'video')] == b'old video'
    updated = copy_snapshot('tenant', 'job', ['video'])
    assert updated != keys
    assert objects[updated['video']] == b'corrected video'
    assert portal_key(delivery, 'umg_master') is None


def test_snapshot_fails_closed_without_complete_copy(monkeypatch):
    monkeypatch.setattr('storage.copy_object', lambda *_: False)
    with pytest.raises(RuntimeError):
        copy_snapshot('tenant', 'job', ['video'])


def test_legacy_missing_master_is_not_a_permanent_render_blocker(monkeypatch):
    monkeypatch.setattr('storage.object_status', lambda key: 'missing' if key.endswith('.mov') else 'exists')
    monkeypatch.setattr('storage.copy_object', lambda *_: True)
    assert list(copy_snapshot('t', 'j', ['video', 'umg_master'], allow_missing=True)) == ['video']
    monkeypatch.setattr('storage.object_status', lambda _: 'unavailable')
    with pytest.raises(RuntimeError):
        copy_snapshot('t', 'j', ['video'], allow_missing=True)
