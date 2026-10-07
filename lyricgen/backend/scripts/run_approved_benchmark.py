#!/usr/bin/env python3
"""Run the real transcription worker against private, hash-pinned audio.

Only inference metadata is read; ground_truth and approval snapshots never
enter this process. Product/database writes stay in a fresh local SQLite DB.
The outbound asynchronous quality queue is suppressed; the ordinary inline
worker postpasses and quality gate run. This measures initial worker output,
not a terminal background repair, held-out quality, or a production release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--run-name', default='current')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{12}', args.job_id):
        parser.error('invalid job id')
    if not 1 <= args.timeout <= 1800:
        parser.error('timeout must be between 1 and 1800 seconds')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', args.run_name):
        parser.error('invalid run name')
    case = args.dataset.resolve() / args.job_id
    destination = case / (args.run_name + '_output.json')
    status_path = case / (args.run_name + '-run-status.json')
    if destination.exists() or status_path.exists():
        parser.error('run already attempted; preserve evidence and use a new dataset for a new run')
    meta = json.loads((case / 'metadata.json').read_text())
    if Path(meta['audio_file']).name != meta['audio_file']:
        parser.error('invalid audio filename')
    audio = case / meta['audio_file']
    digest = hashlib.sha256()
    with audio.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != meta['audio_sha256']:
        parser.error('audio snapshot hash mismatch')
    scratch = case / (args.run_name + '-worker-sandbox')
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, 0o700)
    db_url = 'sqlite:///' + str(scratch / 'worker.db')
    backend = Path(__file__).resolve().parents[1]
    repository = backend.parent.parent
    release = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
    code_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in backend.glob('*.py')}
    os.environ.update(DATABASE_URL=db_url, DELIVERIES_DATABASE_URL=db_url,
                      SENTRY_DSN='', BUSINESS_ALERTS_ENABLED='0', RELEASE=release)
    # Railway's /app credential path does not exist on a local Mac. The
    # ordinary bootstrap decodes the supplied service account privately.
    if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON_B64'):
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(scratch / 'vertex.json')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import main as application
    import queue_jobs
    import storage
    import transcription_worker
    import ops_metrics
    from database import Base, engine, deliveries_engine, SessionLocal, Job
    from transcription_quality import runtime_identity
    assert engine.dialect.name == deliveries_engine.dialect.name == 'sqlite'
    Base.metadata.create_all(engine)
    sandbox_job_id = hashlib.sha256((args.job_id + release + args.run_name + 'worker-initial').encode()).hexdigest()[:12]
    # The input is already local and hash-verified. A local-only pointer
    # avoids uploading a second copy to the shared product input bucket.
    input_key = storage.content_addressed_input_key('local_evaluation', sandbox_job_id,
                                                   meta['audio_sha256'], audio.name)
    with SessionLocal() as db:
        db.add(Job(job_id=sandbox_job_id, user_id=1, tenant_id='local_evaluation',
                   filename=audio.name, artist=meta['artist'], song_title=meta['song_title'],
                   input_r2_key=input_key, input_audio_sha256=meta['audio_sha256'],
                   input_audio_etag=meta['audio_sha256'], audio_revision=meta['audio_revision'],
                   workload_class=meta.get('workload_class', 'batch'), status='queued'))
        db.commit()
    suppressed_queue_calls = []
    queue_jobs.enqueue_transcription_quality = lambda *a, **kw: suppressed_queue_calls.append('quality')
    # Benchmarks must not count as completed product jobs in fleet metrics.
    ops_metrics.increment = lambda *_a, **_kw: None
    # Record the formatter boundary without changing its inputs/outputs.
    # This distinguishes lexical loss from shorter display intervals.
    import copy
    import lyrics_format
    original_formatter = lyrics_format.format_lyrics_pass
    async def recorded_formatter(result, **kwargs):
        before = copy.deepcopy(result)
        after = await original_formatter(result, **kwargs)
        fd = os.open(case / (args.run_name + '-formatter.json'),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'before': before, 'after': after}, handle, ensure_ascii=False,
                      indent=2, default=str)
        return after
    lyrics_format.format_lyrics_pass = recorded_formatter

    def timeout_handler(*_):
        raise TimeoutError('local_evaluation_deadline')

    signal.signal(signal.SIGALRM, timeout_handler)
    started = time.monotonic()
    signal.alarm(args.timeout)
    try:
        result = transcription_worker.run_transcription_job(sandbox_job_id, str(audio),
            language=meta.get('language', 'es'), artist=meta['artist'], title=meta['song_title'],
            filename=meta.get('filename', audio.name), workload_class=meta.get('workload_class', 'batch'),
            reference_required=meta.get('reference_required', True), live=meta.get('live', False))
        signal.alarm(0)
        if not isinstance(result.get('segments'), list) or not result['segments']:
            raise RuntimeError('worker_did_not_return_segments')
        after_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in backend.glob('*.py')}
        if after_hashes != code_hashes:
            raise RuntimeError('backend_code_changed_during_inference')
        with SessionLocal() as db:
            timing_source = db.query(Job).filter_by(job_id=sandbox_job_id).one().timing_source
        result.setdefault('source', timing_source or 'unknown')
        run = {**runtime_identity(), 'audio_sha256': meta['audio_sha256'],
               'environment': application.ENVIRONMENT,
               'audio_revision': meta['audio_revision'], 'human_targets_hidden_during_inference': True,
               'isolation': 'local_sqlite', 'scope': 'worker_initial_output',
               'background_quality_queue_suppressed': True,
               'suppressed_queue_calls': len(suppressed_queue_calls),
               'workload_class': meta.get('workload_class', 'batch'),
               'reference_required': meta.get('reference_required', True),
               'elapsed_s': round(time.monotonic() - started, 2),
               'code_sha256': code_hashes}
        result['meta'] = run
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
        status = {'success': True, 'job_id': args.job_id, 'segments': len(result['segments']),
                  'scope': run['scope'], 'elapsed_s': run['elapsed_s']}
    except Exception as exc:
        signal.alarm(0)
        status = {'success': False, 'job_id': args.job_id, 'error_type': type(exc).__name__,
                  'elapsed_s': round(time.monotonic() - started, 2)}
    status_path.write_text(json.dumps(status)); os.chmod(status_path, 0o600)
    (scratch / 'vertex.json').unlink(missing_ok=True)
    print(json.dumps(status), flush=True)
    return 0 if status['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
