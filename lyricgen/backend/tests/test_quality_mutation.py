import quality_mutation
import transcription_quality


def test_mutation_gate_uses_persisted_tenant_for_zero_percent_pilot(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        transcription_quality, "calibration_identity",
        lambda: {"calibrated": True},
    )
    monkeypatch.setattr(quality_mutation, "_tenant_for_job", lambda _job: "pilot")

    def effective(*, job_id, tenant_id):
        seen.update(job_id=job_id, tenant_id=tenant_id)
        return "enforce" if tenant_id == "pilot" else "observe"

    monkeypatch.setattr(transcription_quality, "effective_policy_mode", effective)
    assert quality_mutation.mutation_authorized(job_id="job-1") is True
    assert seen == {"job_id": "job-1", "tenant_id": "pilot"}


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
