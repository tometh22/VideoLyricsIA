"""Small, production-oriented client for Adobe Firefly Generate Video.

This module deliberately stops at the provider boundary.  Tenant routing,
Genly's scene cache, content validation, cost ceilings, and final provenance
live in the pipeline and can be connected after the Adobe entitlement is
available.  Keeping the HTTP client independent lets us validate the
authentication and async job contract without making a paid request.

The public API contract used here is Adobe Firefly API v3:

* OAuth server-to-server tokens from Adobe IMS
* ``POST /v3/videos/generate`` with ``x-model-version: video1_standard``
* async polling through the returned ``statusUrl``
* a pre-signed output URL on success

Provider URLs are treated as untrusted input.  Status/cancel URLs must stay on
``firefly-api.adobe.io`` and output URLs must use HTTPS.  Pre-signed URLs are
never returned to callers or included in exception messages because their
query strings may contain temporary credentials.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests


FIREFLY_API_BASE = "https://firefly-api.adobe.io"
FIREFLY_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FIREFLY_SCOPES = (
    "openid,AdobeID,session,additional_info,read_organizations,"
    "firefly_api,ff_apis"
)
FIREFLY_VIDEO_MODEL = "video1_standard"
_FIREFLY_API_HOST = "firefly-api.adobe.io"
_TERMINAL_FAILURES = {"failed", "cancelled", "canceled", "timeout"}


class FireflyError(RuntimeError):
    """Base class for Firefly integration failures."""


class FireflyConfigurationError(FireflyError):
    """The required Firefly Services entitlement or secret is unavailable."""


class FireflyAmbiguousSubmission(FireflyError):
    """Adobe may have accepted a billable request, so it must not be retried."""


class FireflyRateLimited(FireflyError):
    """Adobe explicitly rejected the request because of a rate limit."""

    def __init__(self, retry_after: str | None = None):
        super().__init__("Adobe Firefly rate limit exceeded")
        self.retry_after = retry_after


class FireflyJobFailed(FireflyError):
    """Adobe completed the async job without a usable video."""

    def __init__(self, *, status: str, error_code: str = ""):
        detail = f" ({error_code})" if error_code else ""
        super().__init__(f"Adobe Firefly job ended with status={status}{detail}")
        self.status = status
        self.error_code = error_code


@dataclass(frozen=True)
class FireflyVideoResult:
    """Stable metadata safe to persist in Genly provenance."""

    provider_job_id: str
    model_version: str
    width: int
    height: int
    seed: int | None
    output_sha256: str
    output_bytes: int


@dataclass(frozen=True)
class _AccessToken:
    value: str
    expires_at_monotonic: float


class FireflyClient:
    """Synchronous Firefly video client with cached server credentials.

    A ``requests.Session`` can be injected for deterministic tests.  One
    instance is safe to reuse across worker threads; token refresh is guarded
    by a lock and the token is renewed five minutes before Adobe's expiry.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        session: requests.Session | None = None,
        api_base: str = FIREFLY_API_BASE,
        token_url: str = FIREFLY_IMS_TOKEN_URL,
        model_version: str = FIREFLY_VIDEO_MODEL,
        request_timeout_s: float = 60.0,
        download_timeout_s: float = 180.0,
        poll_interval_s: float = 5.0,
        poll_timeout_s: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.client_id = (
            client_id
            if client_id is not None
            else os.environ.get("FIREFLY_SERVICES_CLIENT_ID", "")
        ).strip()
        self.client_secret = (
            client_secret
            if client_secret is not None
            else os.environ.get("FIREFLY_SERVICES_CLIENT_SECRET", "")
        ).strip()
        if not self.client_id or not self.client_secret:
            raise FireflyConfigurationError(
                "FIREFLY_SERVICES_CLIENT_ID and FIREFLY_SERVICES_CLIENT_SECRET "
                "are required"
            )

        parsed_base = urlparse(api_base)
        if parsed_base.scheme != "https" or parsed_base.hostname != _FIREFLY_API_HOST:
            raise FireflyConfigurationError("Firefly API base must use Adobe HTTPS")

        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self.model_version = model_version
        self.request_timeout_s = request_timeout_s
        self.download_timeout_s = download_timeout_s
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleeper
        self._token: _AccessToken | None = None
        self._token_lock = threading.Lock()

    def _access_token(self) -> str:
        now = self._clock()
        cached = self._token
        if cached and now < cached.expires_at_monotonic:
            return cached.value

        with self._token_lock:
            now = self._clock()
            cached = self._token
            if cached and now < cached.expires_at_monotonic:
                return cached.value
            try:
                response = self._session.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": FIREFLY_SCOPES,
                    },
                    timeout=self.request_timeout_s,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise FireflyConfigurationError(
                    "Could not obtain Adobe Firefly access token"
                ) from exc

            value = str(payload.get("access_token") or "").strip()
            if not value:
                raise FireflyConfigurationError(
                    "Adobe IMS response did not contain an access token"
                )
            try:
                expires_in = max(1, int(payload.get("expires_in", 86400)))
            except (TypeError, ValueError):
                expires_in = 86400
            # Keep at least a one-second usable window for deliberately short
            # test tokens while renewing normal 24-hour tokens five minutes
            # before expiry.
            refresh_margin = min(300, max(0, expires_in - 1))
            self._token = _AccessToken(
                value=value,
                expires_at_monotonic=now + expires_in - refresh_margin,
            )
            return value

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "x-api-key": self.client_id,
            "Accept": "application/json",
        }

    @staticmethod
    def _trusted_job_url(url: str) -> str:
        parsed = urlparse(str(url or ""))
        if (
            parsed.scheme != "https"
            or parsed.hostname != _FIREFLY_API_HOST
            or not parsed.path.startswith("/v3/")
            or parsed.username
            or parsed.password
        ):
            raise FireflyError("Adobe returned an invalid async job URL")
        return url

    @staticmethod
    def _trusted_output_url(url: str) -> str:
        parsed = urlparse(str(url or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise FireflyError("Adobe returned an invalid video output URL")
        return url

    @staticmethod
    def _json(response: requests.Response, context: str) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FireflyError(f"Adobe returned invalid JSON during {context}") from exc
        if not isinstance(payload, dict):
            raise FireflyError(f"Adobe returned an invalid payload during {context}")
        return payload

    def submit_video(
        self,
        prompt: str,
        *,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
        video_settings: dict | None = None,
    ) -> dict:
        """Submit one billable five-second Firefly video generation.

        Transport failures after the POST begins are ambiguous.  They raise a
        dedicated exception so the caller can preserve the spend reservation
        and avoid issuing a second paid request.
        """

        cleaned_prompt = str(prompt or "").strip()
        if not cleaned_prompt:
            raise ValueError("Firefly video prompt cannot be empty")
        if width <= 0 or height <= 0:
            raise ValueError("Firefly video dimensions must be positive")

        body: dict = {
            "prompt": cleaned_prompt,
            "sizes": [{"width": int(width), "height": int(height)}],
        }
        if seed is not None:
            body["seeds"] = [int(seed)]
        if video_settings:
            body["videoSettings"] = dict(video_settings)

        headers = self._headers()
        headers.update(
            {
                "Content-Type": "application/json",
                "x-model-version": self.model_version,
            }
        )
        try:
            response = self._session.post(
                f"{self.api_base}/v3/videos/generate",
                headers=headers,
                json=body,
                timeout=self.request_timeout_s,
            )
        except requests.RequestException as exc:
            raise FireflyAmbiguousSubmission(
                "Adobe Firefly submission response was ambiguous"
            ) from exc

        if response.status_code == 429:
            raise FireflyRateLimited(response.headers.get("retry-after"))
        if response.status_code == 408 or response.status_code >= 500:
            raise FireflyAmbiguousSubmission(
                "Adobe Firefly submission response was ambiguous"
            )
        if response.status_code != 202:
            # A complete non-2xx response is an explicit rejection, not an
            # ambiguous paid operation.  Keep the error compact and omit the
            # response body because vendor payloads may echo prompt content.
            raise FireflyError(
                f"Adobe Firefly rejected video submission (HTTP {response.status_code})"
            )

        try:
            payload = self._json(response, "video submission")
            provider_job_id = str(payload.get("jobId") or "").strip()
            status_url = self._trusted_job_url(str(payload.get("statusUrl") or ""))
            cancel_url = self._trusted_job_url(str(payload.get("cancelUrl") or ""))
        except FireflyError as exc:
            raise FireflyAmbiguousSubmission(
                "Adobe accepted the video request without complete job metadata"
            ) from exc
        if not provider_job_id:
            raise FireflyAmbiguousSubmission(
                "Adobe accepted the video request without returning a job ID"
            )
        return {
            "job_id": provider_job_id,
            "status_url": status_url,
            "cancel_url": cancel_url,
            "width": int(width),
            "height": int(height),
        }

    @staticmethod
    def _extract_output(payload: dict) -> tuple[str, int | None]:
        outputs = (payload.get("result") or {}).get("outputs") or []
        if not isinstance(outputs, list) or not outputs:
            raise FireflyJobFailed(status="succeeded", error_code="missing_output")
        first = outputs[0] if isinstance(outputs[0], dict) else {}
        media = first.get("video") or first.get("image") or {}
        output_url = media.get("url") if isinstance(media, dict) else ""
        if not output_url:
            raise FireflyJobFailed(status="succeeded", error_code="missing_output_url")
        raw_seed = first.get("seed")
        try:
            seed = int(raw_seed) if raw_seed is not None else None
        except (TypeError, ValueError):
            seed = None
        return str(output_url), seed

    def wait_for_video(
        self,
        submission: dict,
        *,
        heartbeat: Callable[[str, int | None], None] | None = None,
    ) -> tuple[str, int | None]:
        """Poll until success and return the transient output URL and seed."""

        status_url = self._trusted_job_url(str(submission.get("status_url") or ""))
        provider_job_id = str(submission.get("job_id") or "")
        deadline = self._clock() + self.poll_timeout_s

        while self._clock() < deadline:
            try:
                response = self._session.get(
                    status_url,
                    headers=self._headers(),
                    timeout=self.request_timeout_s,
                )
                response.raise_for_status()
            except requests.RequestException:
                # Polling is idempotent.  A transient read error does not imply
                # another generation, so it is safe to continue until deadline.
                if heartbeat:
                    heartbeat(provider_job_id, None)
                self._sleep(self.poll_interval_s)
                continue

            payload = self._json(response, "job polling")
            status = str(payload.get("status") or "").strip().lower()
            progress_raw = payload.get("progress")
            try:
                progress = int(progress_raw) if progress_raw is not None else None
            except (TypeError, ValueError):
                progress = None
            if heartbeat:
                heartbeat(provider_job_id, progress)

            if status == "succeeded":
                output_url, seed = self._extract_output(payload)
                return self._trusted_output_url(output_url), seed
            if status in _TERMINAL_FAILURES:
                raise FireflyJobFailed(
                    status=status,
                    error_code=str(payload.get("error_code") or ""),
                )
            self._sleep(self.poll_interval_s)

        self.cancel(submission)
        raise FireflyJobFailed(status="timeout", error_code="local_poll_timeout")

    def cancel(self, submission: dict) -> None:
        """Best-effort cancellation used after a local poll timeout."""

        try:
            cancel_url = self._trusted_job_url(str(submission.get("cancel_url") or ""))
            self._session.put(
                cancel_url,
                headers=self._headers(),
                timeout=self.request_timeout_s,
            )
        except (FireflyError, requests.RequestException):
            return

    def download_video(self, output_url: str, output_path: str) -> tuple[str, int]:
        """Download to a sibling temp file and atomically publish the MP4."""

        safe_url = self._trusted_output_url(output_url)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.firefly-part")
        digest = hashlib.sha256()
        written = 0
        try:
            with self._session.get(
                safe_url,
                stream=True,
                timeout=self.download_timeout_s,
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            if written <= 0:
                raise FireflyError("Adobe returned an empty video artifact")
            os.replace(temporary, destination)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return digest.hexdigest(), written

    def generate_video(
        self,
        prompt: str,
        output_path: str,
        *,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
        video_settings: dict | None = None,
        heartbeat: Callable[[str, int | None], None] | None = None,
    ) -> FireflyVideoResult:
        """Submit, poll and atomically download a five-second video."""

        submission = self.submit_video(
            prompt,
            width=width,
            height=height,
            seed=seed,
            video_settings=video_settings,
        )
        output_url, used_seed = self.wait_for_video(
            submission,
            heartbeat=heartbeat,
        )
        output_sha256, output_bytes = self.download_video(output_url, output_path)
        return FireflyVideoResult(
            provider_job_id=submission["job_id"],
            model_version=self.model_version,
            width=submission["width"],
            height=submission["height"],
            seed=used_seed,
            output_sha256=output_sha256,
            output_bytes=output_bytes,
        )
