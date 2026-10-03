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
