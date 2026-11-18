import asyncio

import quality_mutation
import auto_repair_policy
import auto_repair_contract
import targeted_consensus
import transcription_quality
import transcription_worker
from machine_evidence import finalize_machine_evidence, snapshot_hash, validate_machine_evidence


def test_mutation_gate_uses_persisted_tenant_for_zero_percent_pilot(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {
            "calibrated": True,
            "policy_version": quality_mutation.LEGACY_MUTATION_POLICY_VERSION,
        },
    )
    monkeypatch.setattr(quality_mutation, "_tenant_for_job", lambda _job: "pilot")

    def effective(*, job_id, tenant_id):
        seen.update(job_id=job_id, tenant_id=tenant_id)
        return "enforce" if tenant_id == "pilot" else "observe"

    monkeypatch.setattr(transcription_quality, "effective_policy_mode", effective)
    assert quality_mutation.mutation_authorized(job_id="job-1") is True
    assert seen == {"job_id": "job-1", "tenant_id": "pilot"}


def test_v6_calibration_never_authorizes_legacy_mutation(monkeypatch):
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {"calibrated": True, "policy_version": "lyrics-quality-v6"},
    )
    monkeypatch.setattr(
        transcription_quality, "effective_policy_mode",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("v6 must abstain before rollout evaluation")
        ),
    )
    monkeypatch.setattr(
        quality_mutation, "_tenant_for_job",
        lambda _job: (_ for _ in ()).throw(
            AssertionError("v6 must not resolve a mutation tenant")
        ),
    )
    assert quality_mutation.mutation_authorized(job_id="job-1") is False


def test_mutation_gate_fails_closed_without_signed_calibration(monkeypatch):
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {"calibrated": False},
    )
    monkeypatch.setattr(
        quality_mutation, "_tenant_for_job",
        lambda _job: (_ for _ in ()).throw(AssertionError("must not query tenant")),
    )
    assert quality_mutation.mutation_authorized(job_id="job-1") is False


def test_v6_inline_retry_never_adopts_legacy_mutated_segments(monkeypatch):
    original = [{"start": 0.0, "end": 1.0, "text": "ORIGINAL"}]
    windows = [{"id": "qw_0123456789abcdef", "start": 0.0, "end": 1.0}]

    def measured(result, *_args, **_kwargs):
        output = dict(result)
        output["postpass_stats"] = {
            **dict(output.get("postpass_stats") or {}),
            "quality_windows": windows,
            "coverage_final": {},
        }
        return output

    def evaluated(_segments, _metrics, **kwargs):
        return {
            "decision": "review_required", "score": 0, "reasons": [],
            "metrics": {}, "unsafe_windows": kwargs.get("unsafe_windows") or [],
            "retry": kwargs.get("retry_stats") or {},
        }

    monkeypatch.setattr(transcription_worker, "_medir_cobertura_final", measured)
    monkeypatch.setattr(transcription_quality, "evaluate", evaluated)
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {"calibrated": False},
    )
    monkeypatch.setattr(targeted_consensus, "is_enabled", lambda: True)
    monkeypatch.setattr(
        targeted_consensus, "reprocess",
        lambda *_args, **_kwargs: (
            {"segments": [{"start": 0.0, "end": 1.0, "text": "MUTATED"}]},
            {"attempted": True, "lines_replaced": 1, "lines_inserted": 0},
        ),
    )
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_INLINE_RETRY", "1")
    result = asyncio.run(transcription_worker._quality_gate_and_retry(
        {"segments": original}, "/tmp/not-read.wav", "job-v6", "es", None,
        lambda value, _job: value,
    ))
    assert result["segments"][0]["text"] == "ORIGINAL"
    retry = result["transcription_quality"]["retry"]
    assert retry["v6_legacy_mutation_blocked"] is True
    assert retry["lines_replaced"] == 0


def test_optional_auto_repair_provider_failure_keeps_original_quality(monkeypatch):
    original = [{"start": 0.0, "end": 1.0, "text": "ORIGINAL"}]
    windows = [{"id": "qw_0123456789abcdef", "start": 0.0, "end": 1.0}]

    def measured(result, *_args, **_kwargs):
        return {**result, "postpass_stats": {
            "quality_windows": windows, "coverage_final": {},
        }}

    def evaluated(_segments, _metrics, **kwargs):
        retry = kwargs.get("retry_stats") or {}
        return {
            "decision": "retry_failed" if retry.get("failed") else "review_required",
            "score": 0, "reasons": [], "metrics": {}, "retry": retry,
        }

    monkeypatch.setattr(transcription_worker, "_medir_cobertura_final", measured)
    monkeypatch.setattr(transcription_quality, "evaluate", evaluated)
    monkeypatch.setattr(
        transcription_quality, "calibration_identity", lambda: {"calibrated": False},
    )
    monkeypatch.setattr(auto_repair_policy, "runtime_authorization", lambda: {
        "status": "authorized",
        "actions": {"content_reversible": True, "timing_reversible": False},
        "authorization_sha256": "a" * 64,
    })
    monkeypatch.setattr(targeted_consensus, "is_enabled", lambda: True)
    monkeypatch.setattr(
        targeted_consensus, "reprocess",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_INLINE_RETRY", "0")
    result = asyncio.run(transcription_worker._quality_gate_and_retry(
        {"segments": original}, "/tmp/not-read.wav", "job-v6", "es", None,
        lambda value, _job: value,
    ))
    assert [
        (row["start"], row["end"], row["text"])
        for row in result["segments"]
    ] == [(0.0, 1.0, "ORIGINAL")]
    quality = result["transcription_quality"]
    assert quality["decision"] == "review_required"
    assert quality["retry"]["failed"] is False
    assert quality["auto_repair"]["status"] == "candidate_generation_failed"
    assert quality["auto_repair"]["applied_count"] == 0


def test_applied_auto_repair_captures_exact_private_undo_snapshot(monkeypatch):
    original = [{"start": 0.0, "end": 1.0, "text": "BEFORE"}]
    repaired = [{"start": 0.0, "end": 1.0, "text": "AFTER"}]
    windows = [{"id": "qw_0123456789abcdef", "start": 0.0, "end": 1.0}]

    def measured(result, *_args, **_kwargs):
        unsafe = windows if result["segments"][0]["text"] == "BEFORE" else []
        return {**result, "postpass_stats": {
            "quality_windows": unsafe, "coverage_final": {},
        }}

    def evaluated(segments, _metrics, **_kwargs):
        accepted = segments[0]["text"] == "AFTER"
        return {
            "decision": "pass" if accepted else "review_required",
            "score": 1 if accepted else 0, "reasons": [], "metrics": {},
        }

    monkeypatch.setattr(transcription_worker, "_medir_cobertura_final", measured)
    monkeypatch.setattr(transcription_quality, "evaluate", evaluated)
    monkeypatch.setattr(
        transcription_quality, "calibration_identity", lambda: {"calibrated": False},
    )
    monkeypatch.setattr(auto_repair_policy, "runtime_authorization", lambda: {
        "status": "authorized",
        "actions": {"content_reversible": True, "timing_reversible": False},
        "authorization_sha256": "a" * 64,
    })
    monkeypatch.setattr(
        auto_repair_policy, "candidate_has_independent_evidence",
        lambda _candidate, _action: True,
    )
    monkeypatch.setattr(targeted_consensus, "is_enabled", lambda: True)
    monkeypatch.setattr(targeted_consensus, "reprocess", lambda *_args, **_kwargs: (
        {"segments": original},
        {"attempted": True, "quality_proposal_windows": [{"action": "content_reversible"}]},
    ))
    monkeypatch.setattr(
        transcription_worker, "_bind_auto_repair_candidate",
        lambda candidate, _source: candidate,
    )
    monkeypatch.setattr(
        transcription_worker, "_auto_repair_candidate_action",
        lambda _candidate: "content_reversible",
    )
    monkeypatch.setattr(auto_repair_contract, "apply_local_candidates", lambda *_args: {
        "segments": repaired,
        "applied": [{"action": "content_reversible"}],
        "abstentions": [],
    })
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_INLINE_RETRY", "0")

    result = asyncio.run(transcription_worker._quality_gate_and_retry(
        {"segments": original}, "/tmp/not-read.wav", "job-undo", "es", None,
        lambda value, _job: value,
    ))
    assert result["segments"] == repaired
    assert "_pre_auto_repair_segments" not in result
    trace = result["transcription_quality"]["auto_repair"]
    assert trace["applied_count"] == 1
    captured = result["_machine_evidence"]
    source = next(
        item for item in captured["hypotheses_by_family"]
        if item["role"] == "pre_auto_repair"
    )
    assert source["events"][0]["text"] == "BEFORE"
    assert trace["source_snapshot_sha256"] == snapshot_hash(source["events"])
    finalized = finalize_machine_evidence(
        captured, original_segments=repaired,
        quality=result["transcription_quality"],
        audio_sha256="a" * 64, audio_revision=1,
    )
    validate_machine_evidence(finalized, repaired)
