"""Deleting a Job must not blow up on its ai_provenance audit rows.

Incident 2026-06-26 (Universal): PendingRollbackError. A Job that already had
ai_provenance rows was deleted (upload abort, re-upload of the same audio, or
the reaper's stale-job cleanup). Because the Job.provenance relationship had no
delete cascade, SQLAlchemy tried to `UPDATE ai_provenance SET job_id = NULL`
before deleting the parent — which violates the NOT NULL constraint on
ai_provenance.job_id, poisoning the session. Every later query in that request
then raised PendingRollbackError → HTTP 500 ("Sin respuesta del servidor"), and
a stale undeleteable job blocked re-uploading the same audio.

The fix cascades the delete: the audit rows go with the job.
"""

import hashlib

from database import AIProvenance, Job


def _seed_job_with_provenance(db, job_id):
    db.add(Job(
        job_id=job_id, user_id=1, tenant_id="prov_cascade_test",
        artist="A", filename="x.mp3", style="oscuro",
        status="awaiting_upload", delivery_profile="youtube", progress=0,
    ))
    db.commit()
    db.add(AIProvenance(
        job_id=job_id, step="lyrics_reference_fetch",
        tool_name="gemini-2.5-flash", tool_provider="google_vertex",
        prompt_sent="find lyrics", prompt_hash=hashlib.sha256(b"x").hexdigest(),
    ))
    db.commit()


def test_deleting_a_job_with_provenance_does_not_raise_and_cascades(db):
    jid = "provcasc0001"
    _seed_job_with_provenance(db, jid)
    assert db.query(AIProvenance).filter_by(job_id=jid).count() == 1

    job = db.query(Job).filter_by(job_id=jid).first()
    db.delete(job)
    db.commit()  # pre-fix: IntegrityError — UPDATE ai_provenance SET job_id=NULL

    # The job and its audit rows are both gone — no NULLed/orphaned provenance
    # left behind, no poisoned session.
    assert db.query(Job).filter_by(job_id=jid).first() is None
    assert db.query(AIProvenance).filter_by(job_id=jid).count() == 0
