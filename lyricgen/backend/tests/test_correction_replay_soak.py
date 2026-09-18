"""Bounded local replay soak; not a production load or media-worker benchmark.

25 waves of 32 real HTTP/PG commands share one saved correction. Some callers
discard their first response (transport outcome unknown) and retry. We assert
persisted content/revision, acknowledgement identity and pool recovery, not just
200 counts. Seeds/workload are deterministic; no providers or remote storage.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic

import pytest

from database import EditorDocument, engine
from tests.conftest import auth
from tests.test_change_request_preview_cas import proposal_case, _body
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds


def test_bounded_replay_soak_preserves_one_revision_and_releases_connections(
    client, admin_token, proposal_case, db, record_property,
):
    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Replay/load proof requires disposable PostgreSQL')
    path, proposal, job_id = proposal_case
    body, headers = _body(proposal), auth(admin_token)
    db.rollback()
    checked_out_before = engine.pool.checkedout()
    durations, responses, discarded = [], [], []
    started = monotonic()
    with ThreadPoolExecutor(max_workers=32) as pool:
        for wave in range(25):
            barrier = Barrier(32, timeout=15)

            def submit(index):
                barrier.wait()
                then = monotonic()
                response = client.post(path + '/apply', headers=headers, json=body)
                # Deterministic response loss after the server has completed:
                # the operator cannot inspect its payload before retrying.
                lost = (wave * 32 + index) % 7 == 0
                first = response
                if lost:
                    response = client.post(path + '/apply', headers=headers, json=body)
                return response, monotonic() - then, first if lost else None

            for response, elapsed, lost_response in pool.map(submit, range(32)):
                responses.append(response)
                durations.append(elapsed)
                if lost_response is not None:
                    discarded.append(lost_response)

    assert all(row.status_code == 200 for row in responses + discarded)
    assert all(row.json()['revision'] == 1 for row in responses)
    assert sum(row.json().get('applied') is True for row in responses + discarded) == 1
    assert engine.pool.checkedout() <= checked_out_before
    db.expire_all()
    doc = db.query(EditorDocument).filter_by(job_id=job_id).one()
    assert doc.revision == 1
    assert [row['text'] for row in doc.current_segments] == ['Hola, mundo!']
    elapsed = monotonic() - started
    record_property('concurrency', 32)
    record_property('waves', 25)
    record_property('requests', len(responses) + len(discarded))
    record_property('seed', 'deterministic-modulo-7')
    record_property('elapsed_seconds', round(elapsed, 4))
    record_property('request_p95_seconds', round(sorted(durations)[int(len(durations) * .95)], 4))
    # An intentionally generous deadlock/leak guard, not a deployment SLO.
    assert elapsed < 120
