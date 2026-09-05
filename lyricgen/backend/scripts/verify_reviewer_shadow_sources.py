"""Run inside API container. Compare source hashes without touching any record."""
import argparse
import hashlib
import json
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bindings', required=True)
    args = parser.parse_args()
    expected = {}
    for part in args.bindings.split(','):
        jid, revision, sha = part.split(':')
        if not jid.isalnum() or len(jid) != 12 or len(sha) != 64:
            raise ValueError('invalid source binding')
        expected[jid] = (int(revision), sha)
    if len(expected) > 30:
        raise ValueError('read budget exceeded')
    from database import SessionLocal, Job, EditorDocument
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(text('SET TRANSACTION READ ONLY'))
        records = db.query(Job.job_id, Job.status, Job.approved_at, EditorDocument.revision,
                           EditorDocument.current_segments).join(EditorDocument, EditorDocument.job_id == Job.job_id).filter(Job.job_id.in_(expected)).all()
        rows = []
        for jid, status, approved, revision, segments in records:
            sha = hashlib.sha256(json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            rows.append({'job_id': jid, 'revision': revision, 'segments_sha256': sha,
                         'source_unchanged': (revision,sha) == expected[jid], 'status': status,
                         'approved_at': approved.isoformat() if approved else None,
                         'locked_lines': sum(bool(s.get('locked') or s.get('operator_locked')) for s in segments)})
        print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
                          'expected_jobs': len(expected), 'observed_jobs': len(rows), 'jobs': rows}))
    finally:
        db.rollback()
        db.close()


if __name__ == '__main__':
    main()
