"""P0 #4 — Concurrency stress test.

Submits N upload jobs in parallel against the deployed API and polls each one
to terminal state. The check fails if any job hangs (still in a non-terminal
status when the global deadline expires) or returns a non-acceptable terminal
status.

This catches the failure modes that only show up under load:

  - Worker pool starves: jobs enqueue but nothing pulls them off RQ.
  - Race in job_dir creation: two jobs collide on the same path.
  - DB connection pool exhausted under N parallel writes.
  - Plan/cap accidentally throttles a legitimate batch.
  - Vertex rate-limit kicks in and the cooldown logic doesn't recover.

The test is bounded by `--concurrency-mp3` (provide a real MP3 to use as
input — the same file is uploaded N times) and `--concurrency-n` (default 3).
Cost ≈ N × $0.80 in Veo.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ._base import Check, CheckResult
from ._clients import GenLyClient


TERMINAL_OK = {"done"}
TERMINAL_ACCEPTABLE = {"done", "validation_failed", "pending_review"}
TERMINAL_BAD = {"error", "failed", "upload_failed"}


class ConcurrencyCheck(Check):
    name = "concurrency"
    description = "submit N parallel jobs, monitor to terminal state, fail if any hang"
    p0 = True

    def __init__(
        self,
        api_url: str,
        username: str | None,
        password: str | None,
        mp3_path: str | None,
        concurrency: int = 3,
        timeout_secs: int = 900,
    ):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.mp3_path = mp3_path
        self.concurrency = concurrency
        self.timeout_secs = timeout_secs

    def run(self) -> CheckResult:
        if not (self.username and self.password):
            return self._skipped(
                "PREFLIGHT_USERNAME / PREFLIGHT_PASSWORD not set — cannot "
                "authenticate to deployed API for concurrency test"
            )
        if not self.mp3_path or not os.path.exists(self.mp3_path):
            return self._skipped(
                f"MP3 not found at {self.mp3_path!r} — supply with "
                "--concurrency-mp3 or skip this check"
            )

        client = GenLyClient(self.api_url, self.username, self.password)
        try:
            client.login()
        except Exception as e:
            return self._failed(
                f"login failed: {type(e).__name__}: {e}",
                api_url=self.api_url, username=self.username,
            )

        # Phase 1 — submit N uploads in parallel.
        submit_t0 = time.time()
        submit_results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(client.upload, self.mp3_path, f"preflight-{i}"): i
                for i in range(self.concurrency)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    job_id = future.result()
                    submit_results[i] = {"ok": True, "job_id": job_id}
                except Exception as e:
                    submit_results[i] = {
                        "ok": False, "error": f"{type(e).__name__}: {e}",
                    }
        submit_secs = round(time.time() - submit_t0, 1)

        submit_failures = [r for r in submit_results.values() if not r["ok"]]
        if submit_failures:
            return self._failed(
                f"{len(submit_failures)}/{self.concurrency} uploads failed at "
                "submission — API may be over capacity or rate-limiting",
                submit_results=submit_results,
                submit_secs=submit_secs,
            )

        job_ids = [r["job_id"] for r in submit_results.values()]

        # Phase 2 — poll every job to terminal state, parallel polling so a
        # slow job does not delay the picture for the others.
        deadline = time.time() + self.timeout_secs
        statuses: dict[str, dict] = {jid: {"status": "submitted"} for jid in job_ids}
        last_seen: dict[str, str] = {}

        while time.time() < deadline:
            still_open = [jid for jid in job_ids if not _is_terminal(statuses[jid].get("status"))]
            if not still_open:
                break
            with ThreadPoolExecutor(max_workers=min(8, len(still_open))) as pool:
                futures = {pool.submit(client.status, jid): jid for jid in still_open}
                for f in as_completed(futures):
                    jid = futures[f]
                    try:
                        st = f.result()
                    except Exception as e:
                        st = {"status": "<error>", "error": str(e)}
                    statuses[jid] = st
                    last_seen[jid] = st.get("status", "?")
            time.sleep(5)

        # Phase 3 — classify outcomes.
        durations: dict[str, float] = {}
        outcomes: dict[str, str] = {}
        for jid in job_ids:
            st = statuses[jid].get("status")
            if st == "done":
                outcomes[jid] = "done"
            elif st in TERMINAL_BAD:
                outcomes[jid] = f"failed ({st})"
            elif st == "validation_failed":
                outcomes[jid] = "validation_failed"
            elif _is_terminal(st):
                outcomes[jid] = f"terminal_other ({st})"
            else:
                outcomes[jid] = f"hung_at ({st})"

        done = sum(1 for v in outcomes.values() if v == "done")
        hung = sum(1 for v in outcomes.values() if v.startswith("hung_at"))
        failed = sum(1 for v in outcomes.values() if v.startswith("failed"))
        validation_fail = sum(1 for v in outcomes.values() if v == "validation_failed")

        details = {
            "api_url": self.api_url,
            "concurrency": self.concurrency,
            "timeout_secs": self.timeout_secs,
            "submit_secs": submit_secs,
            "outcomes": outcomes,
            "final_statuses": {jid: statuses[jid].get("status") for jid in job_ids},
            "summary": {
                "done": done, "hung": hung, "failed": failed,
                "validation_failed": validation_fail,
            },
        }

        if hung:
            return self._failed(
                f"{hung}/{self.concurrency} jobs HUNG (no terminal state within "
                f"{self.timeout_secs}s) — workers stalled or job lost",
                **details,
            )
        if failed:
            return self._failed(
                f"{failed}/{self.concurrency} jobs failed terminally — see outcomes",
                **details,
            )
        if done == self.concurrency:
            return self._passed(
                f"all {self.concurrency} jobs reached done; submit took {submit_secs}s",
                **details,
            )
        # Some validation_failed mixed in — concurrency itself is fine, but
        # surface as warn since it is unexpected.
        return self._warned(
            f"{done} done, {validation_fail} validation_failed, no hangs",
            **details,
        )


def _is_terminal(status: str | None) -> bool:
    if not status or status == "<error>":
        return False
    return status in TERMINAL_ACCEPTABLE or status in TERMINAL_BAD
