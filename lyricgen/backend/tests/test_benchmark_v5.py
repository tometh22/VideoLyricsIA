"""Contract and metric tests for the fail-closed benchmark v5."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_attestation import sign_artifact
import transcription_quality as tq
from scripts import benchmark_v5_lib as v5
from scripts.export_shadow_ledger import _reviews, build_ledger
from quality_shadow import decision_id, decision_identity


PERICOS_GOLD = [
    {"start": 60.85, "end": 63.77, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 63.77, "end": 67.04, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 67.05, "end": 73.17, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 73.18, "end": 75.65, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 75.65, "end": 75.75, "text": "¡no!", "event_type": "vocalization"},
    {
        "start": 79.31,
        "end": 83.27,
        "text": "¡nooooooooo!",
        "event_type": "vocalization",
    },
]

PERICOS_BROKEN = [
    {"start": 60.93, "end": 63.70, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 67.09, "end": 69.80, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 73.27, "end": 75.60, "text": "Real, uoh uoh", "event_type": "mixed"},
    {"start": 79.37, "end": 83.10, "text": "Real, uoh uoh", "event_type": "mixed"},
]

_TEST_PRIVATE_RAW = hashlib.sha256(b"benchmark-v5-test-attestation").digest()
_TEST_PRIVATE = Ed25519PrivateKey.from_private_bytes(_TEST_PRIVATE_RAW)
_TEST_PRIVATE_B64 = base64.b64encode(_TEST_PRIVATE_RAW).decode("ascii")
_TEST_PUBLIC_B64 = base64.b64encode(_TEST_PRIVATE.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)).decode("ascii")
_TEST_KEY_ID = "benchmark-test-key"


def _signed(payload: dict[str, object]) -> dict[str, object]:
    return sign_artifact(payload, _TEST_PRIVATE_B64, _TEST_KEY_ID)


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _descriptor(root: Path, path: Path, **extra: object) -> dict[str, object]:
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        **extra,
    }


def _segments_with_offset(offset: float) -> list[dict[str, object]]:
    segments = deepcopy(PERICOS_GOLD)
    for segment in segments:
        segment["start"] = round(float(segment["start"]) + offset, 3)
        segment["end"] = round(float(segment["end"]) + offset, 3)
    return segments


def build_manifest(root: Path, *, second_case: bool = False) -> Path:
    trusted_keys = json.dumps({_TEST_KEY_ID: _TEST_PUBLIC_B64})
    os.environ["BENCHMARK_ANNOTATION_PUBLIC_KEYS"] = trusted_keys
    os.environ["BENCHMARK_REVIEW_PUBLIC_KEYS"] = trusted_keys
    os.environ["BENCHMARK_OPERATOR_EVIDENCE_PUBLIC_KEYS"] = trusted_keys
    os.environ["BENCHMARK_FINOPS_PUBLIC_KEYS"] = trusted_keys
    systems: dict[str, object] = {}
    pins: dict[str, tuple[str, str]] = {}
    for system in v5.SYSTEMS:
        config_path = root / "configs" / f"{system}.json"
        config_hash = _write_json(
            config_path,
            {
                "system": system,
                "temperature": 0,
                "pipeline_config_fingerprint": "a" * 16,
            },
        )
        release = f"{system}-release-abc123"
        systems[system] = {
            "release": release,
            "config": _descriptor(root, config_path),
            "render": False,
        }
        pins[system] = (release, config_hash)

    def build_entry(case_id: str, split: str, offset: float) -> dict[str, object]:
        case_dir = root / "cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        audio_path = case_dir / "audio.wav"
        audio_path.write_bytes(f"fake-wave-{case_id}".encode())
        audio_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()

        annotation_descriptors = []
        annotation_hashes = []
        for index, annotator in enumerate(("annotator-a", "annotator-b")):
            annotation_path = case_dir / f"annotation-{index + 1}.json"
            annotation_hash = _write_json(
                annotation_path,
                _signed({
                    "schema_version": 5,
                    "case_id": case_id,
                    "annotator_id": annotator,
                    "signer_id": annotator,
                    "source": "authenticated_annotation_service_v1",
                    "segments": _segments_with_offset(offset + index * 0.02),
                }),
            )
            annotation_hashes.append(annotation_hash)
            annotation_descriptors.append(
                _descriptor(root, annotation_path, annotator_id=annotator)
            )

        adjudication_path = case_dir / "adjudication.json"
        adjudication_hash = _write_json(
            adjudication_path,
            _signed({
                "schema_version": 5,
                "case_id": case_id,
                "adjudicator_id": "adjudicator-c",
                "signer_id": "adjudicator-c",
                "source": "authenticated_annotation_service_v1",
                "source_annotation_sha256": annotation_hashes,
                "segments": _segments_with_offset(offset),
            }),
        )
        gold_path = case_dir / "gold.json"
        _write_json(
            gold_path,
            _signed({
                "schema_version": 5,
                "case_id": case_id,
                "verified": True,
                "verified_by": "adjudicator-c",
                "signer_id": "adjudicator-c",
                "source": "authenticated_annotation_service_v1",
                "adjudication_sha256": adjudication_hash,
                "segments": _segments_with_offset(offset),
            }),
        )

        outputs = {}
        for system in v5.SYSTEMS:
            output_path = case_dir / f"{system}.json"
            if system == "current":
                segments = deepcopy(PERICOS_BROKEN)
                for segment in segments:
                    segment["start"] = round(float(segment["start"]) + offset, 3)
                    segment["end"] = round(float(segment["end"]) + offset, 3)
            else:
                segments = _segments_with_offset(offset)
            release, config_hash = pins[system]
            operational = {}
            if system == "candidate":
                operator_path = case_dir / "candidate-operator-evidence.json"
                _write_json(operator_path, _signed({
                    "schema": "server-editor-session-evidence-v1",
                    "case_id": case_id, "system": system,
                    "source": "server_product_events_v1",
                    "active_minutes": 4.0,
                    "event_ids": [f"event-{case_id}"],
                    "job_id": f"job-{case_id}", "revision": 3,
                    "snapshot_sha256": v5.lyric_snapshot_hash(segments),
                    "scored_segments_sha256": v5.lyric_snapshot_hash(
                        segments, include_event_type=True,
                    ),
                    "operator_id": "operator-1",
                    "pipeline_release": pins[system][0],
                    "config_sha256": pins[system][1],
                    "pipeline_config_fingerprint": "a" * 16,
                }))
                cost_path = case_dir / "candidate-cost-evidence.json"
                billing_source_path = case_dir / "openai-billing-receipts.json"
                billing_source_hash = _write_json(billing_source_path, {
                    "schema": "provider-billing-receipts-v1",
                    "currency": "USD", "billing_period": "2026-08",
                    "invoice_snapshot_id": f"invoice-{case_id}",
                    "receipts": [{
                        "provider": "openai", "sku": "whisper-1",
                        "request_id": f"request-{case_id}",
                        "units": 1.0, "unit_type": "audio_minute",
                        "currency": "USD", "cost_usd": 0.12,
                    }],
                })
                _write_json(cost_path, _signed({
                    "schema": "reconciled-cost-ledger-v1",
                    "case_id": case_id, "system": system,
                    "reconciled": True, "cost_complete": True,
                    "total_usd": 0.12,
                    "currency": "USD", "pricing_version": "2026-08-01",
                    "billing_period": "2026-08",
                    "invoice_snapshot_id": f"invoice-{case_id}",
                    "reconciliation_id": f"reconcile-{case_id}",
                    "reconciled_by": "finops-service",
                    "reconciled_at": "2026-08-10T12:00:00+00:00",
                    "release": pins[system][0],
                    "config_sha256": pins[system][1],
                    "audio_sha256": audio_hash,
                    "source_artifacts": [{
                        "source": "openai_usage_export",
                        "path": str(billing_source_path.relative_to(root)),
                        "sha256": billing_source_hash,
                    }],
                    "line_items": [{
                        "provider": "openai", "sku": "whisper-1",
                        "request_id": f"request-{case_id}",
                        "units": 1.0, "unit_type": "audio_minute",
                        "currency": "USD", "cost_usd": 0.12,
                        "source_receipt_sha256": billing_source_hash,
                    }],
                }))
                operational = {
                    "operator_evidence": _descriptor(root, operator_path),
                    "cost_evidence": _descriptor(root, cost_path),
                }
            _write_json(
                output_path,
                {
                    "schema_version": 5,
                    "case_id": case_id,
                    "system": system,
                    "release": release,
                    "config_sha256": config_hash,
                    "render": False,
                    "operator_review_minutes": 4.0 if system == "candidate" else None,
                    "cost_usd": 0.12 if system == "candidate" else None,
                    **operational,
                    "segments": segments,
                },
            )
            outputs[system] = _descriptor(root, output_path)

        return {
            "case_id": case_id,
            "split": split,
            "category": "live" if case_id == "pericos-live" else "studio",
            "tags": ["repetition", "adlib"] if case_id == "pericos-live" else [],
            "regression_fixture": (
                "los_pericos" if case_id == "pericos-live" else None
            ),
            "identity": {
                "artist": f"artist-{case_id}",
                "song": f"song-{case_id}",
                "master": f"master-{case_id}",
                "audio_sha256": audio_hash,
            },
            "audio": _descriptor(root, audio_path),
            "annotations": annotation_descriptors,
            "adjudication": _descriptor(
                root,
                adjudication_path,
                adjudicator_id="adjudicator-c",
            ),
            "gold": _descriptor(root, gold_path, verified=True),
            "outputs": outputs,
        }

    entries = [build_entry("pericos-live", "dev", 0.0)]
    if second_case:
        entries.append(build_entry("other-holdout", "holdout", 100.0))
    manifest_path = root / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 5,
            "benchmark_id": "quality-v5-fixture",
            "systems": systems,
            "entries": entries,
        },
    )
    return manifest_path


def _read_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite_manifest(path: Path, manifest: dict[str, object]) -> None:
    _write_json(path, manifest)


def test_valid_manifest_is_accepted_and_pericos_has_exactly_six_gold_events(tmp_path):
    manifest_path = build_manifest(tmp_path)
    assert v5.validate_manifest(manifest_path) == []
    manifest = _read_manifest(manifest_path)
    gold_path = tmp_path / manifest["entries"][0]["gold"]["path"]
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    assert len(gold["segments"]) == 6
    assert [segment["start"] for segment in gold["segments"]] == [
        60.85,
        63.77,
        67.05,
        73.18,
        75.65,
        79.31,
    ]
    assert gold["segments"][-1]["text"].startswith("¡no")


def test_manifest_rejects_any_artifact_hash_drift(tmp_path):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    output_path = tmp_path / manifest["entries"][0]["outputs"]["candidate"]["path"]
    output_path.write_text("{}\n", encoding="utf-8")
    errors = v5.validate_manifest(manifest_path)
    assert any("hash mismatch" in error and "candidate" in error for error in errors)


@pytest.mark.parametrize("failure", ["duplicate_annotator", "adjudicator_is_annotator"])
def test_manifest_requires_two_distinct_annotators_and_third_adjudicator(tmp_path, failure):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    entry = manifest["entries"][0]
    if failure == "duplicate_annotator":
        entry["annotations"][1]["annotator_id"] = " Annotator-A "
    else:
        entry["adjudication"]["adjudicator_id"] = "annotator-a"
    _rewrite_manifest(manifest_path, manifest)
    errors = v5.validate_manifest(manifest_path)
    expected = "annotators must be distinct" if failure == "duplicate_annotator" else "adjudicator must differ"
    assert any(expected in error for error in errors)


def test_manifest_requires_verified_gold_bound_to_adjudication(tmp_path):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    manifest["entries"][0]["gold"]["verified"] = False
    _rewrite_manifest(manifest_path, manifest)
    assert any("gold.verified must be exactly true" in error for error in v5.validate_manifest(manifest_path))


@pytest.mark.parametrize("identity_field", ["artist", "song", "master", "audio_sha256"])
def test_manifest_rejects_dev_holdout_identity_leakage(tmp_path, identity_field):
    manifest_path = build_manifest(tmp_path, second_case=True)
    manifest = _read_manifest(manifest_path)
    first, second = manifest["entries"]
    second["identity"][identity_field] = first["identity"][identity_field]
    if identity_field == "audio_sha256":
        second["audio"]["sha256"] = first["audio"]["sha256"]
        second["audio"]["path"] = first["audio"]["path"]
    _rewrite_manifest(manifest_path, manifest)
    errors = v5.validate_manifest(manifest_path)
    assert any(f"{identity_field} leakage" in error for error in errors)


@pytest.mark.parametrize("failure", ["missing_rotor", "release", "config", "render"])
def test_outputs_are_complete_pinned_consistently_and_never_render(tmp_path, failure):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    entry = manifest["entries"][0]
    if failure == "missing_rotor":
        del entry["outputs"]["rotor"]
    else:
        output_descriptor = entry["outputs"]["candidate"]
        output_path = tmp_path / output_descriptor["path"]
        output = json.loads(output_path.read_text(encoding="utf-8"))
        if failure == "release":
            output["release"] = "different-release"
        elif failure == "config":
            output["config_sha256"] = "0" * 64
        else:
            output["render"] = True
        output_descriptor["sha256"] = _write_json(output_path, output)
    _rewrite_manifest(manifest_path, manifest)
    errors = v5.validate_manifest(manifest_path)
    assert errors
    if failure == "missing_rotor":
        assert any("outputs must contain exactly" in error for error in errors)
    elif failure == "render":
        assert any("render must be exactly false" in error for error in errors)
    else:
        assert any("release/config does not match system pin" in error for error in errors)


def test_cli_validate_and_score_fail_closed(tmp_path):
    manifest_path = build_manifest(tmp_path)
    script = Path(__file__).parent.parent / "scripts" / "benchmark_v5.py"
    valid = subprocess.run(
        [sys.executable, str(script), "validate", "--manifest", str(manifest_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0
    assert "Benchmark v5 valid" in valid.stdout

    report_path = tmp_path / "report.json"
    scored = subprocess.run(
        [
            sys.executable,
            str(script),
            "score",
            "--manifest",
            str(manifest_path),
            "--output",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert scored.returncode == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["schema_version"] == 5

    manifest = _read_manifest(manifest_path)
    manifest["entries"][0]["gold"]["sha256"] = "0" * 64
    _rewrite_manifest(manifest_path, manifest)
    rejected = subprocess.run(
        [sys.executable, str(script), "score", "--manifest", str(manifest_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "fail-closed" in rejected.stderr


def test_score_reports_pericos_regression_and_perfect_candidate(tmp_path):
    manifest_path = build_manifest(tmp_path)
    report = v5.score_manifest(manifest_path)
    current = report["systems"]["current"]["all"]
    candidate = report["systems"]["candidate"]["all"]
    rotor = report["systems"]["rotor"]["all"]

    assert current["event_count"]["gold"] == 6
    assert current["event_count"]["predicted"] == 4
    assert current["event_count"]["absolute_error"] == 2
    assert current["event_count"]["recall"] < 1
    assert current["wer"] > 0
    assert current["vocalization"]["recall"] < 1
    assert candidate["wer"] == 0
    assert candidate["cer"] == 0
    assert candidate["alignment"]["monotonic"] is True
    assert candidate["alignment"]["f1"] == 1
    assert candidate["event_count"]["absolute_error"] == 0
    assert candidate["event_count"]["f1"] == 1
    assert candidate["vocalization"]["precision"] == 1
    assert candidate["vocalization"]["recall"] == 1
    assert candidate["boundaries"]["onset_mae_s"] == 0
    assert candidate["boundaries"]["onset_p90_s"] == 0
    assert candidate["boundaries"]["tolerances"]["100ms"]["both_recall"] == 1
    assert rotor["wer"] == 0
    assert report["release_gate"]["decision"] == "NO_GO"
    assert set(report["release_gate"]["checks"]) == set(
        tq.RELEASE_REPORT_REQUIRED_CHECKS
    )
    assert report["release_gate"]["checks"]["pericos_six_events"] is True
    assert "corpus_50" in report["release_gate"]["blockers"]


def test_cli_gate_refuses_a_fixture_that_lacks_release_evidence(tmp_path):
    manifest_path = build_manifest(tmp_path)
    script = Path(__file__).parent.parent / "scripts" / "benchmark_v5.py"
    gated = subprocess.run(
        [sys.executable, str(script), "gate", "--manifest", str(manifest_path)],
        check=False, capture_output=True, text=True,
    )
    assert gated.returncode == 2
    report = json.loads(gated.stdout)
    assert report["release_gate"]["decision"] == "NO_GO"
    assert "shadow_volume_and_duration" in report["release_gate"]["blockers"]


def test_metrics_pin_wer_cer_monotonic_alignment_vocalization_and_boundaries():
    ground = [
        {"start": 1.0, "end": 2.0, "text": "hola mundo", "event_type": "lexical"},
        {"start": 3.0, "end": 4.0, "text": "uoh", "event_type": "vocalization"},
    ]
    hypothesis = [
        {"start": 1.05, "end": 2.15, "text": "hola", "event_type": "lexical"},
        {"start": 3.25, "end": 4.05, "text": "uoh", "event_type": "vocalization"},
    ]
    metrics = v5.score_segments(ground, hypothesis)
    assert metrics["wer"] == pytest.approx(1 / 3)
    assert metrics["cer"] == pytest.approx(5 / 12)
    assert metrics["alignment"]["pairs"] == [(0, 0), (1, 1)]
    assert metrics["vocalization"]["f1"] == 1
    assert metrics["boundaries"]["onset_mae_s"] == pytest.approx(0.15)
    assert metrics["boundaries"]["onset_p90_s"] == pytest.approx(0.25)
    assert metrics["boundaries"]["end_mae_s"] == pytest.approx(0.1)
    assert metrics["boundaries"]["tolerances"]["100ms"]["onset_recall"] == 0.5
    assert metrics["boundaries"]["tolerances"]["200ms"]["both_recall"] == 0.5


def test_operator_percentiles_and_cost_coverage_are_explicit(tmp_path):
    manifest_path = build_manifest(tmp_path, second_case=True)
    report = v5.score_manifest(manifest_path)
    candidate = report["systems"]["candidate"]["all"]
    current = report["systems"]["current"]["all"]
    assert candidate["operator"] == {
        "coverage_count": 2,
        "coverage": 1.0,
        "p50_minutes": 4.0,
        "p90_minutes": 4.0,
    }
    assert candidate["cost"] == {
        "coverage_count": 2,
        "coverage": 1.0,
        "total_usd": pytest.approx(0.24),
        "mean_usd": pytest.approx(0.12),
    }
    assert current["operator"]["coverage"] == 0
    assert current["cost"]["coverage"] == 0


@pytest.mark.parametrize("field", ["operator_evidence", "cost_evidence"])
def test_operational_metrics_require_hashed_server_evidence(tmp_path, field):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    descriptor = manifest["entries"][0]["outputs"]["candidate"]
    output_path = tmp_path / descriptor["path"]
    output = json.loads(output_path.read_text(encoding="utf-8"))
    del output[field]
    descriptor["sha256"] = _write_json(output_path, output)
    _rewrite_manifest(manifest_path, manifest)
    errors = v5.validate_manifest(manifest_path)
    assert any(field in error for error in errors)


def test_shadow_gate_rejects_impossible_counts_and_stale_candidate_binding(tmp_path):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    candidate = manifest["systems"]["candidate"]
    manifest["shadow_evaluation"] = {
        "eligible_decisions": 400,
        "approved_decisions": 800,
        "correct_approvals": 1600,
        "catastrophic_approvals": 0,
        "duration_days": 31,
        "candidate_release": "stale-release",
        "candidate_config_sha256": candidate["config"]["sha256"],
    }
    _rewrite_manifest(manifest_path, manifest)
    errors = v5.validate_manifest(manifest_path)
    assert any("shadow_evaluation.ledger" in error for error in errors)


def test_attested_shadow_ledger_is_derived_from_rows_not_scalar_counters(
    tmp_path, monkeypatch,
):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    candidate = manifest["systems"]["candidate"]
    key = _TEST_PRIVATE_B64
    key_id = "benchmark-test-key"
    monkeypatch.setenv("BENCHMARK_SHADOW_PUBLIC_KEYS", json.dumps({
        key_id: _TEST_PUBLIC_B64,
    }))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(500):
        approved = index < 300
        rows.append({
            "decision_id": hashlib.sha256(
                f"decision-{index:04d}".encode()
            ).hexdigest(),
            "occurred_at": (
                started + timedelta(days=31 * index / 499)
            ).isoformat(),
            "eligible": True,
            "would_approve": approved,
            "reviewed": approved,
            "correct": True if approved else None,
            "catastrophic": False if approved else None,
            "candidate_release": candidate["release"],
            "candidate_config_sha256": candidate["config"]["sha256"],
            "pipeline_config_fingerprint": "a" * 16,
        })
    ledger = {
        "schema_version": 5,
        "candidate_release": candidate["release"],
        "candidate_config_sha256": candidate["config"]["sha256"],
        "pipeline_config_fingerprint": "a" * 16,
        "decisions": rows,
    }
    ledger["attestation"] = v5.shadow_ledger_attestation(ledger, key, key_id)
    ledger_path = tmp_path / "shadow-ledger.json"
    _write_json(ledger_path, ledger)
    manifest["shadow_evaluation"] = {
        "ledger": _descriptor(tmp_path, ledger_path),
        # These deliberately false scalars must have no effect.
        "eligible_decisions": 1,
        "approved_decisions": 0,
    }
    _rewrite_manifest(manifest_path, manifest)

    gate = v5.score_manifest(manifest_path)["release_gate"]
    assert gate["checks"]["shadow_ledger_attested"] is True
    assert gate["checks"]["shadow_counts_consistent"] is True
    assert gate["checks"]["shadow_bound_to_candidate"] is True
    assert gate["checks"]["automatic_precision"] is True
    assert gate["checks"]["automatic_coverage"] is True
    assert gate["checks"]["shadow_volume_and_duration"] is True
    assert gate["observed"]["shadow_decisions"] == 500
    assert gate["observed"]["reviewed_approvals"] == 300


def test_shadow_attestation_rejects_any_row_tampering(tmp_path, monkeypatch):
    manifest_path = build_manifest(tmp_path)
    manifest = _read_manifest(manifest_path)
    candidate = manifest["systems"]["candidate"]
    monkeypatch.setenv("BENCHMARK_SHADOW_PUBLIC_KEYS", json.dumps({
        "key-1": _TEST_PUBLIC_B64,
    }))
    ledger = {
        "schema_version": 5,
        "candidate_release": candidate["release"],
        "candidate_config_sha256": candidate["config"]["sha256"],
        "pipeline_config_fingerprint": "a" * 16,
        "decisions": [{
            "decision_id": "1" * 64,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "eligible": True,
            "would_approve": True,
            "reviewed": True,
            "correct": True,
            "catastrophic": False,
            "candidate_release": candidate["release"],
            "candidate_config_sha256": candidate["config"]["sha256"],
            "pipeline_config_fingerprint": "a" * 16,
        }],
    }
    ledger["attestation"] = v5.shadow_ledger_attestation(
        ledger, _TEST_PRIVATE_B64, "key-1",
    )
    ledger["decisions"][0]["correct"] = False
    ledger_path = tmp_path / "shadow-ledger.json"
    _write_json(ledger_path, ledger)
    manifest["shadow_evaluation"] = {"ledger": _descriptor(tmp_path, ledger_path)}
    _rewrite_manifest(manifest_path, manifest)

    gate = v5.score_manifest(manifest_path)["release_gate"]
    assert gate["checks"]["shadow_ledger_attested"] is False
    assert gate["decision"] == "NO_GO"


def test_shadow_exporter_filters_release_and_joins_reviews_without_lyrics():
    identity = decision_identity(
        "job123", 1, "a" * 64, "candidate-release", "e" * 16,
    )
    canonical_id = decision_id(identity)
    properties = {
        **identity,
        "decision_id": canonical_id,
        "pipeline_release": "candidate-release",
        "pipeline_config_fingerprint": "e" * 16,
        "evaluation_stage": "terminal",
        "eligible": True,
        "would_approve": True,
        "secret_lyric_that_must_not_export": "Real uoh uoh",
    }
    rows = [SimpleNamespace(
        properties=properties,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=None,
    )]
    ledger = build_ledger(
        rows,
        {canonical_id: {
            "correct": True, "catastrophic": False,
            "reviewer_id": "reviewer-1",
            "snapshot_sha256": "a" * 64,
            "reviewed_at": "2026-01-02T00:00:00+00:00",
        }},
        candidate_release="candidate-release",
        candidate_config_sha256="f" * 64,
        pipeline_config_fingerprint="e" * 16,
    )
    assert len(ledger["decisions"]) == 1
    assert ledger["decisions"][0]["reviewed"] is True
    assert "Real" not in json.dumps(ledger)


def test_shadow_exporter_rejects_matching_terminal_with_tampered_id():
    identity = decision_identity(
        "job123", 1, "a" * 64, "candidate-release", "e" * 16,
    )
    properties = {
        **identity, "decision_id": "f" * 64,
        "evaluation_stage": "terminal", "eligible": True,
        "would_approve": False,
    }
    row = SimpleNamespace(
        properties=properties,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=None,
    )
    with pytest.raises(ValueError, match="canonical ID"):
        build_ledger(
            [row], {}, candidate_release="candidate-release",
            candidate_config_sha256="f" * 64,
            pipeline_config_fingerprint="e" * 16,
        )


def test_shadow_exporter_rejects_review_for_different_snapshot():
    identity = decision_identity(
        "job123", 1, "a" * 64, "candidate-release", "e" * 16,
    )
    canonical_id = decision_id(identity)
    row = SimpleNamespace(
        properties={
            **identity, "decision_id": canonical_id,
            "evaluation_stage": "terminal", "eligible": True,
            "would_approve": True,
        },
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=None,
    )
    with pytest.raises(ValueError, match="snapshot mismatch"):
        build_ledger(
            [row], {canonical_id: {
                "correct": True, "catastrophic": False,
                "reviewer_id": "reviewer-1", "snapshot_sha256": "f" * 64,
                "reviewed_at": "2026-01-02T00:00:00+00:00",
            }}, candidate_release="candidate-release",
            candidate_config_sha256="f" * 64,
            pipeline_config_fingerprint="e" * 16,
        )


def test_review_import_requires_authenticated_snapshot_receipts(tmp_path):
    build_manifest(tmp_path)
    review_path = tmp_path / "reviews.json"
    payload = {
        "schema": "authenticated-shadow-reviews-v1",
        "source": "authenticated_review_service_v1",
        "reviews": [{
            "decision_id": "d" * 64, "correct": True,
            "catastrophic": False, "reviewer_id": "reviewer-1",
            "snapshot_sha256": "e" * 64,
            "reviewed_at": "2026-08-01T12:00:00+00:00",
            "review_receipt_id": "receipt-1",
        }],
    }
    _write_json(review_path, payload)
    with pytest.raises(ValueError, match="not authenticated"):
        _reviews(review_path)
    _write_json(review_path, _signed(payload))
    reviews, source_hash = _reviews(review_path)
    assert reviews["d" * 64]["reviewer_id"] == "reviewer-1"
    assert len(source_hash) == 64
