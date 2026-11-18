"""Worker transport, privacy and lease tests; no network, database, or models."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from genly_model_worker import LeaseHeartbeat, StagingClient, WorkerError, download_original, run_claim
from local_genly_review import normalize_snapshot
from reviewer_shadow import source_binding


class Response:
    def __init__(self, chunks=(), status=200):
        self.chunks, self.status_code = chunks, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        yield from self.chunks


class Heartbeat:
    def __init__(self, *args, **kwargs):
        self.lost = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = b"fixture audio bytes"
        self.sha = hashlib.sha256(self.audio).hexdigest()
        self.destination = self.root / "audio" / self.sha
        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps({"token": "test-only-token"}))
        self.auth.chmod(0o600)
        self.song = normalize_snapshot({"job_id": "job-1", "revision": 1, "audio_revision": 1,
                        "audio_sha256": self.sha, "segments": [{"text": "hello", "start": 0., "end": 2.}]})
        self.claim = {"job_id": "job-1", "run_id": "run-1", "lease_token": "test-lease", "source": source_binding(self.song)}

    def test_private_auth_and_known_origin(self):
        for url in ("http://api-staging-9b82.up.railway.app", "https://attacker.invalid",
                    "https://api-staging-9b82.up.railway.app.attacker.invalid"):
            with self.assertRaisesRegex(ValueError, "known_staging_api_required"):
                StagingClient(url, self.auth)
        self.auth.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "auth_file_must_be_private"):
            StagingClient("https://api-staging-9b82.up.railway.app", self.auth)

    def test_rejects_credentials_ports_paths_and_queries_in_api_origin(self):
        origin = "https://api-staging-9b82.up.railway.app"
        for url in (origin + "/other", origin + "?redirect=elsewhere", origin + "#fragment",
                    origin + ":8443", "https://name:password@api-staging-9b82.up.railway.app"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "known_staging_api_required"):
                StagingClient(url, self.auth)

    def test_api_never_follows_redirect_or_accepts_absolute_route(self):
        with patch("genly_model_worker.requests.Session") as factory:
            session = factory.return_value
            response = session.request.return_value
            response.status_code = 302
            client = StagingClient("https://api-staging-9b82.up.railway.app", self.auth)
            with self.assertRaisesRegex(WorkerError, "api_status_302"):
                client.request("GET", "/editor/job")
            self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
            for path in ("https://elsewhere.invalid", "//elsewhere.invalid"):
                with self.assertRaisesRegex(WorkerError, "relative_api_path_required"):
                    client.request("GET", path)

    def test_download_is_private_unauthenticated_and_hash_verified(self):
        get = Mock(return_value=Response([self.audio]))
        result = download_original("https://storage.example/audio", self.sha, self.destination, get=get)
        self.assertEqual(result.read_bytes(), self.audio)
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("headers", get.call_args.kwargs)
        self.assertNotIn("auth", get.call_args.kwargs)
        download_original("https://storage.example/audio", self.sha, self.destination, get=get)
        self.assertEqual(get.call_count, 1)

    def test_storage_session_disables_netrc_and_closes(self):
        with patch("genly_model_worker.requests.Session") as factory:
            session = factory.return_value
            session.get.return_value = Response([self.audio])
            download_original("https://storage.example/audio", self.sha, self.destination)
        self.assertFalse(session.trust_env)
        session.headers.update.assert_not_called()
        session.close.assert_called_once()

    def test_bad_hash_oversize_redirect_leave_no_download(self):
        cases = [(Response([b"wrong"]), 100, "audio_hash_mismatch"),
                 (Response([self.audio]), 2, "audio_download_limit"),
                 (Response([], 302), 100, "audio_download_failed")]
        for response, maximum, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(WorkerError, message):
                download_original("https://storage.example/audio", self.sha, self.destination,
                                  get=Mock(return_value=response), maximum_bytes=maximum)
            self.assertFalse(self.destination.exists())
            self.assertFalse(self.destination.with_suffix(".part").exists())

    def test_mutated_cached_audio_rejected_without_network(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"modified")
        get = Mock()
        with self.assertRaisesRegex(WorkerError, "cached_audio_hash_mismatch"):
            download_original("https://storage.example/audio", self.sha, self.destination, get=get)
        get.assert_not_called()

    def test_crashed_partial_download_does_not_block_retry(self):
        self.destination.parent.mkdir()
        abandoned = self.destination.with_suffix(".part")
        abandoned.write_bytes(b"abandoned prior download")
        result = download_original("https://storage.example/audio", self.sha, self.destination,
                                   get=Mock(return_value=Response([self.audio])))
        self.assertEqual(result.read_bytes(), self.audio)
        self.assertEqual(abandoned.read_bytes(), b"abandoned prior download")

    def test_heartbeat_stops_after_lease_rejection(self):
        client = Mock()
        client.request.side_effect = WorkerError("api_status_409")
        heartbeat = LeaseHeartbeat(client, "worker", self.claim, interval=.001)
        with heartbeat:
            self.assertTrue(heartbeat.lost.wait(timeout=1))
        self.assertEqual(client.request.call_count, 1)

    def client(self):
        client = Mock()
        client.request.side_effect = [
            {"snapshot": self.song, "source_audio_path": "/jobs/job-1/source-audio-url?prefer_original=1"},
            {"source": "input", "fallback": False, "url": "https://storage.example/audio"},
            {"published": True}]
        return client

    def process(self, command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "report.json").write_text(json.dumps({"source": self.claim["source"],
                                                       "status": "complete", "model_id": "test-model"}))
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        return process

    def run_model_claim(self, client):
        return run_claim(client, self.claim, worker_id="worker", output_root=self.root / "runs",
                         selector=self.root / "selector.json", adapter=None, model="base", device="cpu")

    def test_source_changes_stop_before_download_or_inference(self):
        client = self.client()
        changed = json.loads(json.dumps(self.song))
        changed["audio_revision"] = 2
        client.request.side_effect = [{"snapshot": changed}]
        with patch("genly_model_worker.LeaseHeartbeat", Heartbeat), patch("genly_model_worker.download_original") as download, \
                patch("genly_model_worker.subprocess.Popen") as process:
            with self.assertRaisesRegex(WorkerError, "source_changed_before_processing"):
                self.run_model_claim(client)
        download.assert_not_called()
        process.assert_not_called()

    def test_lease_lost_during_download_does_not_launch_model(self):
        heartbeat = Heartbeat()
        def downloaded(*args, **kwargs):
            heartbeat.lost.set()
            return self.destination
        with patch("genly_model_worker.LeaseHeartbeat", return_value=heartbeat), \
                patch("genly_model_worker.download_original", side_effect=downloaded), \
                patch("genly_model_worker.subprocess.Popen") as process:
            with self.assertRaisesRegex(WorkerError, "worker_lease_lost"):
                self.run_model_claim(self.client())
        process.assert_not_called()

    def test_inference_failure_never_uploads_candidate(self):
        client = self.client()
        process = Mock()
        process.poll.return_value = 1
        process.returncode = 1
        with patch("genly_model_worker.LeaseHeartbeat", Heartbeat), \
                patch("genly_model_worker.download_original", return_value=self.destination), \
                patch("genly_model_worker.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(WorkerError, "local_inference_failed"):
                self.run_model_claim(client)
        self.assertEqual(client.request.call_count, 2)

    def test_timeout_terminates_child_without_publication(self):
        client = self.client()
        process = Mock()
        process.poll.return_value = None
        with patch("genly_model_worker.LeaseHeartbeat", Heartbeat), \
                patch("genly_model_worker.download_original", return_value=self.destination), \
                patch("genly_model_worker.subprocess.Popen", return_value=process), \
                patch("genly_model_worker.time.monotonic", side_effect=[0, 2000]):
            with self.assertRaisesRegex(WorkerError, "inference_timeout"):
                self.run_model_claim(client)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)
        self.assertEqual(client.request.call_count, 2)

    def test_reclaimed_run_uses_new_attempt_and_uploads_bound_result(self):
        old = self.root / "runs" / self.claim["run_id"]
        old.mkdir(parents=True)
        (old / "previous-attempt.txt").write_text("preserve")
        client = self.client()
        with patch("genly_model_worker.LeaseHeartbeat", Heartbeat), \
                patch("genly_model_worker.download_original", return_value=self.destination), \
                patch("genly_model_worker.subprocess.Popen", side_effect=self.process):
            result = self.run_model_claim(client)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual((old / "previous-attempt.txt").read_text(), "preserve")
        call = client.request.call_args
        self.assertEqual(call.args[:2], ("POST", "/editor/job-1/genly-model/candidate"))
        self.assertEqual(call.kwargs["headers"]["X-Genly-Lease-Token"], "test-lease")
        self.assertEqual(call.kwargs["payload"]["source"], self.claim["source"])


if __name__ == "__main__":
    unittest.main()
