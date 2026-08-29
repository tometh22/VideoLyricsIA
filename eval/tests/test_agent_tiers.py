import pytest

from eval.agent_tiers import audit_rate, build_policy, dashboard, route_event


def _score(gate="GO_TIER_AGENT"):
    return {"categories": {
        "text": {"gate": gate},
        "timing": {"gate": "NO_GO"},
        "vocalization": {"gate": "BLOCKED_INSUFFICIENT_EVIDENCE"},
    }}


def test_policy_is_eligible_but_never_activates_itself():
    policy = build_policy(_score(), "2026-08-29T12:00:00Z")
    assert policy["categories"]["text"]["agent_eligible"] is True
    assert policy["categories"]["text"]["agent_enabled"] is False
    assert policy["categories"]["text"]["auto_enabled"] is False
    assert policy["runtime_activation_performed"] is False


def test_audit_rate_steps_down_after_two_weeks_and_live_stays_human():
    policy = build_policy(_score(), "2026-08-01T00:00:00Z")
    assert audit_rate(policy["activated_at"], "2026-08-10T00:00:00Z") == 0.20
    assert audit_rate(policy["activated_at"], "2026-08-16T00:00:00Z") == 0.10
    policy["categories"]["text"]["agent_enabled"] = True
    routed = route_event({"event_id": "e-1", "category": "text", "is_live": True}, policy, "2026-08-16T00:00:00Z")
    assert routed == {"event_id": "e-1", "resolver": "agus", "audit_required": False, "reason": "live_excluded"}


def test_dashboard_reports_resolution_share_minutes_and_reversals():
    report = dashboard([
        {"song_id": "song-1", "category": "text", "resolved_by": "agent", "human_seconds": 12, "audit_verdict": "confirmed"},
        {"song_id": "song-1", "category": "timing", "resolved_by": "agent", "human_seconds": 18, "audit_verdict": "reverted"},
        {"song_id": "song-2", "category": "text", "resolved_by": "agus", "human_seconds": 60},
        {"song_id": "song-2", "category": "timing", "resolved_by": "auto", "human_seconds": 0},
    ])
    assert report["resolution_share"]["agent"]["rate"] == pytest.approx(0.5)
    assert report["human_minutes_per_song"] == {"song-1": 0.5, "song-2": 1.0}
    assert report["agent_audit"]["reversal_rate"] == pytest.approx(0.5)
