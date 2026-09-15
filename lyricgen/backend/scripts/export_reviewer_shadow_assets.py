"""Read-only selected-job evidence + GET signatures; output contains private URLs."""
import argparse
import hashlib
import json
import os


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", required=True)
    args = parser.parse_args()
    ids = args.jobs.split(",")
    if len(ids) > 30 or any(len(i) != 12 or not i.isalnum() for i in ids):
        raise ValueError("bounded job ids required")
    from sqlalchemy import text
    from database import SessionLocal, Job, EditorDocument
    import storage
    from vocal_sep import _MODEL, _VARIANT
    identity = "|".join((_MODEL, _VARIANT, os.environ.get("DEMUCS_MODEL_VERSION", "unknown"),
                         os.environ.get("DEMUCS_MODEL_CHECKSUM", "unknown")))
    model_digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    db = SessionLocal()
    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        records = db.query(Job.job_id, Job.input_audio_sha256, Job.input_r2_key,
                           Job.audio_revision, EditorDocument.machine_evidence).join(
            EditorDocument, EditorDocument.job_id == Job.job_id).filter(Job.job_id.in_(ids)).all()
        output = []
        for jid, sha, input_key, revision, evidence in records:
            stem_key = f"stems/{sha}_{_VARIANT}_{model_digest}.wav"
            # Signatures are GET-only; no queues, object writes, or metadata changes.
            output.append({"job_id": jid, "audio_sha256": sha, "audio_revision": revision,
                           "mix_url": storage.generate_signed_url(input_key, expiry_seconds=14400),
                           "stem_url": storage.generate_signed_url(stem_key, expiry_seconds=14400),
                           "stem_key": stem_key, "stem_model_identity": identity,
                           "machine_evidence": {k: (evidence or {}).get(k) for k in
                               ("schema", "pre_human", "hypotheses_by_family", "capture")}})
        print(json.dumps({"jobs": output, "read_only": True}))
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
