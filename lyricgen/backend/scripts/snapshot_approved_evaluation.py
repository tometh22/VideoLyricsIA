#!/usr/bin/env python3
"""Freeze current approved revisions for private development evaluation.

Two phases keep DB and storage credentials separate:
  python scripts/snapshot_approved_evaluation.py --out DIR --job-id ID --latest 8
  python scripts/snapshot_approved_evaluation.py --out DIR --download-audio

The first phase uses DATABASE_PUBLIC_URL (override with --database-env), a
read-only transaction and no product writes. The second uses the ordinary
R2 environment credentials. Output contains private lyrics/audio; keep it
outside git. Approval alone is not an independently verified gold label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot(root, preferred, latest, database_env):
    from sqlalchemy import case, func, select, text
    from sqlalchemy.orm import aliased
    os.environ['DATABASE_URL'] = os.environ[database_env]
    from database import EditorDocument, EditorVersion, Job, SessionLocal
    other = aliased(EditorVersion)
    count = select(func.count(other.id)).where(other.job_id == Job.job_id).scalar_subquery()
    with SessionLocal() as db:
        if db.bind.dialect.name != 'postgresql':
            raise ValueError('snapshot requires a read-only PostgreSQL connection')
        db.execute(text('SET TRANSACTION READ ONLY'))
        db.execute(text("SET LOCAL statement_timeout = '60s'"))
        query = db.query(Job, EditorDocument, EditorVersion).join(
            EditorDocument, EditorDocument.job_id == Job.job_id,
        ).join(EditorVersion, (EditorVersion.job_id == Job.job_id)
               & (EditorVersion.revision == EditorDocument.revision)).filter(
            EditorVersion.is_approved.is_(True), Job.pilot_id.is_(None),
            Job.input_r2_key.isnot(None), Job.input_audio_sha256.isnot(None),
            Job.segments_revision == EditorDocument.revision, count >= 3,
            EditorVersion.segments == EditorDocument.current_segments,
            EditorVersion.segments == Job.segments_json,
        )
        if preferred:
            query = query.order_by(case({j: i for i, j in enumerate(preferred)},
                                        value=Job.job_id, else_=len(preferred)))
        rows = query.order_by(EditorVersion.created_at.desc()).limit(latest).all()
        selected = []
        for job, doc, version in rows:
            if not re.fullmatch(r'[a-f0-9]{12}', job.job_id):
                continue
            if (not version.segments or version.segments != doc.current_segments
                    or version.segments != job.segments_json):
                continue
            first = db.query(EditorVersion).filter_by(job_id=job.job_id).order_by(
                EditorVersion.revision).first()
            versions = list({v.id: v for v in (first, version) if v}.values())
            data = {k: getattr(job, k) for k in ('job_id', 'artist', 'song_title', 'filename',
                'input_r2_key', 'input_audio_sha256', 'audio_revision', 'workload_class')}
            data.update(revision=version.revision, approved_version_id=version.id,
                        segments=version.segments, original_segments=doc.original_segments,
                        versions=[{k: getattr(v, k) for k in ('id', 'revision', 'is_approved',
                            'reason', 'segments', 'created_at', 'created_by')} for v in versions])
            write_private(root / (job.job_id + '.json'), data)
            selected.append(job.job_id)
        missing = set(preferred) - set(selected)
        if missing:
            raise ValueError('requested jobs are not eligible current approvals: ' + ','.join(sorted(missing)))
        write_private(root / 'selection.json', {'job_ids': selected})
    print(json.dumps({'selected': len(selected), 'read_only': True}))


def download(root):
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT_URL'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4', connect_timeout=15, read_timeout=120))
    for job_id in json.loads((root / 'selection.json').read_text())['job_ids']:
        if not re.fullmatch(r'[a-f0-9]{12}', job_id):
            raise ValueError('invalid job id')
        source = root / (job_id + '.json')
        job = json.loads(source.read_text())
        directory = root / job_id
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        audio = directory / ('audio' + (Path(job['input_r2_key']).suffix or '.wav'))
        if not audio.exists():
            fd = os.open(audio, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                body = client.get_object(Bucket=os.environ['R2_BUCKET'], Key=job['input_r2_key'])['Body']
                with os.fdopen(fd, 'wb') as handle:
                    fd = -1
                    for block in body.iter_chunks(1024 * 1024):
                        handle.write(block)
            except Exception:
                audio.unlink(missing_ok=True)
                raise
            finally:
                if fd >= 0:
                    os.close(fd)
        if sha256(audio) != job['input_audio_sha256']:
            raise ValueError('audio hash mismatch:' + job_id)
        for name, value in [('ground_truth.json', job['segments']), ('baseline_output.json', {
            'segments': job['original_segments'], 'source': 'frozen_pre_human_historical',
            'meta': {'historical': True, 'not_current_engine_run': True}}), ('metadata.json', {
            'job_id': job_id, 'artist': job['artist'], 'song_title': job['song_title'],
            'filename': job.get('filename'), 'workload_class': job.get('workload_class', 'batch'),
            'reference_required': job.get('workload_class', 'batch') == 'batch',
            'approved_version_id': job['approved_version_id'], 'approved_revision': job['revision'],
            'audio_sha256': job['input_audio_sha256'], 'audio_revision': job['audio_revision'],
            'audio_file': audio.name, 'source_snapshot_sha256': sha256(source),
            'label_tier': 'operator_approved_development_comparator',
            'independent_backing_timing_verified': False,
        })]:
            write_private(directory / name, value)
        print(json.dumps({'job_id': job_id, 'revision': job['revision'], 'audio_verified': True}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--job-id', action='append', default=[])
    parser.add_argument('--latest', type=int, default=8)
    parser.add_argument('--database-env', default='DATABASE_PUBLIC_URL')
    parser.add_argument('--download-audio', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.latest <= 100 or len(set(args.job_id)) > args.latest:
        parser.error('latest must be 1..100 and include all preferred jobs')
    if any(not re.fullmatch(r'[a-f0-9]{12}', j) for j in args.job_id):
        parser.error('invalid job id')
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.out, 0o700)
    if args.download_audio:
        download(args.out)
    else:
        if (args.out / 'selection.json').exists():
            parser.error('snapshot already exists; choose a new directory')
        snapshot(args.out, args.job_id, args.latest, args.database_env)


if __name__ == '__main__':
    main()
