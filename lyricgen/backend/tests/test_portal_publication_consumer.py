"""Real portal API/PG consumer contract; storage bytes are synthetic, not media.

Deliberately separate from a browser/real-worker E2E claim. Checks both portal
destinations and consumes the returned keys, not merely the Delivery DB row.
"""
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from database import Delivery
from tests.test_deliveries import approved_job
from tests.test_publication_case_binding import fake_r2, _prepare, _publish


@pytest.mark.parametrize('portal', ['argentina', 'chile'])
def test_portal_serves_A_until_publish_then_B_with_only_selected_case_closed(
    client, admin_token, db, approved_job, fake_r2, monkeypatch, portal,
):
    import storage
    import queue_jobs

    monkeypatch.setenv('DELIVERY_PORTAL_TOKEN_' + portal.upper(), 'synthetic-consumer-token')
    monkeypatch.setattr(queue_jobs, '_init_redis', lambda: (None, None, None))
    monkeypatch.setattr(storage, '_get_client', lambda: SimpleNamespace(
        head_object=lambda **kw: {'ContentLength': len(fake_r2['objects'][kw['Key']])}))
    monkeypatch.setattr(storage, 'generate_signed_url',
                        lambda key, **kw: 'https://synthetic.invalid/' + key)
    job_id, delivery_id, requests, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    row = db.get(Delivery, delivery_id)
    row.portal_id = portal
    db.commit()
    db.rollback()
    headers = {'X-Portal-Token': 'synthetic-consumer-token', 'X-Portal-Id': portal}

    def read_consumer():
        response = client.get('/api/deliveries/items', headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'private, no-store, max-age=0'
        matches = [v for song in response.json()['songs'] for v in song['versions']
                   if v['delivery_id'] == delivery_id]
        assert len(matches) == 1
        version = matches[0]
        returned_bytes = {}
        for file in version['files']:
            assert file['available'] is True
            returned_bytes[file['type']] = fake_r2['objects'][urlsplit(file['url']).path.lstrip('/')]
        assert len(returned_bytes) == 5
        return version, returned_bytes

    before, old_bytes = read_consumer()
    assert before['revision'] == 1 and before['pending_change_requests'] == 2
    assert all(data.startswith(b'render-A-') for data in old_bytes.values())
    # _prepare already wrote candidate B into the working keys. Neither a new
    # draft nor a portal GET is allowed to change the immutable published A.
    again, same_bytes = read_consumer()
    assert same_bytes == old_bytes and again['revision'] == 1
    response = _publish(client, admin_token, job_id, portal_id=portal, **body)
    assert response.status_code == 200, response.text
    after, new_bytes = read_consumer()
    assert after['revision'] == 2 and after['pending_change_requests'] == 1
    assert all(data.startswith(b'render-B-') for data in new_bytes.values())
    assert set(new_bytes) == set(old_bytes)
    cases = {case['id']: case for case in after['change_requests']}
    assert cases[requests[0]]['resolved_by_revision'] == 2
    assert cases[requests[0]]['resolved_at'] is not None
    assert cases[requests[1]]['resolved_at'] is None
    # Old published URLs still identify A after the swap.
    for file in before['files']:
        assert fake_r2['objects'][urlsplit(file['url']).path.lstrip('/')] == old_bytes[file['type']]
