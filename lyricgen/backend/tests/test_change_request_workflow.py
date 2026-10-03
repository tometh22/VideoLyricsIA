from types import SimpleNamespace

from change_request_workflow import project_change_request


def workflow(*, request=None, job=None, delivery=None, publication=None,
             verified=False, proposal=None, qc=None):
    return project_change_request(
        request or SimpleNamespace(requested_revision=1, resolved_at=None),
        job=job or SimpleNamespace(status="done"),
        delivery=delivery or SimpleNamespace(removed_at=None),
        publication=publication or {
            "revision": 1, "needs_publish": True,
            "internal_approval_current": True, "prores_pending": [],
        },
        verification_current=verified, proposal_status=proposal, qc_gate=qc,
    )


def test_new_cut_needs_approval_then_qc_then_verification_then_publish():
    base = {"revision": 1, "needs_publish": True, "prores_pending": []}
    assert workflow(publication={**base, "internal_approval_current": False})[
        "next_action"] == "review_render"
    assert workflow(publication={**base, "internal_approval_current": True},
                    qc={"blocked": True, "reason": "fresh_preflight_required"})[
        "next_action"] == "review_qc"
    assert workflow()["next_action"] == "verify"
    ready = workflow(verified=True)
    assert ready["next_action"] == "publish"
    assert ready["allowed_actions"] == ["publish", "edit", "resolve_manual"]


def test_prores_and_busy_render_are_distinct_actions():
    assert workflow(job=SimpleNamespace(status="rendering"))["phase"] == "rendering"
    assert workflow(publication={
        "revision": 1, "needs_publish": True, "internal_approval_current": True,
        "prores_pending": ["umg_master"], "prores_configured": False,
    })["next_action"] == "configure_prores"


def test_applied_proposal_on_old_done_video_requires_render_before_review():
    state = workflow(
        proposal="applied", publication={
            "revision": 1, "needs_publish": True,
            "internal_approval_current": False, "prores_pending": [],
        },
    )
    assert state["phase"] == "changes_saved"
    assert state["next_action"] == "render_changes"
    assert "publish" not in state["allowed_actions"]


def test_republish_same_cut_can_finish_request_only_if_revision_is_newer():
    publication = {
        "revision": 2, "needs_publish": False,
        "internal_approval_current": True, "prores_pending": [],
    }
    assert workflow(publication=publication, verified=True)["next_action"] == "publish"
    same = workflow(
        request=SimpleNamespace(requested_revision=2, resolved_at=None),
        publication=publication, verified=True,
    )
    assert same["next_action"] == "edit"
    assert "publish" not in same["allowed_actions"]


def test_resolved_and_missing_delivery_do_not_advertise_publication():
    resolved = workflow(request=SimpleNamespace(
        requested_revision=1, resolved_at="2026-10-02T12:00:00Z",
    ))
    assert resolved["allowed_actions"] == ["reopen"]
    missing = project_change_request(
        SimpleNamespace(requested_revision=1, resolved_at=None),
        job=None, delivery=None, publication=None,
        verification_current=False, proposal_status=None,
    )
    assert missing["phase"] == "blocked"
    assert missing["allowed_actions"] == []


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
