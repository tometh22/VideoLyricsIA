"""Staging bridge from an authenticated local model runner to the existing editor.

Inference remains outside the API. Raw candidate text belongs to tenant-private
immutable storage and editor proposals; the quality field holds only a pointer.
Uploads never apply changes or approve a song.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Request

from operator_review_proposals import build_operator_review_proposal
from reviewer_shadow import source_binding
from shadow_reference_import import digest

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
POINTER_KEY = "local_genly_model"


def enabled():
    return (os.getenv("ENVIRONMENT", "").lower() == "staging" and
            os.getenv("GENLY_LOCAL_MODEL_CANDIDATES_ENABLED", "0").lower() in {"1", "true", "on"})


def _number(value):
    if isinstance(value, bool):
        raise ValueError("invalid_timing")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_timing") from None
    if not math.isfinite(number):
        raise ValueError("invalid_timing")
    return number


def _view(row):
    return (str(row.get("text") or ""), row.get("start"), row.get("end"))


def _changed(old, new):
    if _view(old) != _view(new):
        return True
    if "words" in new:
        word_view = lambda row: [(w.get("word"), w.get("start"), w.get("end")) for w in row.get("words", [])]
        return word_view(old) != word_view(new)
    return False


def _segments(rows, duration, baseline=None):
    if not isinstance(rows, list) or not rows or len(rows) > 5000:
        raise ValueError("invalid_candidate_segments")
    cleaned = []
    baseline = baseline if isinstance(baseline, list) and len(baseline) == len(rows) else []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("invalid_candidate_segment")
        start, end = _number(row.get("start")), _number(row.get("end"))
        text = row.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or not 0 <= start < end <= duration + .001:
            raise ValueError("invalid_candidate_segment")
        old = baseline[index] if baseline else None
        if cleaned and start < cleaned[-1]["end"] - .0001:
            # Existing editor timelines may contain overlaps. Retaining the
            # exact pair's bounds does not introduce or enlarge an overlap.
            inherited_overlap = (baseline and
                (start, end) == (old.get("start"), old.get("end")) and
                (cleaned[-1]["start"], cleaned[-1]["end"]) ==
                (baseline[index - 1].get("start"), baseline[index - 1].get("end")))
            if not inherited_overlap:
                raise ValueError("candidate_timeline_overlap")
        item = {"text": text.strip(), "start": start, "end": end}
        if row.get("words"):
            if not isinstance(row["words"], list) or len(row["words"]) > 1000:
                raise ValueError("invalid_candidate_words")
            # Legacy word evidence can disagree with edited accents/text or
            # contain historical timing defects. Preserve it byte-for-byte
            # only when the live source has exactly the same text and words,
            # and the line's bounds have not narrowed. This is preservation,
            # never a new alignment certification.
            inherited_words = (old is not None and text == old.get("text") and
                               row["words"] == old.get("words") and
                               start <= _number(old.get("start")) and
                               end >= _number(old.get("end")))
            if inherited_words:
                item["words"] = deepcopy(old["words"])
                if "word_alignment_status" in old:
                    item["word_alignment_status"] = deepcopy(old["word_alignment_status"])
                cleaned.append(item)
                continue
            words = []
            for word in row["words"]:
                if not isinstance(word, dict):
                    raise ValueError("invalid_candidate_words")
                a, b = _number(word.get("start")), _number(word.get("end"))
                spelling = word.get("word")
                if (not isinstance(spelling, str) or not spelling.strip() or len(spelling) > 100
                        or not start - .001 <= a < b <= end + .001
                        or (words and a < words[-1]["end"] - .001)):
                    raise ValueError("invalid_candidate_word_timing")
                words.append({"word": spelling, "start": a, "end": b})
            # Forced alignment cannot silently attach words for another text.
            fold = lambda value: re.findall(r"\w+", value.casefold())
            if fold(" ".join(w["word"] for w in words)) != fold(item["text"]):
                raise ValueError("candidate_words_text_mismatch")
            item["words"] = words
            item["word_alignment_status"] = "model_aligned_requires_review"
        cleaned.append(item)
    return cleaned


def _coverage(coverage):
    if not isinstance(coverage, dict):
        raise ValueError("coverage_required")
    duration = _number(coverage.get("duration_seconds"))
    if not 0 < duration <= 3600:
        raise ValueError("invalid_audio_duration")
    intervals = coverage.get("analyzed_intervals")
    if not isinstance(intervals, list) or len(intervals) > 10000:
        raise ValueError("coverage_intervals_required")
    normalized = []
    for interval in intervals:
        if not isinstance(interval, list) or len(interval) != 2:
            raise ValueError("invalid_coverage_interval")
        a, b = map(_number, interval)
        if not 0 <= a < b <= duration + .001:
            raise ValueError("invalid_coverage_interval")
        normalized.append([a, b])
    cursor, gaps = 0., []
    for a, b in sorted(normalized):
        if a > cursor + .001:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < duration - .001:
        gaps.append([cursor, duration])
    return {"duration_seconds": duration, "analyzed_intervals": normalized,
            "full_audio_covered": not gaps, "uncovered_intervals": gaps,
            "coverage_source": "authenticated_runner_report"}


def _change_windows(baseline, candidate, protected):
    """Close changed intervals over old/new rows; hold any protected component."""
    changed = [i for i in range(len(baseline)) if _changed(baseline[i], candidate[i])]
    components = []
    for index in changed:
        indices = {index}
        while True:
            start = min(min(baseline[i]["start"], candidate[i]["start"]) for i in indices)
            end = max(max(baseline[i]["end"], candidate[i]["end"]) for i in indices)
            expanded = indices | {i for i, (old, new) in enumerate(zip(baseline, candidate))
                                  if (old["start"] < end - .0001 and old["end"] > start + .0001)
                                  or (new["start"] < end - .0001 and new["end"] > start + .0001)}
            if expanded == indices:
                break
            indices = expanded
        if not any(indices == old for old in components):
            components.append(indices)
    # Components reached from different seeds can overlap; union to a fixpoint.
    merged = []
    for component in components:
        touching = [old for old in merged if old & component]
        for old in touching:
            component |= old
            merged.remove(old)
        merged.append(component)
    held, windows = [], []
    for indices in merged:
        indices = sorted(indices)
        if any(i in protected for i in indices):
            for i in indices:
                candidate[i] = deepcopy(baseline[i])
            held.extend(indices)
            continue
        current, proposed = [deepcopy(baseline[i]) for i in indices], [deepcopy(candidate[i]) for i in indices]
        start = min(row["start"] for row in [*current, *proposed])
        end = max(row["end"] for row in [*current, *proposed])
        text_changed = any(left["text"] != right["text"] for left, right in zip(current, proposed))
        windows.append({"kind": "operator_review_candidate", "id": digest([current, proposed]),
                        "suggestion_type": "text" if text_changed else "timing",
                        "start": start, "end": end, "current_segments": current,
                        "proposed_segments": proposed, "confidence": "low",
                        "reasons": ["local_model_candidate_requires_listening"],
                        "selector_policy": "genly-local-model-human-review-v1"})
    return windows, sorted(set(held))


def _partial_line_supported(index, baseline, candidate, report):
    """A global partial result may still contain a locally evidenced edit."""
    provenance = report.get("provenance") or {}
    if provenance.get("clock") != "original_mix_decoded" or provenance.get("reference_conditioned_recognition") is not False:
        return False
    findings = [f for f in report.get("findings", []) if isinstance(f, dict) and f.get("line_index") == index]
    if len(findings) != 1 or findings[0].get("status") != "proposed":
        return False
    finding = findings[0]
    for prefix, row in (("original", baseline), ("candidate", candidate)):
        if any(finding.get(f"{prefix}_{key}") != row[key] for key in ("text", "start", "end")):
            return False
    ids = finding.get("window_ids")
    if not isinstance(ids, list) or not ids:
        return False
    alignments = [a for a in report.get("alignment_evidence", []) if isinstance(a, dict)
                  and (a.get("window") or {}).get("id") in ids and a.get("status") == "aligned"
                  and (a.get("alignment") or {}).get("status") == "aligned_hypothesis"
                  and (a.get("alignment") or {}).get("clock") == "original_mix_decoded"]
    if set(ids) - {(a.get("window") or {}).get("id") for a in alignments}:
        return False
    windows = sorted((a["window"]["start"], a["window"]["end"]) for a in alignments)
    start, end = min(baseline["start"], candidate["start"]), max(baseline["end"], candidate["end"])
    cursor = start
    for a, b in windows:
        if a <= cursor + .001:
            cursor = max(cursor, b)
    if cursor < end - .001:
        return False
    words = finding.get("aligned_word_hypotheses") or []
    if not words or not all(isinstance(word, dict) for word in words):
        return False
    fold = lambda text: re.findall(r"\w+", text.casefold())
    if fold(" ".join(str(word.get("word", "")) for word in words)) != fold(candidate["text"]):
        return False
    previous = -1.
    for word in words:
        try:
            a, b = _number(word.get("start")), _number(word.get("end"))
        except (TypeError, ValueError):
            return False
        if not previous <= a < b or not any(left <= a < b <= right for left, right in windows):
            return False
        previous = b
    return True


def prepare_candidate(song, report):
    """Pure server rebuild: supplied proposal objects are intentionally ignored."""
    if report.get("schema") != "genly-model-result-v1" or report.get("source") != source_binding(song):
        raise ValueError("model_result_source_mismatch")
    status = report.get("status")
    model_id = report.get("model_id")
    if status not in {"complete", "partial"} or not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", model_id):
        raise ValueError("invalid_model_result_identity")
    coverage = _coverage(report.get("coverage"))
    if status == "complete" and not coverage["full_audio_covered"]:
        raise ValueError("complete_result_has_audio_gaps")
    baseline = deepcopy(song["segments"])
    candidate = _segments(report.get("candidate_segments"), coverage["duration_seconds"], baseline=baseline)
    if len(candidate) != len(baseline):
        raise ValueError("candidate_line_correspondence_required")
    original_keys = {_view(row) for row in song.get("original_segments", [])}
    protected = {i for i, row in enumerate(baseline)
                 if row.get("locked") or row.get("operator_locked") or _view(row) not in original_keys}
    # Preserve the live editor's renderer/identity fields. Never accept layout
    # supplied by a worker, nor carry acoustic confidence for replaced words.
    stale_fields = {"text", "start", "end", "words", "word_alignment_status", "review",
                    "alignment_score", "recognition_score", "line_signals_v2", "gap_recovered",
                    "repetition_group", "acoustic_corrected", "reference_fingerprint"}
    stale_prefixes = ("ctc_", "content_", "provider_", "evidence_", "timing_", "live_lexical_")
    for old, new in zip(baseline, candidate):
        for key, value in old.items():
            if key not in stale_fields and not key.startswith(stale_prefixes):
                new[key] = deepcopy(value)
    for index in range(len(baseline)):
        if not _changed(baseline[index], candidate[index]):
            candidate[index] = deepcopy(baseline[index])
    unsupported = []
    if status == "partial":
        for index in range(len(baseline)):
            if _changed(baseline[index], candidate[index]) and not _partial_line_supported(index, baseline[index], candidate[index], report):
                candidate[index] = deepcopy(baseline[index])
                unsupported.append(index)
    windows, held = _change_windows(baseline, candidate, protected)
    changes = [{"line_index": i, "before": {k: baseline[i][k] for k in ("text", "start", "end")},
                "after": {k: candidate[i][k] for k in ("text", "start", "end")}}
               for i in range(len(baseline)) if _changed(baseline[i], candidate[i])]
    projection = {"schema": "genly-model-editor-candidate-v1", "source": source_binding(song),
                  "model_id": model_id, "status": status, "baseline": baseline, "segments": candidate,
                  "changes": changes, "coverage": coverage,
                  "candidate_sha256": digest(candidate), "baseline_sha256": digest(baseline),
                  "evidence_sha256": digest(report), "correctness_certified": False,
                  "automatic_apply_allowed": False, "approved": False,
                  "review_details": {"localized_doubts": [
                      *[{"line_index": i, "reason": "human_protection"} for i in held],
                      *[{"line_index": i, "reason": "partial_result_missing_local_evidence"} for i in unsupported]]}}
    proposal = None
    if windows:
        proposal, _ = build_operator_review_proposal(
            baseline, text_candidates=[w for w in windows if w["suggestion_type"] == "text"],
            timing_candidates=[w for w in windows if w["suggestion_type"] == "timing"], maximum_windows=5000)
        proposal["local_genly_model"] = {"source": source_binding(song), "model_id": model_id,
                                         "analysis_status": status,
                                         "candidate_sha256": digest(candidate),
                                         "changes_text": any(left["text"] != right["text"] for left, right in zip(baseline, candidate)),
                                         "changes_timing": any((left["start"], left["end"], left.get("words")) !=
                                                               (right["start"], right["end"], right.get("words"))
                                                               for left, right in zip(baseline, candidate))}
    return projection, proposal


def live_song(job, document):
    return {"job_id": job.job_id, "segments": deepcopy(document.current_segments or []),
            "original_segments": deepcopy(document.original_segments or []),
            "segments_revision": int(document.revision or 0),
            "segments_sha256": digest(document.current_segments or []),
            "audio_sha256": job.input_audio_sha256, "audio_revision": int(job.audio_revision or 0)}


def _register(tenant_id, candidate):
    from reviewer_candidate_registry import _mode, _register_r2, _root
    identity = digest({"tenant_id": str(tenant_id), "candidate": candidate})
    now = datetime.now(timezone.utc)
    envelope = {"schema": "genly-model-candidate-registry-v1", "identity": identity,
                "tenant_id": str(tenant_id), "payload": candidate, "payload_sha256": digest(candidate),
                "created_at": now.isoformat(), "expires_at": (now + timedelta(days=7)).isoformat()}
    if _mode() == "r2":
        _register_r2(envelope)
    elif _mode() == "local" and _root() is not None:
        root = _root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / f"{identity}.json"
        with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as output:
            temporary = Path(output.name)
            json.dump(envelope, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        try:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = json.loads(destination.read_text())
                if existing.get("payload_sha256") != envelope["payload_sha256"] or existing.get("payload") != candidate:
                    raise ValueError("immutable_candidate_conflict")
        finally:
            temporary.unlink(missing_ok=True)
    else:
        raise ValueError("candidate_storage_not_configured")
    return identity


def _read_candidate(tenant_id, song, pointer, proposal):
    from reviewer_candidate_registry import _mode, _read_r2, _root
    if not pointer or pointer.get("source") != source_binding(song):
        return None
    identity = pointer.get("identity", "")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        return None
    try:
        if _mode() == "r2":
            record = _read_r2(tenant_id, identity)
        elif _mode() == "local" and _root() is not None:
            record = json.loads((_root() / f"{identity}.json").read_text())
        else:
            return None
        candidate = record["payload"]
        if (record.get("schema") != "genly-model-candidate-registry-v1" or record.get("identity") != identity
                or record.get("tenant_id") != str(tenant_id) or record.get("payload_sha256") != digest(candidate)
                or datetime.fromisoformat(record["expires_at"]) <= datetime.now(timezone.utc)
                or candidate.get("source") != source_binding(song) or candidate.get("baseline") != song["segments"]
                or candidate.get("candidate_sha256") != digest(candidate.get("segments"))):
            return None
        matching = ((proposal or {}).get("local_genly_model", {}).get("candidate_sha256") == candidate["candidate_sha256"]
                    and proposal.get("status") == "pending")
        candidate["adoption_status"] = "matching_existing_proposal" if matching else "candidate_not_published_for_adoption"
        return candidate
    except (OSError, ValueError, KeyError, TypeError):
        return None


def publish_candidate(db, job, document, report, user):
    from database import AuditLog, EditorDocument, Job
    from editor import _proposal_for_response, persist_operator_review_proposal_if_current
    from transcription_quality import segments_hash
    # Reacquire both rows in the same order as native editor writes.
    job = db.query(Job).filter(Job.job_id == job.job_id).populate_existing().with_for_update().one()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).populate_existing().with_for_update().one()
    if job.approved_at or job.status in {"lyrics_approved", "done"}:
        raise ValueError("approved_song_preserved")
    song = live_song(job, document)
    candidate, proposal = prepare_candidate(song, report)
    allowed_ids = {value.strip() for value in os.getenv("GENLY_LOCAL_MODEL_ALLOWED_IDS", "").split(",") if value.strip()}
    if allowed_ids and candidate["model_id"] not in allowed_ids:
        raise ValueError("model_version_not_enabled")
    existing = _proposal_for_response(document) or {}
    if existing.get("status") not in {"pending", "observing"}:
        existing = {}
    same = (existing.get("local_genly_model", {}).get("candidate_sha256") == candidate["candidate_sha256"]
            and existing.get("local_genly_model", {}).get("source") == candidate["source"]
            and existing.get("status") == "pending")
    adoption = "no_changes" if not candidate["changes"] else "partial_result_review_only"
    if proposal and existing and not same:
        adoption = "existing_proposal_preserved"
    elif proposal and not same:
        success = persist_operator_review_proposal_if_current(
            db, job_id=job.job_id, expected_revision=song["segments_revision"],
            expected_segments_hash=segments_hash(song["segments"]), expected_audio_revision=song["audio_revision"],
            expected_audio_sha256=song["audio_sha256"], proposal=proposal)
        adoption = "proposal_ready" if success else "proposal_gates_disabled_or_ineligible"
    elif same:
        adoption = "proposal_ready"
    identity = _register(job.tenant_id, candidate)
    quality = deepcopy(job.transcription_quality or {})
    quality[POINTER_KEY] = {"identity": identity, "source": candidate["source"], "model_id": candidate["model_id"],
                            "status": candidate["status"], "changes_count": len(candidate["changes"]), "adoption": adoption}
    job.transcription_quality = quality
    db.add(AuditLog(user_id=user["id"], action="editor.genly_model_candidate", detail={
        "job_id": job.job_id, "model_id": candidate["model_id"], "candidate_sha256": candidate["candidate_sha256"],
        "source_revision": song["segments_revision"], "status": candidate["status"], "adoption": adoption}))
    db.flush()
    return {"published": True, "status": candidate["status"], "candidate_id": identity,
            "model_id": candidate["model_id"], "changes_count": len(candidate["changes"]),
            "adoption": adoption, "proposal_id": (document.quality_proposal or {}).get("id"),
            "automatic_apply_allowed": False}


def create_test_copy(db, body, user):
    """Create a human-owned staging draft; read the source without modifying it."""
    from database import AuditLog, EditorDocument, Job
    from editor import ensure_document
    from jobs import create_job
    source_id = body.get("source_job_id")
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{12}", source_id):
        raise ValueError("invalid_source_job_id")
    original = body.get("use_original", True)
    if type(original) is not bool:
        raise ValueError("invalid_use_original")
    source = db.query(Job).filter(Job.job_id == source_id).first()
    if source is None:
        raise LookupError("source_job_not_found")
    if source.pilot_id or (source.render_params or {}).get("genly_model_test_copy"):
        raise ValueError("test_copy_cannot_be_source")
    if not source.input_r2_key or not re.fullmatch(r"[a-f0-9]{64}", source.input_audio_sha256 or ""):
        raise ValueError("original_audio_identity_required")
    source_document = db.query(EditorDocument).filter(EditorDocument.job_id == source_id).first()
    if original:
        baseline = deepcopy((source_document.original_segments if source_document else None)
                            or (source_document.current_segments if source_document else None) or source.segments_json or [])
    else:
        baseline = deepcopy((source_document.current_segments if source_document else None) or source.segments_json or [])
    if not baseline:
        raise ValueError("source_lyrics_required")
    tenant = str(user.get("tenant_id") or "")
    if not tenant:
        raise ValueError("publisher_tenant_required")
    job_id = create_job(db, artist=source.artist, style=source.style or "oscuro", filename=source.filename,
                        user_id=user["id"], tenant_id=tenant, delivery_profile=source.delivery_profile or "youtube",
                        initial_status="transcribed_pending", song_title="[Prueba modelo] " + (source.song_title or ""),
                        input_r2_key=source.input_r2_key, workload_class="interactive", campaign_id=None,
                        campaign_item_id=None, commit=False)
    copy = db.query(Job).filter(Job.job_id == job_id).one()
    copy.parent_job_id = source.job_id
    copy.input_audio_sha256 = source.input_audio_sha256
    copy.input_audio_etag = source.input_audio_etag
    copy.audio_revision = source.audio_revision
    copy.segments_json = baseline
    copy.segments_revision = 0
    copy.machine_snapshot_required = False
    copy.render_params = {"genly_model_test_copy": True, "source_job_id": source.job_id}
    db.flush()
    document = ensure_document(db, job_id, tenant, baseline, initial_reason="migration")
    db.add(AuditLog(user_id=user["id"], action="staging.genly_model_test_copy", detail={
        "source_job_id": source.job_id, "source_tenant_id": source.tenant_id,
        "copy_job_id": job_id, "copy_tenant_id": tenant, "use_original": original,
        "lines": len(document.current_segments), "learning_eligible": False}))
    return {"job_id": job_id, "source_job_id": source.job_id, "revision": document.revision,
            "audio_sha256": copy.input_audio_sha256, "lines": len(document.current_segments),
            "approved": False, "learning_eligible": False, "campaign_id": None, "owner_user_id": user["id"]}


def register_routes(app, *, get_current_user, get_db, authorize_editor):
    router = APIRouter()

    def admin(user):
        if not enabled():
            raise HTTPException(404, "local_model_disabled")
        if not (user.get("role") == "admin" or user.get("is_super_admin")):
            raise HTTPException(403, "model_publisher_required")

    def queue_call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(409, str(exc)[:100]) from None
        except Exception:
            raise HTTPException(503, "model_queue_unavailable") from None

    @router.post("/admin/genly-model/test-copy")
    def make_test_copy(body: dict, user=Depends(get_current_user), db=Depends(get_db)):
        admin(user)
        try:
            result = create_test_copy(db, body, user)
            db.commit()
            return result
        except LookupError:
            db.rollback()
            raise HTTPException(404, "source_job_not_found") from None
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)[:120]) from None
        except Exception:
            db.rollback()
            raise

    @router.post("/editor/{job_id}/genly-model/request")
    def enqueue_request(job_id: str, body: dict, user=Depends(get_current_user), db=Depends(get_db)):
        if not enabled():
            raise HTTPException(404, "local_model_disabled")
        job, document = authorize_editor(db, job_id, user)
        if type(body.get("base_revision")) is not int or body["base_revision"] != document.revision:
            db.rollback()
            raise HTTPException(409, "editor_revision_conflict")
        if job.approved_at or job.status in {"lyrics_approved", "done"}:
            db.rollback()
            raise HTTPException(409, "approved_song_preserved")
        source = source_binding(live_song(job, document))
        db.commit()
        from genly_model_queue import enqueue, worker_available
        return {"request": queue_call(enqueue, job_id, source, user["id"]),
                "worker_available": queue_call(worker_available)}

    @router.get("/editor/{job_id}/genly-model/status")
    def request_status(job_id: str, user=Depends(get_current_user), db=Depends(get_db)):
        if not enabled():
            raise HTTPException(404, "local_model_disabled")
        job, document = authorize_editor(db, job_id, user)
        source = source_binding(live_song(job, document))
        db.commit()
        from genly_model_queue import status
        result = queue_call(status, job_id)
        if result.get("request") and result["request"].get("source") != source:
            result["request"]["status"] = "stale"
        return result

    @router.post("/admin/genly-model/claim")
    def claim_request(body: dict, user=Depends(get_current_user)):
        admin(user)
        from genly_model_queue import claim
        return queue_call(claim, body.get("worker_id"))

    @router.post("/admin/genly-model/heartbeat")
    def worker_heartbeat(body: dict, user=Depends(get_current_user)):
        admin(user)
        from genly_model_queue import heartbeat
        return queue_call(heartbeat, body.get("worker_id"), run_id=body.get("run_id"),
                          lease_token=body.get("lease_token"), stage=body.get("stage"))

    @router.post("/admin/genly-model/fail")
    def fail_request(body: dict, user=Depends(get_current_user)):
        admin(user)
        from genly_model_queue import finish, verify_claim
        row = queue_call(verify_claim, body.get("run_id"), body.get("lease_token"))
        if row.get("worker_id") != body.get("worker_id"):
            raise HTTPException(409, "model_lease_lost")
        return {"request": queue_call(finish, body.get("run_id"), body.get("lease_token"),
                                       status="failed", reason=body.get("reason", "model_failed"))}

    @router.get("/editor/{job_id}/genly-model/request")
    def model_request(job_id: str, user=Depends(get_current_user), db=Depends(get_db)):
        if not enabled():
            raise HTTPException(404, "local_model_disabled")
        job, document = authorize_editor(db, job_id, user)
        song = live_song(job, document)
        song.update(status=job.status, approved_at=str(job.approved_at) if job.approved_at else None)
        db.commit()
        return {"snapshot": song, "source_audio_path": f"/jobs/{job_id}/source-audio-url?prefer_original=1",
                "execution": "local_runner_required"}

    @router.get("/editor/{job_id}/genly-model/candidate")
    def get_candidate(job_id: str, user=Depends(get_current_user), db=Depends(get_db)):
        if not enabled():
            raise HTTPException(404, "local_model_disabled")
        job, document = authorize_editor(db, job_id, user)
        tenant, song = str(job.tenant_id), live_song(job, document)
        pointer = deepcopy((job.transcription_quality or {}).get(POINTER_KEY))
        from editor import _proposal_for_response
        proposal = deepcopy(_proposal_for_response(document))
        db.commit()  # Storage I/O must not hold an editor transaction open.
        return {"candidate": _read_candidate(tenant, song, pointer, proposal), "quality_proposal": proposal}

    @router.post("/editor/{job_id}/genly-model/candidate")
    async def upload_candidate(job_id: str, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
        admin(user)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "model_result_too_large")
        try:
            report = json.loads(body)
            if not isinstance(report, dict):
                raise ValueError("model_result_required")
            run_id, lease_token = request.headers.get("X-Genly-Run-Id"), request.headers.get("X-Genly-Lease-Token")
            if bool(run_id) != bool(lease_token):
                raise ValueError("model_lease_lost")
            if run_id:
                from genly_model_queue import verify_claim
                verify_claim(run_id, lease_token, job_id=job_id, source=report.get("source"))
            job, document = authorize_editor(db, job_id, user)
            result = publish_candidate(db, job, document, report, user)
            if run_id:
                verify_claim(run_id, lease_token, job_id=job_id, source=report.get("source"))
            db.commit()
            if run_id:
                from genly_model_queue import finish
                result["request"] = finish(run_id, lease_token, status=result["status"])
            return result
        except (ValueError, TypeError, KeyError) as exc:
            db.rollback()
            reason = str(exc)
            code = 409 if reason in {"model_result_source_mismatch", "approved_song_preserved", "model_lease_lost",
                                    "model_request_source_mismatch"} else 422
            raise HTTPException(code, reason[:160]) from None
        except Exception:
            db.rollback()
            raise

    app.include_router(router)
