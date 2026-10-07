#!/usr/bin/env python3
"""Offline development evaluation of frozen approved editor snapshots.

Inputs: a private snapshot directory containing selection.json, JOB.json
and JOB/{audio.*, metadata.json, ground_truth.json, baseline_output.json}.
An optional current_output.json is scored separately from historical output.
Targets never enter inference. Approval is an operational comparator, not an
independent acoustic annotation or a claim of held-out model generalization.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_benchmark import _aoo, _monotonic_alignment, _normalise_text, _recall, _wer


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def lead_rows(rows, *, inline_backing=False):
    result = deepcopy(rows)
    if inline_backing:
        for row in result:
            row['text'] = re.sub(r'\([^()]*\)', '', row.get('text', '')).strip()
    return [row for row in result if _normalise_text(row.get('text', ''))]


def evaluate(target, candidate, *, inline_backing=False):
    for rows in (target, candidate):
        for row in rows:
            start, end = row.get('start'), row.get('end')
            if (not isinstance(row.get('text'), str)
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               and math.isfinite(v) for v in (start, end))
                    or not 0 <= start < end):
                raise ValueError('invalid_segment')
    if not target or not any(_normalise_text(row['text']) for row in target):
        raise ValueError('empty_approved_target')
    ground = lead_rows(target, inline_backing=inline_backing)
    output = lead_rows(candidate, inline_backing=inline_backing)
    g = sorted(ground, key=lambda row: row['start'])
    o = sorted(output, key=lambda row: row['start'])
    onset_mean, onset_p95, matched = _aoo(g, o)
    ends = []
    for g0, g1, o0, o1 in _monotonic_alignment(g, o):
        if g1 - g0 > 1:
            ends.extend(abs(o[o1 - 1]['end'] - g[i]['end']) for i in range(g0, g1))
        elif o1 - o0 > 1:
            ends.extend(abs(o[i]['end'] - g[g0]['end']) for i in range(o0, o1))
        else:
            ends.append(abs(o[o0]['end'] - g[g0]['end']))
    return {
        'full_text_wer': _wer(target, candidate),
        'lead_text_wer': _wer(g, o) if g else None,
        'lead_occurrence_recall': _recall(g, o) if g else None,
        'lead_matched_rows': matched, 'lead_target_rows': len(g),
        'candidate_rows': len(candidate),
        # No matches is unknown timing, never a misleading perfect zero.
        'display_onset_mean_s': onset_mean if matched else None,
        'display_onset_p95_s': onset_p95 if matched else None,
        'display_end_mean_s': mean(ends) if ends else None,
        'target_inline_backing_entries': (
            sum(len(re.findall(r'\([^()]+\)', row['text'])) for row in target)
            if inline_backing else None),
        'independent_backing_timing_scored': False,
    }


def validate_case(root, job_id):
    snapshot_path = root / (job_id + '.json')
    snapshot = json.loads(snapshot_path.read_text())
    case = root / job_id
    metadata = json.loads((case / 'metadata.json').read_text())
    target = json.loads((case / 'ground_truth.json').read_text())
    version = next((v for v in snapshot['versions']
                    if v['id'] == snapshot['approved_version_id']), None)
    if (snapshot.get('job_id') != job_id or metadata.get('job_id') != job_id
            or metadata.get('audio_revision') != snapshot.get('audio_revision')
            or version is None or version['is_approved'] is not True
            or version['revision'] != snapshot['revision']
            or target != version['segments'] or target != snapshot['segments']
            or metadata['approved_version_id'] != version['id']
            or metadata['approved_revision'] != version['revision']):
        raise ValueError('approved_snapshot_mismatch:' + job_id)
    if file_hash(snapshot_path) != metadata['source_snapshot_sha256']:
        raise ValueError('snapshot_hash_mismatch:' + job_id)
    audio_file = metadata['audio_file']
    if Path(audio_file).name != audio_file:
        raise ValueError('invalid_audio_filename:' + job_id)
    audio_hash = file_hash(case / audio_file)
    if not audio_hash or audio_hash != metadata['audio_sha256'] or audio_hash != snapshot['input_audio_sha256']:
        raise ValueError('audio_hash_mismatch:' + job_id)
    return case, snapshot, metadata, target


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    jobs = json.loads((args.snapshot_dir / 'selection.json').read_text())['job_ids']
    rows = []
    for job_id in jobs:
        if not re.fullmatch(r'[a-f0-9]{12}', job_id):
            raise ValueError('invalid_job_id')
        case, snapshot, metadata, target = validate_case(args.snapshot_dir, job_id)
        result = {'job_id': job_id, 'artist': snapshot['artist'], 'title': snapshot['song_title'],
                  'approved_revision': metadata['approved_revision'], 'audio_sha256': metadata['audio_sha256'],
                  'target_sha256': file_hash(case / 'ground_truth.json')}
        for name in ('baseline', 'current'):
            path = case / (name + '_output.json')
            if not path.exists():
                continue
            bundle = json.loads(path.read_text())
            if name == 'current':
                run = bundle.get('meta') or {}
                if (run.get('human_targets_hidden_during_inference') is not True
                        or run.get('audio_sha256') != metadata['audio_sha256']
                        or run.get('audio_revision') != metadata['audio_revision']
                        or not run.get('pipeline_release') or not run.get('pipeline_config_fingerprint')):
                    raise ValueError('current_run_provenance_missing:' + job_id)
            elif bundle['segments'] != snapshot['original_segments']:
                raise ValueError('historical_snapshot_mismatch:' + job_id)
            result[name] = {'source': bundle.get('source'), 'meta': bundle.get('meta'),
                            **evaluate(target, bundle['segments'],
                                       inline_backing=metadata.get('inline_parentheses_are_backing') is True)}
        rows.append(result)
    report = {'schema': 'approved-corrections-development-evaluation-v1',
              'purpose': 'development_only', 'independent_holdout': False,
              'automatic_apply_allowed': False, 'jobs': rows}
    private_write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'jobs': len(rows), 'historical_runs': sum('baseline' in x for x in rows),
                      'current_runs': sum('current' in x for x in rows), 'report': str(args.out)}))


if __name__ == '__main__':
    main()
