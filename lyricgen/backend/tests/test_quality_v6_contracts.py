import pytest

import quality_jobs
import quality_v6_calibration
from quality_v6_contracts import (
    CertifiedMutation,
    DIAGNOSTIC_SCHEMA,
    PROPOSAL_CANDIDATE_SCHEMA,
    PROPOSAL_WINDOW_SCHEMA,
    REVIEW_PROPOSAL_SCHEMA,
    DiagnosticFinding,
    ReviewProposal,
    ReviewProposalCandidate,
)


def _window():
    return {
        "kind": "review_proposal_window", "schema": PROPOSAL_WINDOW_SCHEMA,
        "id": "tail", "start": 60.0, "end": 83.3,
        "reasons": ["acoustic_cardinality_disagreement"],
        "current_segments": [{"start": 60.0, "end": 83.0, "text": "old"}],
        "proposed_segments": [{"start": 60.0, "end": 64.0, "text": "new"}],
    }


def _candidate():
    return {
        **_window(),
        "kind": "review_proposal_candidate",
        "schema": PROPOSAL_CANDIDATE_SCHEMA,
        "parent_window_id": "parent",
    }


def test_diagnostic_cannot_be_reinterpreted_as_a_review_proposal():
    finding = DiagnosticFinding.from_mapping({
        "kind": "diagnostic_finding", "schema": DIAGNOSTIC_SCHEMA,
        "id": "tail", "start": 60, "end": 83,
        "reasons": ["event_count"],
    })
    assert finding.reason_codes == ("event_count",)
    with pytest.raises(ValueError, match="proposal kind mismatch"):
        ReviewProposal.from_mapping(finding.__dict__)


def test_review_proposal_is_typed_review_only_and_requires_rows():
    proposal = ReviewProposal.from_mapping({
        "kind": "review_proposal", "schema": REVIEW_PROPOSAL_SCHEMA,
        "policy_version": "lyrics-quality-v6", "review_only": True,
        "windows": [_window()],
    })
    assert proposal.to_dict()["review_only"] is True
    with pytest.raises(ValueError, match="review-only"):
        ReviewProposal.from_mapping({
            "kind": "review_proposal", "schema": REVIEW_PROPOSAL_SCHEMA,
            "policy_version": "lyrics-quality-v6", "review_only": False,
            "windows": [_window()],
        })


@pytest.mark.parametrize("windows", [
    [_window(), "malformed"],
    {"tail": _window()},
])
def test_review_proposal_rejects_partially_malformed_window_arrays(windows):
    with pytest.raises(ValueError, match="proposal windows"):
        ReviewProposal.from_mapping({
            "kind": "review_proposal", "schema": REVIEW_PROPOSAL_SCHEMA,
            "policy_version": "lyrics-quality-v6", "review_only": True,
            "windows": windows,
        })


def test_candidate_rejects_scalar_reasons_instead_of_iterating_characters():
    candidate = {**_candidate(), "reasons": "event_count"}
    with pytest.raises(ValueError, match="candidate reasons"):
        ReviewProposalCandidate.from_mapping(candidate)


@pytest.mark.parametrize("metadata", [
    {"words": "private lyric payload"},
    {"pos": "0.5,0.5"},
    {"scale": float("nan")},
    {"rot": float("inf")},
    {"id": "invalid id with spaces"},
    {"words": [{"word": "x", "start": 2.0, "end": 1.0}]},
])
def test_proposal_segments_reject_malformed_or_nonfinite_metadata(metadata):
    candidate = _candidate()
    candidate["proposed_segments"] = [{
        "start": 60.0, "end": 64.0, "text": "new", **metadata,
    }]
    with pytest.raises(ValueError):
        ReviewProposalCandidate.from_mapping(candidate)


def test_certified_mutation_is_not_constructible_in_v6_runtime():
    with pytest.raises(RuntimeError, match="not authorized"):
        CertifiedMutation()


def test_proposal_builder_fails_closed_without_signed_calibration(monkeypatch):
    monkeypatch.delenv("QUALITY_V6_PROPOSALS_ENABLED", raising=False)
    proposal, telemetry = quality_jobs._build_review_proposal(
        _window()["current_segments"], [_candidate()],
        {"parent": {"complete": True}},
    )
    assert proposal is None
    assert telemetry["blocked"] is True
    assert "proposal_kill_switch_off" in telemetry["blockers"]
    # Telemetry is safe for global analytics: no raw row payload is copied.
    assert "old" not in str(telemetry) and "new" not in str(telemetry)


def test_observation_builder_requires_independent_source_families():
    candidate = _candidate()
    candidate["source_families"] = ["whisper_raw", "gemini_audio"]
    proposal, telemetry = quality_jobs._build_review_proposal(
        _window()["current_segments"], [candidate],
        {"parent": {"complete": True}}, observation_only=True,
    )
    assert telemetry["authorized_windows"] == 1
    assert proposal["observation_only"] is True
    assert proposal["windows"][0]["source_families"] == [
        "gemini_audio", "whisper",
    ]

    candidate["source_families"] = ["whisper_raw", "whisper_contextual"]
    proposal, telemetry = quality_jobs._build_review_proposal(
        _window()["current_segments"], [candidate],
        {"parent": {"complete": True}}, observation_only=True,
    )
    assert proposal is None
    assert "independent_source_family_missing" in telemetry["blockers"]


def test_only_complete_parent_windows_can_become_proposals(monkeypatch):
    monkeypatch.setattr(
        quality_v6_calibration, "runtime_review_proposal_authorization",
        lambda _certification: {
            "authorized": True, "review_only": True,
            "automatic_apply_allowed": False, "blockers": [],
        },
    )
    raw = [_candidate()]
    proposal, telemetry = quality_jobs._build_review_proposal(
        [], raw, {"parent": {"complete": False}},
    )
    assert proposal is None
    assert telemetry["candidates"] == 0

    proposal, telemetry = quality_jobs._build_review_proposal(
        _window()["current_segments"], raw, {"parent": {"complete": True}},
    )
    assert proposal["kind"] == "review_proposal"
    assert proposal["schema"] == REVIEW_PROPOSAL_SCHEMA
    assert proposal["policy_version"] == "lyrics-quality-v6"
    assert proposal["windows"][0]["id"] == "parent"
    assert telemetry["authorized_windows"] == 1


def test_runtime_review_authorization_requires_hash_pinned_artifacts(monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    monkeypatch.delenv("QUALITY_V6_DATASET_MANIFEST_PATH", raising=False)
    result = quality_v6_calibration.runtime_review_proposal_authorization({})
    assert result["authorized"] is False
    assert result["automatic_apply_allowed"] is False
    assert "pinned_artifacts_missing" in result["blockers"]


def test_diagnostic_with_proposal_shaped_extras_is_not_promoted(monkeypatch):
    monkeypatch.setattr(
        quality_v6_calibration, "runtime_review_proposal_authorization",
        lambda _certification: {
            "authorized": True, "review_only": True,
            "automatic_apply_allowed": False, "blockers": [],
        },
    )
    diagnostic = {
        "kind": "diagnostic_finding", "schema": DIAGNOSTIC_SCHEMA,
        "id": "diag", "parent_window_id": "parent",
        "start": 1.0, "end": 2.0, "reasons": ["event_count"],
        "current_segments": [{"start": 1.0, "end": 2.0, "text": "old"}],
        "proposed_segments": [{"start": 1.0, "end": 2.0, "text": "new"}],
    }
    proposal, telemetry = quality_jobs._build_review_proposal(
        diagnostic["current_segments"], [diagnostic],
        {"parent": {"complete": True}},
    )
    assert proposal is None
    assert telemetry["invalid_candidates"] == 1


def test_candidate_current_rows_must_match_builder_snapshot(monkeypatch):
    monkeypatch.setattr(
        quality_v6_calibration, "runtime_review_proposal_authorization",
        lambda _certification: {
            "authorized": True, "review_only": True,
            "automatic_apply_allowed": False, "blockers": [],
        },
    )
    proposal, telemetry = quality_jobs._build_review_proposal(
        [{"start": 60.0, "end": 83.0, "text": "actual"}],
        [_candidate()], {"parent": {"complete": True}},
    )
    assert proposal is None
    assert "proposal_current_segments_mismatch" in telemetry["blockers"]
