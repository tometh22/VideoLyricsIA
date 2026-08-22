"""Contract tests for the post-deploy edit smoke API client."""

from scripts.preflight import edit_smoke


class _Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text

    def json(self):
        return self._payload


def _install_common_smoke_mocks(monkeypatch, post, get):
    def put(url, **kwargs):
        assert url == "https://r2.example/upload"
        assert kwargs["headers"] == {"Content-Type": "audio/wav"}
        assert kwargs["data"].startswith(b"RIFF")
        return _Response()

    monkeypatch.setenv("PREFLIGHT_USERNAME", "smoke-user")
    monkeypatch.setenv("PREFLIGHT_PASSWORD", "smoke-password")
    monkeypatch.setattr(edit_smoke.requests, "post", post)
    monkeypatch.setattr(edit_smoke.requests, "put", put)
    monkeypatch.setattr(edit_smoke.requests, "get", get)


def test_edit_smoke_uses_current_presigned_upload_flow(monkeypatch):
    calls = []
    segments = [{"start": 0.0, "end": 1.0, "text": "smoke"}]

    def post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        if url.endswith("/auth/login"):
            return _Response({"token": "test-token"})
        if url.endswith("/upload-url"):
            assert kwargs["json"]["content_type"] == "audio/wav"
            assert kwargs["json"]["size_bytes"] > 0
            return _Response({
                "job_id": "smokejob123",
                "upload_url": "https://r2.example/upload",
                "use_multipart": False,
            })
        if url.endswith("/transcribe-uploaded"):
            assert kwargs["json"]["job_id"] == "smokejob123"
            return _Response({"status": "transcribing"})
        if url.endswith("/generate"):
            fields = kwargs["files"]
            assert fields["job_id"] == (None, "smokejob123")
            assert "smoke" in fields["segments_json"][1]
            return _Response({"status": "queued"})
        if url.endswith("/jobs/smokejob123/save-segments"):
            return _Response({"count": 2})
        if url.endswith("/edit/smokejob123"):
            return _Response({"status": "editing"})
        raise AssertionError(f"unexpected POST {url}")

    def put(url, **kwargs):
        calls.append(("PUT", url, kwargs))
        assert url == "https://r2.example/upload"
        assert kwargs["headers"] == {"Content-Type": "audio/wav"}
        assert kwargs["data"].startswith(b"RIFF")
        return _Response()

    def get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        if url.endswith("/transcription-status/smokejob123"):
            return _Response({"status": "transcribed", "segments": segments})
        if url.endswith("/status/smokejob123"):
            return _Response({
                "status": "pending_review",
                "current_step": "done",
                "progress": 100,
                "error": None,
            })
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setenv("PREFLIGHT_USERNAME", "smoke-user")
    monkeypatch.setenv("PREFLIGHT_PASSWORD", "smoke-password")
    monkeypatch.setattr(edit_smoke.requests, "post", post)
    monkeypatch.setattr(edit_smoke.requests, "put", put)
    monkeypatch.setattr(edit_smoke.requests, "get", get)
    monkeypatch.setattr(
        edit_smoke.sys,
        "argv",
        ["edit_smoke", "--api-url", "https://api.example"],
    )

    assert edit_smoke.main() == 0
    called_urls = [url for _method, url, _kwargs in calls]
    assert "https://api.example/upload" not in called_urls
    assert "https://api.example/upload-url" in called_urls
    assert "https://api.example/transcribe-uploaded" in called_urls
    assert "https://api.example/generate" in called_urls


def test_edit_smoke_accepts_fail_closed_quality_gate_in_staging(monkeypatch):
    segments = [{"start": 0.0, "end": 1.0, "text": "smoke"}]

    def post(url, **kwargs):
        if url.endswith("/auth/login"):
            return _Response({"token": "test-token"})
        if url.endswith("/upload-url"):
            return _Response({
                "job_id": "qualitysmoke1",
                "upload_url": "https://r2.example/upload",
                "use_multipart": False,
            })
        if url.endswith("/transcribe-uploaded"):
            return _Response({"status": "transcribing"})
        if url.endswith("/generate"):
            return _Response(
                {
                    "code": "transcription_quality_review_required",
                    "transcription_quality": {"decision": "review_required"},
                },
                status_code=409,
            )
        raise AssertionError(f"unexpected POST {url}")

    def get(url, **kwargs):
        if url.endswith("/transcription-status/qualitysmoke1"):
            return _Response({"status": "transcribed", "segments": segments})
        raise AssertionError(f"unexpected GET {url}")

    _install_common_smoke_mocks(monkeypatch, post, get)
    monkeypatch.setattr(
        edit_smoke.sys,
        "argv",
        [
            "edit_smoke", "--api-url", "https://api.example",
            "--allow-quality-gate-block",
        ],
    )

    assert edit_smoke.main() == 0


def test_edit_smoke_accepts_asynchronous_quality_gate_in_staging(monkeypatch):
    segments = [{"start": 0.0, "end": 1.0, "text": "smoke"}]
    posts = []

    def post(url, **kwargs):
        posts.append(url)
        if url.endswith("/auth/login"):
            return _Response({"token": "test-token"})
        if url.endswith("/upload-url"):
            return _Response({
                "job_id": "qualitysmoke3",
                "upload_url": "https://r2.example/upload",
                "use_multipart": False,
            })
        if url.endswith("/transcribe-uploaded"):
            return _Response({"status": "transcribing"})
        if url.endswith("/generate"):
            return _Response({"status": "queued"})
        raise AssertionError(f"unexpected POST {url}")

    def get(url, **kwargs):
        if url.endswith("/transcription-status/qualitysmoke3"):
            return _Response({"status": "transcribed", "segments": segments})
        if url.endswith("/status/qualitysmoke3"):
            return _Response({
                "status": "transcribed_pending",
                "current_step": "quality_review",
                "progress": 20,
                "error": "transcription_quality_review_required",
            })
        raise AssertionError(f"unexpected GET {url}")

    _install_common_smoke_mocks(monkeypatch, post, get)
    monkeypatch.setattr(
        edit_smoke.sys,
        "argv",
        [
            "edit_smoke", "--api-url", "https://api.example",
            "--allow-quality-gate-block",
        ],
    )

    assert edit_smoke.main() == 0
    assert not any(url.endswith("/save-segments") for url in posts)


def test_edit_smoke_does_not_hide_unknown_generate_conflict(monkeypatch):
    segments = [{"start": 0.0, "end": 1.0, "text": "smoke"}]

    def post(url, **kwargs):
        if url.endswith("/auth/login"):
            return _Response({"token": "test-token"})
        if url.endswith("/upload-url"):
            return _Response({
                "job_id": "qualitysmoke2",
                "upload_url": "https://r2.example/upload",
                "use_multipart": False,
            })
        if url.endswith("/transcribe-uploaded"):
            return _Response({"status": "transcribing"})
        if url.endswith("/generate"):
            return _Response({"code": "stale_revision"}, status_code=409)
        raise AssertionError(f"unexpected POST {url}")

    def get(url, **kwargs):
        if url.endswith("/transcription-status/qualitysmoke2"):
            return _Response({"status": "transcribed", "segments": segments})
        raise AssertionError(f"unexpected GET {url}")

    _install_common_smoke_mocks(monkeypatch, post, get)
    monkeypatch.setattr(
        edit_smoke.sys,
        "argv",
        [
            "edit_smoke", "--api-url", "https://api.example",
            "--allow-quality-gate-block",
        ],
    )

    assert edit_smoke.main() == 1


def test_edit_smoke_only_accepts_known_quality_status_errors():
    assert edit_smoke._status_quality_gate_code({
        "error": (
            "La letra requiere revisión de calidad antes de renderizar "
            "(transcription_quality_analysis_incomplete)."
        ),
    }) == "transcription_quality_analysis_incomplete"
    assert edit_smoke._status_quality_gate_code({
        "error": "render_failed: transcription provider unavailable",
    }) is None
