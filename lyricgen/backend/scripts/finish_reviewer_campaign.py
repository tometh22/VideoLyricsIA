"""Bounded completion pass, after the primary campaign owner has exited.

Reuses successful evidence, retries each known malformed response once through
the existing runner, then subdivides only still-uncovered known failed windows.
Never retries unknown completion or changes the frozen acoustic/selection model.
"""
import argparse
import json
from pathlib import Path

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import atomic_json
from scripts.run_reviewer_campaign import covered, run
from scripts.recover_campaign_audio_windows import recover
from scripts.watch_reviewer_campaign_budget import tick
from shadow_reference_import import digest


def recovery_plan(root, snapshot, manifest, index):
    jobs = {s['job_id']: s for s in snapshot['jobs']}
    plan = []
    for row in manifest['songs']:
        if row['status'] == 'complete' or not jobs[row['job_id']]['segments']:
            continue
        song = jobs[row['job_id']]
        cached = cached_receipts(song, index=index)
        seen = set()
        folder = root / 'campaign-300' / row['job_id']
        # Prefer the latest known retry, but only ONE subdivision per parent.
        paths = list((folder / 'retry-1' / 'requests').glob('*.json'))
        paths += list((folder / 'requests').glob('*.json'))
        for path in paths:
            if path.name.endswith('.attempt.json'):
                continue
            failed = json.loads(path.read_text())
            window = failed.get('window', {})
            if (failed.get('provider') != 'google'
                or failed.get('tool_status') != 'invalid_response'
                or failed.get('received_audio') is not True
                or failed.get('source') != row['source']
                or failed.get('prompt_version') != 'blind-vocal-events-shadow-v2-bounded-schema'
                or not 18 < window.get('end', 0) - window.get('start', 0) <= 24):
                continue
            key = digest(window)
            if key in seen:
                continue
            seen.add(key)
            if covered(cached['receipts'], 'google/gemini-2.5-flash-audio', window):
                continue
            # Existing attempt files/receipts are a durable no-repeat boundary,
            # even if a prior process died before producing its report.
            existing = folder / 'bounded-recovery' / key
            if existing.exists() and any(existing.rglob('*.json')):
                continue
            plan.append({'job_id': row['job_id'], 'failed': str(path), 'window': window})
    return plan


def finish(root, snapshot_path, authorization_path, *, recovery_only=False):
    folder = root / 'campaign-300'
    snapshot = json.loads(snapshot_path.read_text())
    if tick(root, authorization_path)['exceeds_budget']:
        run(root, snapshot_path, authorization_path=authorization_path, local_only=True)
        return
    # This pass has one durable known-response retry, never an unbounded loop.
    if not recovery_only:
        run(root, snapshot_path, authorization_path=authorization_path)
    manifest = json.loads((folder / 'manifest.json').read_text())
    plan = recovery_plan(root, snapshot, manifest, request_index(root))
    atomic_json(folder / 'bounded-completion-plan.json', {
        'schema': 'reviewer-bounded-completion-v1', 'plan': plan,
        'automatic_apply_allowed': False, 'maximum_subdivisions_per_parent': 1})
    for item in plan:
        if tick(root, authorization_path)['exceeds_budget']:
            break
        recover(root, snapshot, item['job_id'], Path(item['failed']))
    # Local-only reconciliation records actual coverage, including unresolved
    # failures. It cannot purchase another retry or certify unchanged text.
    run(root, snapshot_path, authorization_path=authorization_path, local_only=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--authorization', type=Path, required=True)
    parser.add_argument('--recovery-only', action='store_true',
                        help='Skip the normal runner; repair only existing known failed windows')
    args = parser.parse_args()
    finish(args.root, args.snapshot, args.authorization, recovery_only=args.recovery_only)
