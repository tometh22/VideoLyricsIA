import hashlib

import pytest
import requests

from firefly_client import (
    FIREFLY_IMS_TOKEN_URL,
    FireflyAmbiguousSubmission,
    FireflyClient,
    FireflyJobFailed,
    FireflyRateLimited,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        del chunk_size
        midpoint = len(self._content) // 2
        yield self._content[:midpoint]
        yield self._content[midpoint:]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeSession:
    def __init__(self):
        self.routes = {"post": [], "get": [], "put": []}
        self.calls = []

    def queue(self, method, response):
        self.routes[method].append(response)

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        response = self.routes[method].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        return self._next("post", url, kwargs)

    def get(self, url, **kwargs):
        return self._next("get", url, kwargs)

    def put(self, url, **kwargs):
        return self._next("put", url, kwargs)


def _client(session, **kwargs):
    return FireflyClient(
        client_id="client-id",
        client_secret="client-secret",
        session=session,
        poll_interval_s=0,
        sleeper=lambda _seconds: None,
        **kwargs,
    )


def _token(session, value="token-1"):
    session.queue(
        "post",
        FakeResponse(payload={"access_token": value, "expires_in": 86400}),
    )


def _accepted(job_id="urn:ff:jobs:test:123"):
    return FakeResponse(
        status_code=202,
        payload={
            "jobId": job_id,
            "statusUrl": f"https://firefly-api.adobe.io/v3/status/{job_id}",
            "cancelUrl": f"https://firefly-api.adobe.io/v3/cancel/{job_id}",
        },
    )


def test_generate_video_caches_token_and_returns_safe_provenance(tmp_path):
    session = FakeSession()
    video_bytes = b"fake-mp4-video"
    _token(session)
    session.queue("post", _accepted())
    session.queue(
        "get",
        FakeResponse(
            payload={"jobId": "urn:ff:jobs:test:123", "status": "running", "progress": 42}
        ),
    )
    session.queue(
        "get",
        FakeResponse(
            payload={
                "jobId": "urn:ff:jobs:test:123",
                "status": "succeeded",
                "result": {
                    "outputs": [
                        {
                            "seed": 741,
                            "video": {
                                "url": "https://example-bucket.s3.amazonaws.com/video.mp4?signature=secret"
                            },
                        }
                    ]
                },
            }
        ),
    )
    session.queue("get", FakeResponse(content=video_bytes))
    heartbeats = []
    destination = tmp_path / "clip.mp4"

    result = _client(session).generate_video(
        "A quiet cinematic landscape",
        str(destination),
        width=1920,
        height=1080,
        heartbeat=lambda job_id, progress: heartbeats.append((job_id, progress)),
    )

    assert destination.read_bytes() == video_bytes
    assert result.provider_job_id == "urn:ff:jobs:test:123"
    assert result.model_version == "video1_standard"
    assert result.seed == 741
    assert result.output_bytes == len(video_bytes)
    assert result.output_sha256 == hashlib.sha256(video_bytes).hexdigest()
    assert heartbeats == [
        ("urn:ff:jobs:test:123", 42),
        ("urn:ff:jobs:test:123", None),
    ]

    token_calls = [call for call in session.calls if call[1] == FIREFLY_IMS_TOKEN_URL]
    assert len(token_calls) == 1
    submit = next(call for call in session.calls if call[1].endswith("/v3/videos/generate"))
    assert submit[2]["headers"]["x-model-version"] == "video1_standard"
    assert submit[2]["json"] == {
        "prompt": "A quiet cinematic landscape",
        "sizes": [{"width": 1920, "height": 1080}],
    }
    # Output downloads use the pre-signed URL directly.  Adobe credentials
    # must not be forwarded to the storage host.
    download = session.calls[-1]
    assert "headers" not in download[2]


def test_submission_transport_failure_is_ambiguous_and_never_retried():
    session = FakeSession()
    _token(session)
    session.queue("post", requests.Timeout("socket timed out"))

    with pytest.raises(FireflyAmbiguousSubmission):
        _client(session).submit_video("Landscape")

    submit_calls = [call for call in session.calls if call[1].endswith("/v3/videos/generate")]
    assert len(submit_calls) == 1


def test_explicit_rate_limit_exposes_retry_after_without_echoing_body():
    session = FakeSession()
    _token(session)
    session.queue(
        "post",
        FakeResponse(
            status_code=429,
            payload={"message": "prompt should not escape"},
            headers={"retry-after": "12"},
        ),
    )

    with pytest.raises(FireflyRateLimited) as exc_info:
        _client(session).submit_video("private prompt")

    assert exc_info.value.retry_after == "12"
    assert "private prompt" not in str(exc_info.value)


def test_provider_job_urls_are_restricted_to_adobe_and_remain_ambiguous():
    session = FakeSession()
    _token(session)
    response = _accepted()
    response._payload["statusUrl"] = "https://attacker.example/v3/status/123"
    session.queue("post", response)

    with pytest.raises(FireflyAmbiguousSubmission, match="complete job metadata"):
        _client(session).submit_video("Landscape")


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_timeout_and_server_responses_are_ambiguous(status_code):
    session = FakeSession()
    _token(session)
    session.queue("post", FakeResponse(status_code=status_code, payload={}))

    with pytest.raises(FireflyAmbiguousSubmission):
        _client(session).submit_video("Landscape")

    submit_calls = [call for call in session.calls if call[1].endswith("/v3/videos/generate")]
    assert len(submit_calls) == 1


def test_terminal_job_failure_does_not_download_or_resubmit():
    session = FakeSession()
    _token(session)
    session.queue("post", _accepted())
    session.queue(
        "get",
        FakeResponse(
            payload={
                "jobId": "urn:ff:jobs:test:123",
                "status": "failed",
                "error_code": "content_rejected",
                "message": "provider detail",
            }
        ),
    )
    client = _client(session)
    submission = client.submit_video("Landscape")

    with pytest.raises(FireflyJobFailed) as exc_info:
        client.wait_for_video(submission)

    assert exc_info.value.status == "failed"
    assert exc_info.value.error_code == "content_rejected"
    assert not session.routes["get"]
    submit_calls = [call for call in session.calls if call[1].endswith("/v3/videos/generate")]
    assert len(submit_calls) == 1
