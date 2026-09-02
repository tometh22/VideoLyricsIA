from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_attestation import lyric_snapshot_hash, sign_artifact, verify_artifact
from scripts.export_cost_evidence import build_cost_evidence
from scripts.export_operator_evidence import build_operator_evidence


def _keys(monkeypatch, env_name="TEST_EVIDENCE_PUBLIC_KEYS"):
    raw = hashlib.sha256(b"operational-evidence-test-key").digest()
    private = Ed25519PrivateKey.from_private_bytes(raw)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(env_name, json.dumps({
        "test": base64.b64encode(public).decode("ascii"),
    }))
    return base64.b64encode(raw).decode("ascii")


def test_ed25519_receipt_rejects_manual_tampering(monkeypatch):
    private = _keys(monkeypatch)
    signed = sign_artifact({"value": 1}, private, "test")
    assert verify_artifact(signed, "TEST_EVIDENCE_PUBLIC_KEYS")[0] is True
    signed["value"] = 2
    assert verify_artifact(signed, "TEST_EVIDENCE_PUBLIC_KEYS")[0] is False


def test_scored_snapshot_hash_includes_event_type():
    lexical = [{"start": 1, "end": 2, "text": "uoh", "event_type": "lexical"}]
    vocal = [{**lexical[0], "event_type": "vocalization"}]
    assert lyric_snapshot_hash(lexical) == lyric_snapshot_hash(vocal)
    assert lyric_snapshot_hash(
        lexical, include_event_type=True,
    ) != lyric_snapshot_hash(vocal, include_event_type=True)


def test_operator_minutes_come_only_from_contiguous_server_timestamps():
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    common = {
        "revision": 4, "snapshot_sha256": "a" * 64,
        "pipeline_release": "release-1",
        "pipeline_config_fingerprint": "b" * 16,
        "session_id": "session-123456789",
    }
    rows = [SimpleNamespace(
        id=index + 1, name="editor_activity_heartbeat", job_id="job123",
        user_id=7, created_at=start + timedelta(seconds=offset),
        properties={**common, "activity_seq": index + 1},
    ) for index, offset in enumerate((0, 15, 30, 90))]
    evidence = build_operator_evidence(
        rows, case_id="case-1", system="candidate", job_id="job123",
        revision=4, snapshot_sha256="a" * 64,
        pipeline_release="release-1", config_sha256="c" * 64,
        pipeline_config_fingerprint="b" * 16,
        scored_segments_sha256="d" * 64,
    )
    assert evidence["active_minutes"] == 0.5
    assert evidence["operator_id"] == "7"
    assert evidence["event_ids"] == ["1", "2", "3", "4"]


def test_operator_session_crosses_autosave_revisions_without_losing_time():
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    for index, (revision, snapshot) in enumerate((
        (1, "1" * 64), (1, "1" * 64),
        (2, "2" * 64), (2, "2" * 64),
    )):
        rows.append(SimpleNamespace(
            id=index + 1, name="editor_activity_heartbeat", job_id="job123",
            user_id=7, created_at=start + timedelta(seconds=index * 15),
            properties={
                "revision": revision, "snapshot_sha256": snapshot,
                "pipeline_release": "release-1",
                "pipeline_config_fingerprint": "b" * 16,
                "session_id": "session-123456789", "activity_seq": index + 1,
            },
        ))
    evidence = build_operator_evidence(
        rows, case_id="case-1", system="candidate", job_id="job123",
        revision=2, snapshot_sha256="2" * 64,
        pipeline_release="release-1", config_sha256="c" * 64,
        pipeline_config_fingerprint="b" * 16,
        scored_segments_sha256="d" * 64,
    )
    assert evidence["active_minutes"] == 0.75
    assert [item["revision"] for item in evidence["revision_transitions"]] == [1, 1, 2, 2]


def test_finops_export_binds_receipts_and_rejects_duplicate_requests(tmp_path):
    source_path = tmp_path / "openai-receipts.json"
    source_path.write_text(json.dumps({
        "schema": "provider-billing-receipts-v1", "currency": "USD",
        "billing_period": "2026-08", "invoice_snapshot_id": "invoice-1",
        "receipts": [{
            "provider": "openai", "request_id": "req-1", "sku": "whisper-1",
            "units": 1, "unit_type": "audio_minute",
            "currency": "USD", "cost_usd": 0.2,
        }],
    }), encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    payload = {
        "schema": "finops-reconciliation-input-v1",
        "case_id": "case-1", "system": "candidate",
        "release": "release-1", "config_sha256": "a" * 64,
        "audio_sha256": "b" * 64, "pricing_version": "2026-08-01",
        "billing_period": "2026-08", "invoice_snapshot_id": "invoice-1",
        "reconciliation_id": "reconcile-1", "reconciled_by": "finops-1",
        "reconciled_at": "2026-08-10T12:00:00+00:00", "total_usd": 0.2,
        "source_artifacts": [{
            "source": "provider", "path": source_path.name,
            "sha256": source_hash,
        }],
        "line_items": [{
            "provider": "openai", "sku": "whisper-1", "request_id": "req-1",
            "units": 1, "unit_type": "audio_minute", "currency": "USD",
            "cost_usd": 0.2, "source_receipt_sha256": source_hash,
        }],
    }
    evidence = build_cost_evidence(payload, source_root=tmp_path)
    assert evidence["cost_complete"] is True
    invalid_date = dict(payload, reconciled_at="not-a-date")
    try:
        build_cost_evidence(invalid_date, source_root=tmp_path)
    except ValueError as exc:
        assert "reconciled_at" in str(exc)
    else:  # pragma: no cover - regression assertion
        raise AssertionError("invalid reconciliation timestamp was accepted")
    payload["line_items"].append(dict(payload["line_items"][0], cost_usd=0.0))
    try:
        build_cost_evidence(payload, source_root=tmp_path)
    except ValueError as exc:
        assert "unique" in str(exc)
    else:  # pragma: no cover - regression assertion
        raise AssertionError("duplicate provider request was accepted")
