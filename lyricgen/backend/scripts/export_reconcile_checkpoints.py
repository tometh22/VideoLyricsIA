#!/usr/bin/env python3
"""Read only the six reserved private checkpoints; never query human documents.

Run inside the authorized staging environment or import collect() through the
existing SSH export transport. Keep exports private. Incomplete checkpoints are
retained as outcomes and cannot pass the full-document replay gate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path


def collect(cohort):
    from redis import Redis
    if os.environ.get('ENVIRONMENT') != 'staging' or not cohort or len(cohort) > 128:
        raise ValueError('explicit_staging_cohort_required')
    key = 'reconcile-capture:' + hashlib.sha256(cohort.encode()).hexdigest()
    client = Redis.from_url(os.environ['REDIS_URL'], decode_responses=True,
                            socket_connect_timeout=1, socket_timeout=2)
    try:
        ledger = client.hgetall(key + ':window')
        members = {int(value): name[4:] for name, value in ledger.items() if name.startswith('job:')}
        count = int(ledger.get('count', 0))
        if (not 0 <= count <= 6 or set(members) != set(range(1, count + 1))
                or len(members) != sum(name.startswith('job:') for name in ledger)):
            raise ValueError('invalid_admission_ledger')
        slots = []
        for ordinal in range(1, 7):
            job_id = members.get(ordinal)
            raw = client.get(key + ':result:' + job_id) if job_id else None
            slot = {'ordinal': ordinal, 'job_id': job_id,
                    'state': 'missing_checkpoint' if job_id else 'not_admitted'}
            if raw:
                if len(raw.encode()) > 4 * 1024 * 1024:
                    raise ValueError('checkpoint_byte_limit')
                envelope = json.loads(raw)
                trace = envelope['trace']
                digest = hashlib.sha256(json.dumps(trace, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()).hexdigest()
                if (digest != envelope['trace_sha256'] or trace['job_id'] != job_id
                        or trace['ordinal'] != ordinal or trace['cohort'] != cohort
                        or envelope['attempt_id'] != trace['admission']['attempt_id']):
                    raise ValueError('checkpoint_identity_mismatch')
                slot.update(state='complete' if envelope['complete'] else 'incomplete', checkpoint=envelope)
            slots.append(slot)
        return {'cohort': cohort, 'ledger': ledger, 'slots': slots,
                'candidate_executed': False, 'human_documents_read': False}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # Exclusive creation: a later export never destroys prior outcomes.
    payload = collect(args.cohort)
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
