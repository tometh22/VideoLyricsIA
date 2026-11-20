import os
import io
import unittest
from contextlib import redirect_stdout
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
    def __init__(self, fail_panel=False, fail_path=None, timeout_path=None, session_rows=None,
                 revoke_current=True, runtime_match=True, release="tested-release",
                 degraded_reasons=None):
        self.headers = {}
        self.calls = []
        self.closed = False
        self.fail_panel = fail_panel
        self.fail_path = fail_path
        self.timeout_path = timeout_path
        self.session_rows = session_rows
        self.revoke_current = revoke_current
        self.runtime_match = runtime_match
        self.release = release
        self.degraded_reasons = degraded_reasons or []

    def get(self, url, **kwargs):
        assert url == smoke.API + '/health/ready'
        return Response({
            'env': 'staging', 'release': self.release, 'fleet_release_match': True,
            'quality_gates': {'fleet_runtime_token_match': self.runtime_match},
            'degraded_reasons': self.degraded_reasons,
        })

    def request(self, method, url, **kwargs):
        path = url.removeprefix(smoke.API)
        self.calls.append((method, path))
        if path == self.timeout_path:
            raise smoke.requests.Timeout('simulated request timeout')
        if path == self.fail_path:
            return Response({}, status=503)
        if path == '/auth/login':
            return Response({'token': 'test-token'})
        if path == '/auth/sessions':
            rows = self.session_rows if self.session_rows is not None else [
                {'id': 41, 'current': False}, {'id': 42, 'current': True},
            ]
            return Response({'sessions': rows})
        if path == '/auth/me':
            return Response({'id': 7, 'role': 'admin'})
        if path == '/auth/sessions/42/revoke':
            return Response({'was_current': self.revoke_current})
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

    def test_auth_failures_report_unverified_revocation_without_revoking_other_sessions(self):
        cases = [
            FakeSession(fail_path='/auth/login'),
            FakeSession(fail_path='/auth/sessions'),
            FakeSession(timeout_path='/auth/login'),
            FakeSession(timeout_path='/auth/sessions'),
            FakeSession(session_rows=[]),
            FakeSession(session_rows=[{'id': 41, 'current': False}]),
            FakeSession(session_rows=[{'id': 41, 'current': True}, {'id': 42, 'current': True}]),
            FakeSession(session_rows=[{'current': True}]),
            FakeSession(session_rows=[{'id': None, 'current': True}]),
        ]
        for session in cases:
            with self.subTest(session=session):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises((RuntimeError, AssertionError, KeyError, smoke.requests.Timeout)):
                    self.run_smoke(session)
                self.assertIn('Session revocation NOT verified', output.getvalue())
                self.assertNotIn('Closed only', output.getvalue())
                self.assertEqual([c for c in session.calls if c[0] != 'GET'], [('POST', '/auth/login')])
                self.assertTrue(session.closed)

    def test_revocation_failures_never_claim_success_and_close_http_client(self):
        for session in [FakeSession(fail_path='/auth/sessions/42/revoke'), FakeSession(revoke_current=False)]:
            with self.subTest(session=session):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises((RuntimeError, AssertionError)):
                    self.run_smoke(session)
                self.assertIn('Session revocation NOT verified', output.getvalue())
                self.assertNotIn('Closed only', output.getvalue())
                self.assert_no_business_writes(session)

    def test_unready_release_or_runtime_fails_without_authentication_or_skip(self):
        for session in [
            FakeSession(runtime_match=False), FakeSession(release='older-release'),
            FakeSession(degraded_reasons=['fleet_runtime_token_mismatch']),
        ]:
            with self.subTest(session=session), patch.object(smoke.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'fleet runtime are not ready'):
                    self.run_smoke(session)
                self.assertEqual(session.calls, [])
                self.assertTrue(session.closed)
