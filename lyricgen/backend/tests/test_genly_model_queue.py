import json
import unittest
from unittest.mock import patch

import fakeredis
import genly_model_queue as queue


class ModelQueueTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.patch = patch.object(queue, "_client", self.redis)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.source = {"job_id": "song", "segments_revision": 1}

    def test_request_is_idempotent_and_claim_exclusive(self):
        first = queue.enqueue("song", self.source, 1)
        self.assertEqual(first["run_id"], queue.enqueue("song", self.source, 1)["run_id"])
        claimed = queue.claim("worker1")["request"]
        self.assertEqual(claimed["run_id"], first["run_id"])
        self.assertIsNone(queue.claim("worker2")["request"])
        self.assertNotIn("lease_token", queue.status("song")["request"])

    def test_completion_needs_lease_and_source(self):
        queue.enqueue("song", self.source, 1)
        row = queue.claim("worker1")["request"]
        with self.assertRaisesRegex(ValueError, "lease_lost"):
            queue.finish(row["run_id"], "wrong", status="complete")
        with self.assertRaisesRegex(ValueError, "source_mismatch"):
            queue.verify_claim(row["run_id"], row["lease_token"], job_id="other")
        queue.finish(row["run_id"], row["lease_token"], status="complete")
        self.assertEqual(queue.status("song")["request"]["status"], "complete")
        self.assertIsNone(queue.claim("worker1")["request"])

    def test_expired_lease_is_recoverable_and_old_token_stays_invalid(self):
        queue.enqueue("song", self.source, 1)
        first = queue.claim("worker1")["request"]
        key = queue._run_key(first["run_id"])
        stored = json.loads(self.redis.get(key))
        stored["lease_expires_at"] = 0
        self.redis.set(key, json.dumps(stored))
        self.assertEqual(queue.status("song")["request"]["status"], "worker_unavailable")
        second = queue.claim("worker2")["request"]
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        with self.assertRaisesRegex(ValueError, "lease_lost"):
            queue.heartbeat("worker1", run_id=first["run_id"], lease_token=first["lease_token"])

    def test_heartbeat_and_failure_do_not_expose_raw_errors(self):
        self.assertFalse(queue.worker_available())
        queue.heartbeat("worker1")
        self.assertTrue(queue.worker_available())
        queue.enqueue("song", self.source, 1)
        row = queue.claim("worker1")["request"]
        result = queue.finish(row["run_id"], row["lease_token"], status="failed", reason="secret token private path")
        self.assertEqual(result["failure_reason"], "model_failed")


if __name__ == "__main__":
    unittest.main()
