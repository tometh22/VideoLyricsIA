from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from reviewer_candidate import build_candidate
from reviewer_candidate_registry import (
    candidate_for_editor, prepare_registry_record, register_candidate,
)
from tests.test_reviewer_batch_bridge import fixture as review_fixture


@pytest.mark.parametrize("existing", [[], ["*"], ["*", "*"], ['"other-etag"']])
def test_create_only_header_has_identical_signed_and_sent_value(existing):
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4Auth
    from botocore.credentials import Credentials
    from reviewer_candidate_registry import _create_only_header
    request = AWSRequest(method="PUT", url="https://example.invalid/bucket/candidate", data=b"{}")
    for value in existing:
        request.headers["if-none-match"] = value
    # A modeled IfNoneMatch or a re-sign must not duplicate the conditional.
    _create_only_header(request)
    _create_only_header(request)
    assert request.headers.get_all("If-None-Match") == ["*"]
    signer = SigV4Auth(Credentials("test", "test"), "s3", "auto")
    canonical = signer.canonical_request(request)
    assert "\nif-none-match:*\n" in canonical
    assert "if-none-match:*,*" not in canonical
    prepared = request.prepare()
    assert prepared.headers["If-None-Match"] == "*"

def setup_registry(monkeypatch, tmp_path, *, no_changes=False):
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_ASSIST_CAMPAIGN_ID", "fixture00001")
    monkeypatch.setenv("REVIEWER_ASSIST_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("REVIEWER_CANDIDATE_STORAGE", "local")
    song, candidate, review = review_fixture()
    song["campaign_id"] = "fixture00001"
    if no_changes:
        candidate = build_candidate(song)
    job = SimpleNamespace(job_id=song["job_id"], tenant_id="tenant", campaign_id="fixture00001",
        audio_revision=song["audio_revision"], input_audio_sha256=song["audio_sha256"], status="ready")
    document = SimpleNamespace(job_id=song["job_id"], tenant_id="tenant",
        revision=song["segments_revision"], current_segments=deepcopy(song["segments"]))
    return song, candidate, review, job, document


@pytest.mark.parametrize("no_changes", [False, True])
def test_complete_candidate_with_or_without_edits_is_associated(monkeypatch, tmp_path, no_changes):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path, no_changes=no_changes)
    before = deepcopy(document.current_segments)
    result = register_candidate("tenant", song, candidate, review)
    assert result["created"] is True
    payload = candidate_for_editor(job, document)
    assert payload["segments"] == candidate["segments"]
    assert payload["review_complete"] is True
    assert payload["correctness_certified"] is False
    assert payload["approved"] is False
    assert payload["read_only"] is True
    assert document.current_segments == before
    assert register_candidate("tenant", song, candidate, review)["created"] is False


@pytest.mark.parametrize("mutation", [
    lambda j,d: setattr(j, "audio_revision", 2),
    lambda j,d: setattr(j, "input_audio_sha256", "b" * 64),
    lambda j,d: setattr(d, "revision", 2),
    lambda j,d: d.current_segments[0].update(end=4.5),
    lambda j,d: setattr(d, "tenant_id", "other"),
    lambda j,d: setattr(j, "job_id", "other"),
])
def test_live_identity_mismatch_never_serves_candidate(monkeypatch, tmp_path, mutation):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    mutation(job, document)
    assert candidate_for_editor(job, document) is None


def test_approved_song_stays_read_only(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    job.status = "lyrics_approved"
    payload = candidate_for_editor(job, document)
    assert payload["read_only"] and payload["current_song_approved"]
    assert job.status == "lyrics_approved"


def test_expired_record_not_served(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    created = datetime(2026, 9, 6, tzinfo=timezone.utc)
    register_candidate("tenant", song, candidate, review, now=created)
    assert candidate_for_editor(job, document, now=created + timedelta(days=8)) is None


def test_conflicting_candidate_cannot_overwrite_same_source(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    with pytest.raises(ValueError, match="immutable_candidate_conflict"):
        register_candidate("tenant", song, build_candidate(song), review)
    assert candidate_for_editor(job, document)["segments"] == candidate["segments"]


def test_tampering_fails_closed(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    path = next((tmp_path / "complete_candidates").glob("*.json"))
    data = json.loads(path.read_text())
    data["payload"]["segments"][0]["text"] = "tampered"
    path.write_text(json.dumps(data))
    assert candidate_for_editor(job, document) is None


def test_off_switch_never_reads_or_writes(monkeypatch, tmp_path):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    monkeypatch.setenv("REVIEWER_ASSIST_CACHE_DIR", str(tmp_path))
    assert register_candidate(None, {}, {}, {})["reason"] == "reviewer_assist_disabled"
    assert candidate_for_editor(None, None) is None
    assert list(tmp_path.iterdir()) == []


def test_preparation_possible_with_flag_off(monkeypatch):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    record = prepare_registry_record("tenant", *review_fixture())
    assert record["payload"]["review_complete"] is True


def test_localized_reconciliation_doubts_survive_safe_rebuild():
    song, candidate, review = review_fixture()
    review["localized_doubts"] = [{"line_index": 0, "start": 2., "end": 4.,
        "reason": "uncertain_word_occurrence"}]
    review["private_path"] = "/not/for/client"
    record = prepare_registry_record("tenant", song, candidate, review)
    assert record["payload"]["review_details"]["localized_doubts"] == review["localized_doubts"]
    assert "private_path" not in record["payload"]["review_details"]


def test_interrupted_serialization_never_publishes_partial_record(monkeypatch, tmp_path):
    song, candidate, review, _, _ = setup_registry(monkeypatch, tmp_path)
    def fail_dump(value, output, **kwargs):
        output.write("partial")
        raise OSError("interrupted")
    monkeypatch.setattr("reviewer_candidate_registry.json.dump", fail_dump)
    with pytest.raises(OSError, match="interrupted"):
        register_candidate("tenant", song, candidate, review)
    assert list((tmp_path / "complete_candidates").iterdir()) == []


def test_held_evidence_and_private_diagnostics_not_sent_to_editor():
    song, candidate, review = review_fixture()
    review["held_decisions"] = [{"reason": "ambiguous", "decision": {
        "proposal_id": "p", "window": {"line_index": 0},
        "evidence": [{"private_path": "/secret", "text": "raw provider response"}]}}]
    review["line_diagnostics"] = [{"line_index": 0, "phrase_status": "unresolved",
        "private_path": "/secret", "exact": {"raw": "large alignment"}}]
    record = prepare_registry_record("tenant", song, candidate, review)
    details = record["payload"]["review_details"]
    assert details["held_decisions"] == [{"reason": "ambiguous", "proposal_id": "p", "line_index": 0}]
    assert details["line_diagnostics"] == [{"line_index": 0, "phrase_status": "unresolved"}]


def test_editor_get_fetches_only_after_existing_authorization(monkeypatch):
    import asyncio
    # This is a local route-wiring fixture, not a production config fixture.
    # Do not depend on another test having imported main under development.
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "1")
    import main
    calls = []
    job = SimpleNamespace(transcription_quality=None, segments_json=[], artist="Artist",
        song_title="Song", filename="audio.wav", status="ready", job_id="song",
        tenant_id="tenant", audio_revision=1, input_audio_sha256="a" * 64)
    document = SimpleNamespace(job_id="song", tenant_id="tenant", revision=1,
        current_segments=[{"text": "Canto", "start": 0., "end": 2.}])
    def authorize(db, job_id, user):
        calls.append("authorized")
        return job, document
    def fetch(actual_job, actual_document):
        assert calls == ["authorized", "committed"]
        assert actual_job is not job and actual_document is not document
        assert actual_job.audio_revision == 1
        assert actual_document.current_segments[0]["text"] == "Canto"
        calls.append("registry")
        return {"id": "candidate", "read_only": True}
    def commit():
        calls.append("committed")
        job.audio_revision = 2
        document.current_segments[0]["text"] = "Later revision"
    monkeypatch.setattr(main, "_editor_document_or_404", authorize)
    monkeypatch.setattr(main, "_audit_cross_tenant_access", lambda *a, **k: None)
    monkeypatch.setattr(main, "revoke_quality_proposal_if_disabled", lambda *a: None)
    monkeypatch.setattr(main, "serialize_document", lambda *a: {})
    monkeypatch.setattr("reviewer_candidate_registry.candidate_for_editor", fetch)
    result = asyncio.run(main.get_editor_document("song", {"id": 1}, SimpleNamespace(commit=commit)))
    assert calls == ["authorized", "committed", "registry"]
    assert result["reviewer_candidate"]["read_only"] is True
    # With the rollout off there is no registry/threadpool await while locked.
    monkeypatch.setenv("REVIEWER_ASSIST_ENABLED", "0")
    result = asyncio.run(main.get_editor_document("song", {"id": 1}, SimpleNamespace(commit=commit)))
    assert result["reviewer_candidate"] is None
    assert calls == ["authorized", "committed", "registry", "authorized", "committed"]
    def deny(*args):
        raise main.HTTPException(status_code=404)
    monkeypatch.setattr(main, "_editor_document_or_404", deny)
    with pytest.raises(main.HTTPException):
        asyncio.run(main.get_editor_document("song", {"id": 1}, None))
    assert calls == ["authorized", "committed", "registry", "authorized", "committed"]


def test_registry_native_proposal_not_claimed_as_candidate_adoption(monkeypatch, tmp_path):
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    native = {"status": "pending", "windows": [{"proposed_segments": [{"text": "Different edit"}]}]}
    document.quality_proposal = deepcopy(native)
    result = candidate_for_editor(job, document)
    assert result["adoption_status"] == "existing_different_proposal_preserved"
    assert document.quality_proposal == native


def test_adoption_requires_matching_source_and_actual_window_edits(monkeypatch, tmp_path):
    from reviewer_batch_bridge import prepare_batch_candidate
    song, candidate, review, job, document = setup_registry(monkeypatch, tmp_path)
    register_candidate("tenant", song, candidate, review)
    proposal = prepare_batch_candidate(song, candidate, review)["proposal"]
    proposal.update(status="pending", expires_at="2099-01-01T00:00:00+00:00",
        base_revision=song["segments_revision"], audio_revision=song["audio_revision"],
        audio_sha256=song["audio_sha256"])
    document.quality_proposal = proposal
    monkeypatch.setenv("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "1")
    assert candidate_for_editor(job, document)["adoption_status"] == "matching_existing_proposal"
    proposal["windows"][0]["proposed_segments"][0]["text"] = "Different edit"
    assert candidate_for_editor(job, document)["adoption_status"] == "existing_different_proposal_preserved"


def r2_fixture(monkeypatch, tmp_path):
    import boto3
    from botocore.stub import Stubber
    import reviewer_candidate_registry as registry
    values = setup_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("REVIEWER_CANDIDATE_STORAGE", "r2")
    monkeypatch.delenv("REVIEWER_ASSIST_CACHE_DIR", raising=False)
    client = boto3.client("s3", region_name="auto", endpoint_url="https://example.invalid",
        aws_access_key_id="test", aws_secret_access_key="test")
    client.meta.events.register("before-sign.s3.PutObject", registry._create_only_header)
    monkeypatch.setattr(registry, "_r2_client", lambda: (client, "test-bucket"))
    stub = Stubber(client)
    return values, client, stub


@pytest.mark.parametrize("environment", ["test", "staging"])
def test_r2_conditional_put_is_shared_readable_without_local_volume(monkeypatch, tmp_path, environment):
    from botocore.stub import ANY
    from io import BytesIO
    import reviewer_candidate_registry as registry
    values, client, stub = r2_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.delenv("REVIEWER_CANDIDATE_STORAGE_PREFIX", raising=False)
    song, candidate, review, job, document = values
    record = prepare_registry_record("tenant", song, candidate, review)
    created = datetime(2026, 9, 6, tzinfo=timezone.utc)
    envelope = {**record, "created_at": created.isoformat(), "expires_at": (created + timedelta(days=7)).isoformat()}
    key = registry._object_key("tenant", record["identity"])
    assert key.startswith("staging/") == (environment == "staging")
    conditional = ({"IfNoneMatch": "*"} if "IfNoneMatch" in
        client.meta.service_model.operation_model("PutObject").input_shape.members else {})
    stub.add_response("put_object", {}, {"Bucket": "test-bucket", "Key": key,
        "Body": ANY, "ContentType": "application/json", "CacheControl": "private, no-store", **conditional})
    stub.add_response("get_object", {"Body": BytesIO(json.dumps(envelope).encode())},
        {"Bucket": "test-bucket", "Key": key})
    with stub:
        assert register_candidate("tenant", song, candidate, review, now=created)["storage"] == "r2"
        payload = candidate_for_editor(job, document, now=created)
        assert payload["segments"] == candidate["segments"]
    stub.assert_no_pending_responses()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("prefix", ["reviewer-candidates/v1", "staging/../prod", "/staging/a", "staging//a"])
def test_staging_storage_prefix_rejects_shared_or_unsafe_keys(monkeypatch, prefix):
    import reviewer_candidate_registry as registry
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("REVIEWER_CANDIDATE_STORAGE_PREFIX", prefix)
    with pytest.raises(ValueError, match="invalid_reviewer_storage_prefix"):
        registry._object_key("tenant", "identity")


def test_r2_precondition_failure_reads_existing_never_overwrites(monkeypatch, tmp_path):
    from io import BytesIO
    values, _, stub = r2_fixture(monkeypatch, tmp_path)
    song, candidate, review, _, _ = values
    record = prepare_registry_record("tenant", song, candidate, review)
    stub.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412)
    stub.add_response("get_object", {"Body": BytesIO(json.dumps(record).encode())})
    with stub:
        assert register_candidate("tenant", song, candidate, review)["created"] is False
    stub.assert_no_pending_responses()


def test_r2_conflict_never_overwrites(monkeypatch, tmp_path):
    from io import BytesIO
    values, _, stub = r2_fixture(monkeypatch, tmp_path)
    song, candidate, review, _, _ = values
    stub.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412)
    stub.add_response("get_object", {"Body": BytesIO(b'{"payload": {}}')})
    with stub, pytest.raises(ValueError, match="immutable_candidate_conflict"):
        register_candidate("tenant", song, candidate, review)
    stub.assert_no_pending_responses()


def test_r2_unavailable_does_not_fallback_to_local_or_block_editor(monkeypatch, tmp_path):
    values, _, stub = r2_fixture(monkeypatch, tmp_path)
    song, candidate, review, job, document = values
    stub.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
    stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    with stub:
        assert register_candidate("tenant", song, candidate, review)["registered"] is False
        assert candidate_for_editor(job, document) is None
    assert list(tmp_path.iterdir()) == []


def test_pinned_old_sdk_signs_conditional_header_before_any_network(monkeypatch):
    import boto3
    import storage
    import reviewer_candidate_registry as registry
    client = boto3.client("s3", region_name="auto", endpoint_url="https://example.invalid",
        aws_access_key_id="test", aws_secret_access_key="test")
    # Simulate the exact missing member in repository-pinned botocore 1.35.0.
    client.meta.service_model.operation_model("PutObject").input_shape.members.pop("IfNoneMatch", None)
    monkeypatch.setattr(registry, "_object_client", None)
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    registry._r2_client()
    captured = []
    def intercept(request, **kwargs):
        captured.append(request.headers)
        raise RuntimeError("offline_intercept_before_network")
    client.meta.events.register("before-send.s3.PutObject", intercept)
    with pytest.raises(RuntimeError, match="offline_intercept_before_network"):
        client.put_object(Bucket="test-bucket", Key="candidate.json", Body=b"{}")
    assert captured[0]["If-None-Match"] == b"*"
    assert b"if-none-match" in captured[0]["Authorization"]
