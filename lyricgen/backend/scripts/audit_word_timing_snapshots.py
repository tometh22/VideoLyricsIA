"""Read-only development audit. Never imports provider clients or opens a DB."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timing_validation import diagnose


def audit(path):
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    baseline = snapshot['versions'][0]['segments']
    findings = diagnose(baseline)
    # Same snapshot's documentary control lines: untouched timing does not
    # certify acoustic correctness. No use of these controls to fit thresholds.
    human = {str(s.get('_id')): s for s in snapshot['document']['current_segments']}
    unchanged = [i for i, s in enumerate(baseline)
                 if str(i) in human and all(s[k] == human[str(i)][k] for k in ('start','end','text'))]
    flagged = {f['segment_index'] for f in findings}
    return {'job_id': snapshot['job']['job_id'], 'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
            'baseline_line_count': len(baseline), 'findings': findings,
            'documentary_unchanged_controls': unchanged,
            'flagged_documentary_controls': sorted(flagged.intersection(unchanged)),
            'controls_are_acoustically_certified': False, 'repairs': 0,
            'snapshot_unchanged': path.read_bytes() == raw}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = {'schema': 'word-timing-snapshot-audit-v1', 'songs': [audit(p) for p in args.snapshot]}
    with args.output.open('x') as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result, indent=2))
