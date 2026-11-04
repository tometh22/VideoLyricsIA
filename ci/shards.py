"""Deterministic file-level partitions and a fail-closed coverage gate."""
import argparse
import hashlib
import json
from pathlib import Path


def owner(nodeid, count):
    # Keep module/session fixtures and all parameter variants together.
    filename = nodeid.split('::', 1)[0]
    return int.from_bytes(hashlib.sha256(filename.encode()).digest()[:8], 'big') % count


def verify(directory, count, commit):
    reports = [json.loads(p.read_text()) for p in Path(directory).glob('shard-*.json')]
    if len(reports) != count or {r['index'] for r in reports} != set(range(count)):
        raise ValueError('Missing or repeated shard reports')
    reference = reports[0]['collected']
    if not reference or len(reference) != len(set(reference)):
        raise ValueError('Empty or duplicate collection')
    observed = []
    for report in reports:
        if report['count'] != count or report['commit'] != commit:
            raise ValueError('Shard count or commit mismatch')
        if report['collected'] != reference:
            raise ValueError('Shards collected different test inventories')
        expected = [n for n in reference if owner(n, count) == report['index']]
        if not expected or report['selected'] != expected:
            raise ValueError('Shard omitted, duplicated or selected foreign tests')
        observed.extend(report['selected'])
    if len(observed) != len(set(observed)) or set(observed) != set(reference):
        raise ValueError('Combined shards do not cover the exact collection')
    return len(reference)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    total = verify(args.directory, args.count, args.commit)
    print(f'All {total} collected tests assigned exactly once across {args.count} successful shards.')
