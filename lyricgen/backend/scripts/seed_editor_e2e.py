"""Seed two same-workspace operators and one durable-editor browser job."""

import sys
import os
from pathlib import Path

# Allow direct execution from ``backend/scripts`` in CI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import create_user
from database import Job, SessionLocal, init_db

JOB_ID = "e2ecollab001"
TENANT_ID = "editor_e2e_team"
PASSWORD = "EditorE2E-test-123"


def seed_reviewer_candidate(db, user):
    """Synthetic integration evidence, NOT model output or a real song.

    This opt-in fixture is restricted to the disposable CI database. No media
    provider is called; the browser supplies the same synthetic WAV throughout.
    """
    from sqlalchemy.engine import make_url
    if make_url(os.environ["DATABASE_URL"]).database != "genly_editor_e2e":
        raise RuntimeError("reviewer_fixture_requires_disposable_ci_database")
    from database import BatchCampaign, BatchCampaignItem
    from editor import get_or_create_document
    from reviewer_batch_bridge import REQUIRED_AUDIO_FAMILIES, publish_batch_candidate
    from reviewer_candidate import build_candidate
    from reviewer_candidate_registry import register_candidate
    from reviewer_shadow import review_window, source_binding
    from reference_hypothesis import build_unavailable
    from shadow_reference_import import digest

    campaign_id, job_id = "e2erevcamp01", "e2ereview001"
    rows = [{"_id": 0, "text": "Canto así", "start": 2., "end": 4.},
            {"_id": 1, "text": "No cambiar", "start": 6., "end": 8., "locked": True}]
    db.add(BatchCampaign(id=campaign_id, tenant_id=TENANT_ID, created_by=user.id,
        name="Synthetic reviewer integration", expected_count=1))
    db.flush()
    db.add(BatchCampaignItem(id="e2erevieweritem", campaign_id=campaign_id,
        tenant_id=TENANT_ID, ordinal=1, filename="reviewer.wav", title="Synthetic candidate",
        artist="E2E Artist", technical_code="E2E-REVIEW", sha256="a" * 64,
        duration_seconds=10., upload_state="uploaded"))
    db.flush()
    db.add(Job(job_id=job_id, user_id=user.id, tenant_id=TENANT_ID,
        campaign_id=campaign_id, campaign_item_id="e2erevieweritem",
        artist="E2E Artist", song_title="Synthetic candidate", filename="reviewer.wav",
        style="oscuro", status="transcribed_pending", current_step="editing", progress=100,
        delivery_profile="youtube", segments_json=rows, segments_revision=0,
        audio_revision=1, input_audio_sha256="a" * 64,
        transcription_quality={"reference_hypothesis": build_unavailable(
            audio_sha256="a" * 64, audio_revision=1,
            source_version={"synthetic_ci_fixture": True}),
            "reference_hypothesis_unavailable": True},
        input_r2_key=f"inputs/{TENANT_ID}/{job_id}/reviewer.wav"))
    db.flush()
    document = get_or_create_document(db, job_id, TENANT_ID, rows)
    song = {"job_id": job_id, "campaign_id": campaign_id, "audio_sha256": "a" * 64,
        "audio_revision": 1, "segments_revision": document.revision,
        "segments": document.current_segments, "segments_sha256": digest(document.current_segments),
        "duration_seconds": 10.}
    evidence = [{"kind": "content", "family": family, "text": "Canto aquí",
        "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
        "occurrence_verified": True, "synthetic_ci_fixture": True}
        for family in ("whisper-1", "gemini")]
    decision = review_window(song, {"line_index": 0, "start": 1., "end": 5.,
        "offset_seconds": 1.}, evidence=evidence, commit="0" * 40)
    candidate = build_candidate(song, [decision])
    review = {"schema": "full-song-review-v1", "source": source_binding(song),
        "reconciliation_complete": True, "synthetic_ci_fixture": True,
        "required_families": sorted(REQUIRED_AUDIO_FAMILIES),
        "audio_evidence": [{"source": source_binding(song), "family": family,
            "tool_status": "ok", "received_audio": True, "start": 0., "end": 10.,
            "clock": "original_mix_decoded", "evidence_sha256": digest(["synthetic", family])}
            for family in sorted(REQUIRED_AUDIO_FAMILIES)],
        "localized_doubts": [{"line_index": 1, "reason": "human_protection"}]}
    registered = register_candidate(TENANT_ID, song, candidate, review,
        original_segments=document.original_segments)
    if not registered.get("registered"):
        raise RuntimeError(f"reviewer_fixture_registry_failed:{registered.get('reason')}")
    published = publish_batch_candidate(db, song, candidate, review)
    if not published.get("published"):
        raise RuntimeError(f"reviewer_fixture_proposal_failed:{published.get('reason')}")


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        users = []
        for username in ("editor_e2e_a", "editor_e2e_b"):
            user = create_user(db, username, PASSWORD, None, tenant_id=TENANT_ID)
            users.append(user)
        segments = [
            {"_id": f"line-{index}", "start": index * 0.8, "end": index * 0.8 + 0.6, "text": text}
            for index, text in enumerate(("Primera línea", "Segunda línea", "Tercera línea", "Cuarta línea", "Quinta línea"))
        ]
        db.add(Job(
            job_id=JOB_ID,
            user_id=users[0].id,
            tenant_id=TENANT_ID,
            artist="E2E Artist",
            song_title="Collaboration",
            filename="collaboration.wav",
            style="oscuro",
            status="pending_review",
            current_step="editing",
            progress=100,
            delivery_profile="youtube",
            segments_json=segments,
            segments_revision=0,
            input_r2_key=f"inputs/{TENANT_ID}/{JOB_ID}/collaboration.wav",
            bg_r2_key_cached=f"backgrounds/{TENANT_ID}/{JOB_ID}/background.mp4",
        ))
        db.commit()
        if os.getenv("REAL_REVIEWER_E2E") == "1":
            reviewer = create_user(db, "reviewer_e2e_admin", PASSWORD, None, tenant_id=TENANT_ID)
            reviewer.role = "admin"  # /admin/cola intentionally stays admin-only.
            db.flush()
            seed_reviewer_candidate(db, reviewer)
            db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
