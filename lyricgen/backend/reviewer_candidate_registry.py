"""Immutable full-song review artifacts, separate from adoptable edit windows.

This is not an authorization layer. Fetch only after the existing editor route
has authorized the job. Live Job and EditorDocument, never request-supplied
source metadata, determine which artifact may be returned.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from botocore.exceptions import BotoCoreError, ClientError

from reviewer_assist import enabled
from reviewer_batch_bridge import prepare_batch_candidate
from reviewer_shadow import source_binding, validate_snapshot
from shadow_reference_import import digest

MAX_RECORD_BYTES = 8 * 1024 * 1024
_object_client = None


def _mode():
    return os.environ.get("REVIEWER_CANDIDATE_STORAGE", "local").strip().lower()


def _create_only_header(request, **kwargs):
    # Compatibility with boto3 1.35.0: the service model lacks IfNoneMatch,
    # but signing an explicit HTTP header is supported. This hook exists only
    # on the registry client, never storage's shared media client.
    request.headers["If-None-Match"] = "*"


def _r2_client():
    """Existing R2 configuration, separate bounded SDK pool for editor reads.

    storage's bulk-media client has 120 s read timeouts: do not put that client
    on the editor read path or modify its settings for unrelated media jobs.
    """
    import storage
    if not storage.is_enabled():
        raise RuntimeError("candidate_r2_not_configured")
    global _object_client
    if _object_client is None:
        import boto3
        from botocore.config import Config
        options = dict(signature_version="s3v4", connect_timeout=2, read_timeout=3,
            retries={"total_max_attempts": 1}, max_pool_connections=4)
        # Checksum switches are absent from the repository's older pinned SDK.
        for name in ("request_checksum_calculation", "response_checksum_validation"):
            if name in Config.OPTION_DEFAULTS:
                options[name] = "when_required"
        _object_client = boto3.client("s3", endpoint_url=storage.R2_ENDPOINT_URL,
            aws_access_key_id=storage.R2_ACCESS_KEY_ID,
            aws_secret_access_key=storage.R2_SECRET_ACCESS_KEY,
            config=Config(**options))
        _object_client.meta.events.register("before-sign.s3.PutObject", _create_only_header)
    return _object_client, storage.R2_BUCKET


def _object_key(tenant_id, identity):
    return f"reviewer-candidates/v1/{digest(str(tenant_id))}/{identity}.json"


def _read_r2(tenant_id, identity):
    client, bucket = _r2_client()
    response = client.get_object(Bucket=bucket, Key=_object_key(tenant_id, identity))
    body = response["Body"]
    try:
        data = body.read(MAX_RECORD_BYTES + 1)
    finally:
        body.close()
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("candidate_record_too_large")
    return json.loads(data)


def _register_r2(envelope):
    client, bucket = _r2_client()
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("candidate_record_too_large")
    identity = envelope["identity"]
    try:
        # Verified SDK support + R2 conditional operations. Never retry with an
        # unconditional overwrite if the server rejects the precondition.
        modeled = client.meta.service_model.operation_model("PutObject").input_shape.members
        conditional = {"IfNoneMatch": "*"} if "IfNoneMatch" in modeled else {}
        client.put_object(Bucket=bucket, Key=_object_key(envelope["tenant_id"], identity),
            Body=encoded, ContentType="application/json", CacheControl="private, no-store",
            **conditional)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
        existing = _read_r2(envelope["tenant_id"], identity)
        if (existing.get("payload_sha256") != envelope["payload_sha256"]
                or existing.get("payload") != envelope["payload"]):
            raise ValueError("immutable_candidate_conflict")
        return {"registered": True, "created": False, "identity": identity, "storage": "r2"}
    return {"registered": True, "created": True, "identity": identity, "storage": "r2"}

def _root():
    configured = os.environ.get("REVIEWER_ASSIST_CACHE_DIR")
    return Path(configured) / "complete_candidates" if configured else None


def _identity(tenant_id, song):
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_identity_required")
    validate_snapshot(song)
    return digest({"tenant_id": str(tenant_id), "source": source_binding(song)})


def _review_details(review):
    """Explicit UI projection, never raw provider requests or local paths."""
    fields = {"line_index", "protected", "start", "end", "global_start", "global_end",
        "text", "reason", "classification", "phrase_status", "occurrence_status",
        "endpoint_generation", "endpoint_selector", "content_decision", "discrepancy_class",
        "proposed_text", "status", "kind", "evidence_sha256", "proposal_id"}
    def row(item):
        return {k: v for k, v in item.items() if k in fields
                and isinstance(v, (str, int, float, bool, type(None)))}
    details = {}
    for name in ("line_diagnostics", "uncovered_singing_hypotheses", "localized_doubts",
                 "line_results", "findings", "tool_failures", "abstentions", "unresolved_intervals"):
        details[name] = [row(item) for item in review.get(name, []) if isinstance(item, dict)]
    details["held_decisions"] = [{"reason": item.get("reason"),
        "proposal_id": item.get("decision", {}).get("proposal_id"),
        "line_index": item.get("decision", {}).get("window", {}).get("line_index")}
        for item in review.get("held_decisions", []) if isinstance(item, dict)]
    details["invalid_annotations"] = [{"evidence_sha256": item.get("evidence_sha256"),
        "count": len(item.get("annotations", []))}
        for item in review.get("invalid_annotations", []) if isinstance(item, dict)]
    return details


def prepare_registry_record(tenant_id, song, candidate, review, *, original_segments=None):
    """Pure, possible with rollout off; excludes held/previously-human edits."""
    identity = _identity(tenant_id, song)
    prepared = prepare_batch_candidate(song, candidate, review,
        original_segments=original_segments)
    safe = prepared["candidate"]
    payload = {"schema": "reviewer-complete-candidate-v1", "id": safe["id"],
        "source": source_binding(song), "baseline": deepcopy(safe["baseline"]),
        "segments": deepcopy(safe["segments"]), "changes": deepcopy(safe["changes"]),
        "baseline_sha256": safe["baseline_sha256"], "candidate_sha256": safe["candidate_sha256"],
        "coverage_seconds": prepared["coverage_seconds"], "reconciliation_complete": True,
        "residual_qc": deepcopy(safe["residual_qc"]),
        "review_details": _review_details(review),
        "original_candidate_residual_qc": deepcopy(candidate.get("residual_qc", {})),
        "held_decision_ids": prepared["held_decision_ids"],
        "review_receipt_sha256": digest(review),
        "review_complete": True, "correctness_certified": False,
        "approved": False, "automatic_apply_allowed": False,
        "adoption_via_existing_operator_proposal_only": True,
        "audio_playback": "existing_authorized_editor_audio"}
    return {"schema": "reviewer-candidate-registry-v1", "identity": identity,
        "tenant_id": str(tenant_id), "payload": payload, "payload_sha256": digest(payload)}


def register_candidate(tenant_id, song, candidate, review, *, original_segments=None, now=None):
    """Future authorized artifact publication; no document or approval writes.

    Same source/content is idempotent; a different candidate for the same source
    fails rather than silently replacing evidence. The cache must be durable
    and shared by the authorized publisher and editor API.
    """
    if not enabled():
        return {"registered": False, "reason": "reviewer_assist_disabled"}
    mode, root = _mode(), _root()
    if mode not in {"local", "r2"}:
        return {"registered": False, "reason": "unsupported_candidate_storage"}
    if mode == "local" and root is None:
        return {"registered": False, "reason": "persistent_cache_directory_required"}
    record = prepare_registry_record(tenant_id, song, candidate, review,
        original_segments=original_segments)
    created = now or datetime.now(timezone.utc)
    envelope = {**record, "created_at": created.isoformat(),
        "expires_at": (created + timedelta(days=7)).isoformat()}
    if mode == "r2":
        try:
            return _register_r2(envelope)
        except (BotoCoreError, ClientError, RuntimeError):
            return {"registered": False, "reason": "candidate_r2_unavailable"}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{record['identity']}.json"
    # Link a fully fsynced temporary file atomically, without replacing a prior
    # immutable record. A crash during serialization cannot poison its key.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".candidate-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(envelope, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = json.loads(target.read_text())
            if existing.get("payload_sha256") != record["payload_sha256"] or existing.get("payload") != record["payload"]:
                raise ValueError("immutable_candidate_conflict")
            return {"registered": True, "created": False, "identity": record["identity"]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"registered": True, "created": True, "identity": record["identity"]}


def candidate_for_editor(job, document, *, now=None):
    """Read-only lookup for an ALREADY authorized editor Job/Document pair.

    Missing, stale, expired, corrupt or cross-tenant records return no candidate.
    No unsigned audio URLs or private filesystem paths enter the response.
    """
    if not enabled() or _mode() not in {"local", "r2"} or (_mode() == "local" and _root() is None):
        return None
    if str(job.job_id) != str(document.job_id) or str(job.tenant_id) != str(document.tenant_id):
        return None
    song = {"job_id": job.job_id, "audio_sha256": job.input_audio_sha256,
        "audio_revision": job.audio_revision, "segments_revision": document.revision,
        "segments": list(document.current_segments or []),
        "segments_sha256": digest(document.current_segments or [])}
    try:
        identity = _identity(job.tenant_id, song)
        if _mode() == "r2":
            record = _read_r2(job.tenant_id, identity)
        else:
            path = _root() / f"{identity}.json"
            record = json.loads(path.read_text())
        expires = datetime.fromisoformat(record["expires_at"])
        current = now or datetime.now(timezone.utc)
        payload = record["payload"]
        if (expires <= current or record.get("schema") != "reviewer-candidate-registry-v1"
                or record.get("identity") != identity or record.get("tenant_id") != str(job.tenant_id)
                or payload.get("source") != source_binding(song)
                or record.get("payload_sha256") != digest(payload)
                or payload.get("baseline") != song["segments"]
                or payload.get("candidate_sha256") != digest(payload.get("segments"))):
            return None
        result = deepcopy(payload)
        result["current_song_approved"] = bool(getattr(document, "approved_at", None)
            or getattr(job, "approved_at", None) or getattr(job, "status", None) in {"lyrics_approved", "done"})
        result["read_only"] = True
        return result
    except (OSError, ValueError, KeyError, TypeError, BotoCoreError, ClientError, RuntimeError):
        return None
