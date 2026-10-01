"""Freeze a local inspection delivery; never publish to Genly or write sources."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from reviewer_campaign import atomic_json


def export(root, directory):
    if directory.exists():
        raise ValueError('delivery_directory_already_exists')
    campaign = json.loads((root/'campaign-300'/'manifest.json').read_text())
    directory.mkdir(parents=True, mode=0o700)
    (directory/'candidates').mkdir(mode=0o700)
    candidates = []
    for row in campaign['songs']:
        if row['status'] != 'complete':
            continue
        path = root/'campaign-300'/row['job_id']/'candidate.json'
        candidate = json.loads(path.read_text())
        if candidate['source'] != row['source'] or not candidate['residual_qc'].get('complete_audio_coverage_verified'):
            raise ValueError('stale_or_incomplete_candidate')
        frozen_path = directory/'candidates'/f"{row['job_id']}.json"
        atomic_json(frozen_path, candidate)
        candidates.append({'job_id': row['job_id'], 'mode': 'shadow_complete_audio_review',
            'label': f"{row.get('artist','')} — {row.get('title',row['job_id'])}",
            'path': str(frozen_path.resolve())})
    atomic_json(directory/'manifest.json', {'schema':'reviewer-inspection-delivery-v1',
        'campaign_id': campaign['campaign_id'], 'snapshot_sha256': campaign['snapshot_sha256'],
        'counts':campaign['counts'], 'spend':campaign.get('spend'),
        'candidates':candidates, 'songs':campaign['songs'],
        'operationally_published':False, 'human_accuracy_or_time_savings_demonstrated':False})
    subprocess.run([sys.executable, str(Path(__file__).with_name('build_full_candidate_preview.py')),
                    '--directory', str(directory)], check=True)
    print(json.dumps({'delivery':str(directory), 'counts':campaign['counts'],
                      'full_review_candidates':len(candidates), 'operationally_published':False}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--directory',type=Path,required=True)
    args=parser.parse_args()
    export(args.root,args.directory)
