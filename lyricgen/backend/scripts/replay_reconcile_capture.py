#!/usr/bin/env python3
"""Replay one prospective capture, with all outbound sockets disabled.

No new model calls, paid jobs, edits, or downloads. Requires the exact captured
backend files and the runtime's evidence-signing configuration for byte-exact
annotation replay. Never export signing configuration in an evidence file.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

CANDIDATE_SHA256 = 'cb5a509292b5bffa1d94218b7fdda9cef856e942651da9c0c1e0d3edc4f87b60'
CANDIDATE_COMMIT = '1cc550b5913f27a7c44cec33e0ad7401583a4c66'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--candidate-file', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raw = Path(args.capture).read_bytes()
    evidence = json.loads(raw)
    trace = evidence.get('capture', {}).get('reconcile_stages') or evidence
    if trace.get('schema') != 'reconcile-stage-capture-v1':
        raise SystemExit('Missing prospective capture')
    candidate = Path(args.candidate_file)
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != CANDIDATE_SHA256:
        raise SystemExit('Frozen candidate checksum mismatch')
    for key, value in trace['configuration'].items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    def no_network(*args, **kwargs):
        raise RuntimeError('offline_replay_network_forbidden')
    socket.socket.connect = no_network
    socket.create_connection = no_network
    sys.path.insert(0, str(Path(args.backend).resolve()))
    from reconcile_replay import compare, verify_code
    from whisperx_reconcile import reconcile
    verify_code(trace, args.backend)
    if evidence.get('schema') == 'machine-transcription-evidence-v3':
        from machine_evidence import validate_machine_evidence
        selected = next(h['events'] for h in evidence['hypotheses_by_family'] if h['role'] == 'selected')
        validate_machine_evidence(evidence, selected)
    spec = importlib.util.spec_from_file_location('frozen_reconcile_candidate', candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        report = compare(trace, reconcile, module.reconcile)
    except ValueError as exc:
        report = {'status': 'blocked_replay', 'reason': str(exc), 'quality_verdict': 'not_evaluated'}
    report.update({
        'capture_sha256': hashlib.sha256(raw).hexdigest(),
        'candidate_commit': CANDIDATE_COMMIT, 'candidate_sha256': CANDIDATE_SHA256,
        'job_id': trace['job_id'], 'cohort': trace['cohort'], 'ordinal': trace['ordinal'],
        'historical_incident_reproduction': False,
    })
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'status': report['status'], 'job_id': trace['job_id'], 'output': args.output}))
    return 0 if report['status'] == 'stage_comparison_complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
