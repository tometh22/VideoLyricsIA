"""Opt-in pytest partitioning; each CI runner has its own PostgreSQL service."""
import json
import os
from pathlib import Path

import pytest
from ci.shards import owner


def pytest_addoption(parser):
    group = parser.getgroup('ci-shard')
    group.addoption('--ci-shard-count', type=int, default=1)
    group.addoption('--ci-shard-index', type=int, default=0)
    group.addoption('--ci-shard-report', required=True)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # Run after pytest's normal -m/-k deselection. No new skip markers.
    count, index = config.getoption('ci_shard_count'), config.getoption('ci_shard_index')
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError('Invalid CI shard index/count')
    inventory = sorted(item.nodeid for item in items)
    selected = [item for item in items if owner(item.nodeid, count) == index]
    rejected = [item for item in items if owner(item.nodeid, count) != index]
    report = Path(config.getoption('ci_shard_report'))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'index': index, 'count': count,
        'commit': os.environ.get('GITHUB_SHA', 'local'),
        'collected': inventory, 'selected': sorted(item.nodeid for item in selected)}, indent=2))
    if rejected:
        config.hook.pytest_deselected(items=rejected)
    items[:] = selected
