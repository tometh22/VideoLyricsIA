import os
import unittest
from unittest.mock import patch

from scripts.preflight import campaign_access_smoke as smoke


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status
        self.ok = status == 200
        self.headers = {'Access-Control-Allow-Origin': smoke.ORIGIN}

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, fail_panel=False):
        self.headers = {}
        self.calls = []
        self.closed = False
        self.fail_panel = fail_panel

    def get(self, url, **kwargs):
        assert url == smoke.API + '/health/deploy'
        return Response({'env': 'staging', 'release': 'tested-release'})

    def request(self, method, url, **kwargs):
        path = url.removeprefix(smoke.API)
        self.calls.append((method, path))
        if path == '/auth/login':
            return Response({'token': 'test-token'})
        if path == '/auth/sessions':
            return Response({'sessions': [{'id': 41, 'current': False}, {'id': 42, 'current': True}]})
        if path == '/auth/me':
            return Response({'id': 7, 'role': 'admin'})
        if path == '/auth/sessions/42/revoke':
            return Response({'was_current': True})
        assert path == '/admin/users/7/campaign-access'
        assert method == 'GET'
        if self.fail_panel:
            return Response({}, status=503)
        rows = [] if kwargs.get('params', {}).get('granted_only') else [
            {'id': 'campaign', 'name': 'Existing campaign', 'inherited': 'admin', 'has_access': True, 'granted': False},
        ]
        return Response({'items': rows, 'total': len(rows), 'user_active': True, 'campaigns_enabled': True})

    def close(self):
        self.closed = True


class CampaignAccessSmokeTest(unittest.TestCase):
    def run_smoke(self, session):
        with patch.dict(os.environ, {'EXPECTED_RELEASE': 'tested-release', 'PREFLIGHT_USERNAME': 'test-admin', 'PREFLIGHT_PASSWORD': 'test-only'}):
            with patch.object(smoke.requests, 'Session', return_value=session):
                smoke.main()

    def assert_no_business_writes(self, session):
        self.assertEqual([call for call in session.calls if call[0] != 'GET'], [
            ('POST', '/auth/login'), ('POST', '/auth/sessions/42/revoke'),
        ])
        self.assertTrue(session.closed)

    def test_reads_only_own_admin_panel_and_closes_only_own_session(self):
        session = FakeSession()
        self.run_smoke(session)
        self.assert_no_business_writes(session)
        self.assertEqual(sum(path == '/admin/users/7/campaign-access' for _, path in session.calls), 3)

    def test_closes_own_session_when_panel_check_fails(self):
        session = FakeSession(fail_panel=True)
        with self.assertRaisesRegex(RuntimeError, 'got 503'):
            self.run_smoke(session)
        self.assert_no_business_writes(session)
