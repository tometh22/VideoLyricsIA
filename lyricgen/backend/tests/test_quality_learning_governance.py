"""Super-admin governance for quality-learning proposals."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import pytest
from fastapi import HTTPException

from database import (
    engine, QualityExperimentRun, QualityFixProposal, QualityPattern, SessionLocal,
)
from tests.conftest import auth


def _seed(status="draft", passed=False, candidate_config=None):
    db = SessionLocal()
    pattern_id = str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    try:
        db.add(QualityPattern(
            id=pattern_id, fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            category="missing_event", context_key="is_live=true",
            status="confirmed" if passed else "correlated",
            support_jobs=12, support_tenants=3,
            support_artists=3,
            baseline_rate=0.1, observed_rate=0.5, relative_risk=5,
            ci_low=1.2, ci_high=8.0, impact_seconds=90,
            evidence={
                "association_only": True,
                "raw_lyrics": "PRIVACY_SENTINEL_MUST_NEVER_LEAVE_DB",
            }, version=1,
            first_seen_at=now, last_seen_at=now, updated_at=now,
        ))
        # Persist the parent explicitly so this fixture exercises the real
        # PostgreSQL foreign key without depending on ORM insert ordering.
        db.flush()
        db.add(QualityFixProposal(
            id=proposal_id, pattern_id=pattern_id, proposal_type="routing_rule",
            title="Confirmar mezcla", hypothesis="Asociación; requiere ablation",
            status=status, version=1,
            candidate_config=(
                {"prefer_mix_witness": True}
                if candidate_config is None else candidate_config
            ),
            expected_impact={"minimum_relative_reduction": 0.2},
            validation_summary={"passed": True, "benchmark_report_hash": "b" * 64}
            if passed else None,
            action_idempotency_keys=[], created_at=now, updated_at=now,
        ))
        db.commit()
        return pattern_id, proposal_id
    finally:
        db.close()


def _cleanup(pattern_id):
    db = SessionLocal()
    try:
        proposal_ids = [value for value, in db.query(QualityFixProposal.id).filter(
            QualityFixProposal.pattern_id == pattern_id,
        ).all()]
        if proposal_ids:
            db.query(QualityExperimentRun).filter(
                QualityExperimentRun.proposal_id.in_(proposal_ids),
            ).delete(synchronize_session=False)
            db.query(QualityFixProposal).filter(
                QualityFixProposal.id.in_(proposal_ids),
            ).delete(synchronize_session=False)
        db.query(QualityPattern).filter(QualityPattern.id == pattern_id).delete()
        db.commit()
    finally:
        db.close()


def test_quality_learning_is_super_admin_only(client, user_token):
    response = client.get(
        "/admin/quality-learning/summary", headers=auth(user_token),
    )
    assert response.status_code == 403


def test_http_serializers_fail_closed_against_raw_evidence(client, admin_token, monkeypatch):
    pattern_id, _proposal_id = _seed()
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    try:
        response = client.get(
            f"/admin/quality-learning/patterns/{pattern_id}",
            headers=auth(admin_token),
        )
        assert response.status_code == 200
        assert "PRIVACY_SENTINEL" not in response.text
        assert "raw_lyrics" not in response.text
    finally:
        _cleanup(pattern_id)


def test_validate_is_optimistic_idempotent_and_no_render(
    client, admin_token, monkeypatch,
):
    pattern_id, proposal_id = _seed()
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    monkeypatch.setattr(
        "queue_jobs.enqueue_quality_proposal_validation",
        lambda proposal, experiment: f"queued:{proposal}:{experiment}",
    )
    body = {
        "reason": "Validar una variable en benchmark",
        "expected_version": 1,
        "idempotency_key": f"validate-{uuid.uuid4()}",
    }
    try:
        first = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/validate",
            headers=auth(admin_token), json=body,
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "validating"
        assert first.json()["experiments"][0]["metrics"] == {"render": False}
        repeated = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/validate",
            headers=auth(admin_token), json=body,
        )
        assert repeated.status_code == 200
        db = SessionLocal()
        try:
            assert db.query(QualityExperimentRun).filter(
                QualityExperimentRun.proposal_id == proposal_id,
            ).count() == 1
        finally:
            db.close()
        stale = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/reject",
            headers=auth(admin_token), json={
                **body, "idempotency_key": f"reject-{uuid.uuid4()}",
            },
        )
        assert stale.status_code == 409
    finally:
        _cleanup(pattern_id)


def test_validate_rejects_empty_ablation(client, admin_token, monkeypatch):
    pattern_id, proposal_id = _seed(candidate_config={})
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    try:
        response = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/validate",
            headers=auth(admin_token), json={
                "reason": "No debe validar sin variable",
                "expected_version": 1,
                "idempotency_key": f"empty-{uuid.uuid4()}",
            },
        )
        assert response.status_code == 422
    finally:
        _cleanup(pattern_id)


def test_proposal_and_ablation_kill_switches_fail_closed(
    client, admin_token, monkeypatch,
):
    pattern_id, proposal_id = _seed()
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    monkeypatch.setenv("QUALITY_LEARNING_ABLATIONS_ENABLED", "0")
    try:
        response = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/validate",
            headers=auth(admin_token), json={
                "reason": "Debe fallar cerrado",
                "expected_version": 1,
                "idempotency_key": f"disabled-{uuid.uuid4()}",
            },
        )
        assert response.status_code == 503
        assert response.json()["detail"]["flag"] == "QUALITY_LEARNING_ABLATIONS_ENABLED"
    finally:
        _cleanup(pattern_id)


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL advisory locks")
def test_concurrent_cross_proposal_idempotency_key_is_deterministic():
    from admin import (
        QualityLearningActionRequest, _proposal_for_action, _remember_proposal_action,
    )
    first_pattern, first_proposal = _seed()
    second_pattern, second_proposal = _seed()
    barrier = Barrier(2)
    key = f"shared-{uuid.uuid4()}"

    def reserve(proposal_id):
        db = SessionLocal()
        try:
            body = QualityLearningActionRequest(
                reason="Concurrent reservation",
                expected_version=1,
                idempotency_key=key,
            )
            barrier.wait(timeout=5)
            try:
                proposal, reused = _proposal_for_action(db, proposal_id, body)
                if reused and proposal.id != proposal_id:
                    return "conflict"
                _remember_proposal_action(proposal, key)
                db.commit()
                return "reserved"
            except HTTPException as exc:
                db.rollback()
                return "conflict" if exc.status_code == 409 else f"http-{exc.status_code}"
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, [first_proposal, second_proposal]))
        assert sorted(outcomes) == ["conflict", "reserved"]
    finally:
        _cleanup(first_pattern)
        _cleanup(second_pattern)


def test_approve_creates_signed_non_mutating_artifact(
    client, admin_token, monkeypatch,
):
    pattern_id, proposal_id = _seed(status="ready", passed=True)
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )).decode()
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    monkeypatch.setenv("QUALITY_LEARNING_SIGNING_PRIVATE_KEY", private_b64)
    monkeypatch.setenv("QUALITY_LEARNING_SIGNING_KEY_ID", "test-key")
    try:
        response = client.post(
            f"/admin/quality-learning/proposals/{proposal_id}/approve",
            headers=auth(admin_token), json={
                "reason": "Ablation aprobada; crear PR manual",
                "expected_version": 1,
                "idempotency_key": f"approve-{uuid.uuid4()}",
            },
        )
        assert response.status_code == 200, response.text
        artifact = response.json()["ready_artifact"]
        assert artifact["status"] == "ready_for_implementation"
        assert artifact["runtime_mutated"] is False
        assert artifact["attestation"]["algorithm"] == "Ed25519"
        assert "candidate_config" not in artifact
    finally:
        _cleanup(pattern_id)
