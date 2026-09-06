"""Campaign-scoped product receipts. No inference on reads or publication."""
from copy import deepcopy
from datetime import datetime, timezone

from reviewer_assist_scope import display_enabled, publication_enabled
from reviewer_shadow import source_binding
from shadow_reference_import import digest

KEY = "reviewer_campaign_status"
CAMPAIGN = "ba3318bdfffe"


def live_source(job, document):
    return {"job_id": job.job_id, "audio_sha256": job.input_audio_sha256,
        "audio_revision": job.audio_revision, "segments_revision": document.revision,
        "segments_sha256": digest(document.current_segments or [])}


def public_blocker(value):
    text = str(value or "")
    if not text: return None
    if "empty" in text: return "empty_transcription"
    if "audio_not" in text or "not_downloaded" in text: return "audio_unavailable"
    if "source" in text or "stale" in text: return "source_changed"
    if "budget" in text: return "budget_hold"
    if "human_review" in text: return "active_human_review"
    if "proposal" in text: return "existing_proposal_preserved"
    if "tool" in text or "429" in text or "execution_failed" in text: return "tool_failure"
    return "review_blocked"


def status_for_job(job, document):
    if not job or not display_enabled(getattr(job, "campaign_id", None)):
        return None
    stored = (getattr(job, "transcription_quality", None) or {}).get(KEY)
    base = {"status": "pending", "candidate_available": False, "changes_count": None,
        "doubts_count": None, "blocker": "review_pending", "coverage_seconds": {}}
    if not isinstance(stored, dict) or stored.get("campaign_id") != job.campaign_id:
        return base
    result = {k: deepcopy(stored.get(k)) for k in base}
    if document is None or stored.get("source") != live_source(job, document):
        result.update(status="stale", candidate_available=False, blocker="source_changed")
    if result["status"] not in {"complete", "partial", "pending", "blocked", "stale"}:
        result.update(status="blocked", candidate_available=False, blocker="review_blocked")
    if result["status"] != "complete": result["candidate_available"] = False
    return result


def campaign_payload(db, campaign_id, pairs, documents=None):
    if not display_enabled(campaign_id): return None, {}
    jobs = [j for _, j in pairs if j]
    if documents is None:
        from database import EditorDocument
        from sqlalchemy.orm import load_only
        ids = [j.job_id for j in jobs]
        documents = {d.job_id: d for d in db.query(EditorDocument).options(load_only(
            EditorDocument.job_id, EditorDocument.revision, EditorDocument.current_segments
        )).filter(EditorDocument.job_id.in_(ids)).all()} if ids else {}
    statuses = {j.job_id: status_for_job(j, documents.get(j.job_id)) for j in jobs}
    rows = [s for s in statuses.values() if s]
    published = [(getattr(j, "transcription_quality", None) or {}).get(KEY, {}).get("published_at") for j in jobs]
    summary = {"enabled": True, "campaign_id": campaign_id, "total": len(pairs),
        "counters": {state: sum(r["status"] == state for r in rows) for state in ("complete", "partial", "pending", "blocked", "stale")},
        "candidate_count": sum(bool(r["candidate_available"]) for r in rows),
        "changed_song_count": sum(bool(r["candidate_available"] and r.get("changes_count")) for r in rows),
        "unchanged_song_count": sum(r["candidate_available"] and r.get("changes_count") == 0 for r in rows),
        "published_at": max((p for p in published if isinstance(p, str)), default=None)}
    summary["counters"]["pending"] += len(pairs) - len(rows)
    return summary, statuses


def prepare_status(row, *, candidate=None, registered=False, publication_reason=None, now=None):
    status = row["status"] if row["status"] in {"complete", "partial", "pending", "blocked"} else "blocked"
    if status == "complete" and not registered: status = "blocked"
    changes = len(candidate.get("changes", [])) if candidate else None
    qc = (candidate or {}).get("residual_qc", {})
    doubts = len(qc.get("unresolved_decisions", [])) if candidate else None
    reason = public_blocker(row.get("blocker"))
    if status == "partial" and not reason: reason = "missing_independent_audio"
    if row["status"] == "complete" and not registered: reason = "candidate_unavailable"
    if publication_reason and registered: reason = public_blocker(publication_reason)
    return {"schema": "reviewer-campaign-product-v1", "campaign_id": CAMPAIGN,
        "source": deepcopy(row["source"]), "status": status, "candidate_available": bool(registered),
        "changes_count": changes, "doubts_count": doubts, "blocker": reason,
        "coverage_seconds": deepcopy(row.get("coverage_seconds", {})),
        "published_at": (now or datetime.now(timezone.utc)).isoformat(), "automatic_apply_allowed": False}


def publish_song(db, campaign, song, row, artifact=None, *, execute=False):
    """Caller owns transaction; root release publisher alone may execute it."""
    from database import Job, EditorDocument
    from reviewer_candidate_registry import prepare_registry_record, register_candidate
    from reviewer_batch_bridge import publish_batch_candidate
    if campaign.id != CAMPAIGN or (execute and not publication_enabled(campaign.id)):
        raise ValueError("campaign_publication_not_enabled")
    job_query = db.query(Job).filter(Job.job_id == song["job_id"], Job.campaign_id == campaign.id, Job.tenant_id == campaign.tenant_id)
    doc_query = db.query(EditorDocument).filter(EditorDocument.job_id == song["job_id"], EditorDocument.tenant_id == campaign.tenant_id)
    job = job_query.populate_existing().with_for_update().first() if execute else job_query.first()
    document = doc_query.populate_existing().with_for_update().first() if execute else doc_query.first()
    if job is None or document is None: return {"job_id": song["job_id"], "status": "blocked", "reason": "job_or_document_missing"}
    if row["source"] != source_binding(song) or live_source(job, document) != row["source"]:
        stale = {**prepare_status(row), "status": "stale", "candidate_available": False, "blocker": "source_changed"}
        if execute:
            quality = deepcopy(job.transcription_quality or {}); quality[KEY] = stale; job.transcription_quality = quality
        return {"job_id": song["job_id"], "status": "stale", "reason": "source_changed"}
    scoped = {**song, "campaign_id": campaign.id}
    candidate = (artifact or {}).get("candidate")
    registered = False; publication_reason = None
    if row["status"] == "complete":
        if not candidate or not artifact.get("review"): raise ValueError("complete_candidate_artifact_missing")
        record = prepare_registry_record(campaign.tenant_id, scoped, candidate, artifact["review"], original_segments=song.get("original_segments"), versioned=True)
        candidate = {**candidate, "changes": record["payload"]["changes"]}
        import json
        from reviewer_candidate_registry import MAX_RECORD_BYTES
        if len(json.dumps(record).encode()) > MAX_RECORD_BYTES - 1024: raise ValueError("candidate_record_too_large")
        if execute:
            registration = register_candidate(campaign.tenant_id, scoped, candidate, artifact["review"], original_segments=song.get("original_segments"), versioned=True)
            registered = registration.get("registered") is True
            if registered:
                publication = publish_batch_candidate(db, scoped, candidate, artifact["review"])
                publication_reason = publication.get("reason") if not publication.get("published") else None
                if publication_reason in {"no_backed_changes", "human_approval_preserved"}:
                    publication_reason = None
                elif publication_reason == "existing_proposal_preserved":
                    existing = (document.quality_proposal or {}).get("reviewer_assist", {})
                    if (existing.get("source") == row["source"] and
                        existing.get("candidate", {}).get("candidate_sha256") == record["payload"]["candidate_sha256"]):
                        publication_reason = None
        else: registered = True
    state = prepare_status(row, candidate=candidate, registered=registered, publication_reason=publication_reason)
    if registered:
        # Only publish a new pointer after the immutable object was written.
        # Job metadata and native proposal commit together; documents stay intact.
        state['candidate_registry_identity'] = record['identity']
    if execute:
        quality = deepcopy(job.transcription_quality or {})
        old = quality.get(KEY)
        if isinstance(old, dict) and {k:v for k,v in old.items() if k != "published_at"} == {k:v for k,v in state.items() if k != "published_at"}:
            state = old
        quality[KEY] = state; job.transcription_quality = quality
    return {"job_id": song["job_id"], "status": state["status"], "candidate_available": registered,
        "proposal_reason": publication_reason, "executed": execute}
