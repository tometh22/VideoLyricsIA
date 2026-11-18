"""Serial local inference worker for staging's authenticated Genly model queue.

Audio and weights stay local during inference. The backend receives only the
source-bound candidate; credentials are never forwarded to signed audio URLs.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import requests

from local_genly_review import normalize_snapshot
from local_lora_probe import private_json
from reviewer_shadow import source_binding


class WorkerError(RuntimeError):
    pass


class StagingClient:
    def __init__(self, api_url: str, auth_file: Path):
        url = urlparse(api_url)
        if not (url.scheme == "https" and url.hostname == "api-staging-9b82.up.railway.app"
                and url.port in {None, 443} and not url.username and not url.password
                and url.path in {"", "/"} and not url.query and not url.fragment):
            raise ValueError("known_staging_api_required")
        if auth_file.stat().st_mode & 0o077:
            raise ValueError("auth_file_must_be_private")
        token = json.loads(auth_file.read_text())["token"]
        self.auth_file = auth_file
        self.refresh_at = self._refresh_deadline(token)
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(Authorization="Bearer " + token)
        self.lock = threading.Lock()

    @staticmethod
    def _refresh_deadline(token):
        # This payload is only a refresh scheduling hint; the API verifies JWTs.
        try:
            payload = token.split(".")[1]
            expires = float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
            return min(time.time() + 6 * 3600, expires - 3600)
        except (ValueError, KeyError, IndexError, TypeError):
            return time.time() + 6 * 3600

    def refresh_if_due(self):
        with self.lock:
            if time.time() < self.refresh_at:
                return
            response = self.session.request("POST", self.api_url + "/auth/refresh",
                                            timeout=(10, 45), allow_redirects=False)
            if response.status_code != 200:
                raise WorkerError("api_status_" + str(response.status_code))
            token = response.json().get("token")
            if not isinstance(token, str) or not token:
                raise WorkerError("invalid_refreshed_token")
            descriptor, name = tempfile.mkstemp(prefix=".genly-auth-", dir=self.auth_file.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump({"token": token}, stream)
                temporary.replace(self.auth_file)
            finally:
                temporary.unlink(missing_ok=True)
            self.session.headers.update(Authorization="Bearer " + token)
            self.refresh_at = self._refresh_deadline(token)

    def request(self, method: str, path: str, *, payload=None, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise WorkerError("relative_api_path_required")
        with self.lock:
            response = self.session.request(method, self.api_url + path, json=payload,
                                            headers=headers, timeout=(10, 45), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise WorkerError("api_status_" + str(response.status_code))
        return response.json()


def download_original(url: str, expected_sha: str, destination: Path,
                      *, get=None, maximum_bytes: int = 256 * 1024 * 1024) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WorkerError("https_audio_url_required")
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        raise WorkerError("audio_hash_required")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    if destination.is_file():
        with destination.open("rb") as stream:
            value = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        if value.hexdigest() != expected_sha:
            raise WorkerError("cached_audio_hash_mismatch")
        return destination
    descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + "-", suffix=".part",
                                                  dir=destination.parent)
    temporary = Path(temporary_name)
    value, size = hashlib.sha256(), 0
    storage_session = None
    if get is None:
        storage_session = requests.Session()
        storage_session.trust_env = False
        get = storage_session.get
    try:
        with os.fdopen(descriptor, "wb") as stream:
            # Deliberately NOT the API's authenticated Session.
            with get(url, stream=True, timeout=(10, 90), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise WorkerError("audio_download_failed")
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise WorkerError("audio_download_limit")
                    value.update(chunk)
                    stream.write(chunk)
        if value.hexdigest() != expected_sha:
            raise WorkerError("audio_hash_mismatch")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        if storage_session is not None:
            storage_session.close()
    return destination


class LeaseHeartbeat:
    def __init__(self, client, worker_id, claim, *, interval=20):
        self.client, self.worker_id, self.claim = client, worker_id, claim
        self.interval = interval
        self.stopped, self.lost = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        failures = 0
        while not self.stopped.wait(self.interval):
            try:
                self.client.request("POST", "/admin/genly-model/heartbeat", payload={
                    "worker_id": self.worker_id, "run_id": self.claim["run_id"],
                    "lease_token": self.claim["lease_token"], "stage": "processing"})
                failures = 0
            except Exception as exc:
                failures += 1
                if str(exc) in {"api_status_401", "api_status_403", "api_status_409", "api_status_410"} or failures >= 3:
                    self.lost.set()
                    return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=1)


def run_claim(client, claim: dict, *, worker_id: str, output_root: Path,
              selector: Path, adapter: Path | None, model="base", device="mps",
              timeout_seconds=1800, stem_manifest: Path | None = None,
              qwen_python: Path | None = None, qwen_checkpoint: Path | None = None,
              separate_vocals: bool = False, activity_vendor: Path | None = None) -> dict:
    run_id = claim["run_id"]
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise WorkerError("invalid_run_identity")
    job_id = claim["job_id"]
    if not job_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in job_id):
        raise WorkerError("invalid_job_identity")
    attempt = hashlib.sha256(claim["lease_token"].encode()).hexdigest()[:20]
    folder = output_root / run_id / attempt
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    with LeaseHeartbeat(client, worker_id, claim) as heartbeat:
        inputs = client.request("GET", f"/editor/{job_id}/genly-model/request")
        song = normalize_snapshot(inputs["snapshot"])
        if source_binding(song) != claim["source"]:
            raise WorkerError("source_changed_before_processing")
        audio_link = client.request("GET", inputs["source_audio_path"])
        if audio_link.get("source") != "input" or audio_link.get("fallback") is True:
            raise WorkerError("original_audio_required")
        audio = download_original(audio_link["url"], song["audio_sha256"],
                                  output_root / "audio" / song["audio_sha256"])
        if heartbeat.lost.is_set():
            raise WorkerError("worker_lease_lost")
        private_json(folder / "snapshot.json", song)
        preparation_commands = []
        if separate_vocals:
            prepared_manifest = folder / "stems.json"
            preparation = [sys.executable, str(Path(__file__).parent / "genly_model_stems.py"),
                           "--audio", str(audio), "--audio-sha256", song["audio_sha256"],
                           "--cache-dir", str(output_root / "stems"),
                           "--manifest-output", str(prepared_manifest), "--timeout", "600", "--max-duration", "900"]
            if stem_manifest:
                preparation.extend(["--existing-manifest", str(stem_manifest)])
            preparation_commands.append(preparation)
            stem_manifest = prepared_manifest
        command = [sys.executable, str(Path(__file__).parent / "scripts/run_genly_model.py"),
                   "--snapshot", str(folder / "snapshot.json"), "--audio", str(audio),
                   "--selector", str(selector), "--model", "base" if model == "qwen" else model, "--device", device,
                   "--output", str(folder / "inference"), "--execute"]
        if adapter:
            command.extend(["--adapter", str(adapter)])
        if stem_manifest:
            command.extend(["--stem-manifest", str(stem_manifest)])
        if activity_vendor:
            command.extend(["--activity-vendor", str(activity_vendor)])
        commands = [command]
        if model == "qwen":
            if adapter or not qwen_python or not qwen_python.is_file() or not qwen_checkpoint or not qwen_checkpoint.is_dir():
                raise WorkerError("qwen_local_runtime_required")
            recognition_command = [str(qwen_python), str(Path(__file__).parent / "scripts/run_qwen_genly_recognition.py"),
                                   "--snapshot", str(folder / "snapshot.json"), "--audio", str(audio),
                                   "--selector", str(selector), "--checkpoint", str(qwen_checkpoint),
                                   "--output", str(folder / "recognition"), "--execute"]
            if stem_manifest:
                recognition_command.extend(["--stem-manifest", str(stem_manifest)])
            command.extend(["--recognition", str(folder / "recognition/recognition.json")])
            commands = [recognition_command, command]
        commands = preparation_commands + commands
        log_fd = os.open(folder / "inference.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(log_fd, "w") as log:
            started = time.monotonic()
            for stage_command in commands:
                if heartbeat.lost.is_set():
                    raise WorkerError("worker_lease_lost")
                process = subprocess.Popen(stage_command, stdout=log, stderr=log)
                try:
                    while process.poll() is None:
                        if heartbeat.lost.is_set():
                            raise WorkerError("worker_lease_lost")
                        if time.monotonic() - started > timeout_seconds:
                            raise WorkerError("inference_timeout")
                        time.sleep(.5)
                    if process.returncode != 0:
                        raise WorkerError("local_inference_failed")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        if heartbeat.lost.is_set():
            raise WorkerError("worker_lease_lost")
        report = json.loads((folder / "inference/report.json").read_text())
        if report.get("source") != claim["source"]:
            raise WorkerError("inference_source_changed")
        result = client.request("POST", f"/editor/{job_id}/genly-model/candidate", payload=report,
                                headers={"X-Genly-Run-Id": run_id,
                                         "X-Genly-Lease-Token": claim["lease_token"]})
        private_json(folder / "publication.json", result)
        return {"job_id": job_id, "run_id": run_id, "status": "uploaded",
                "candidate_status": report["status"], "model_id": report["model_id"]}
