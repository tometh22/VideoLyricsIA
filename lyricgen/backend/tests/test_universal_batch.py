from pathlib import Path
from types import SimpleNamespace

import pytest

from batch_manifest import AudioManifestEntry, build_manifest, parse_audio_filename
from batch_profiles import RenderProfileError, normalize_render_profile, pipeline_fields
from universal_batch import (
    Api, AuthSession, BatchError, _assert_wave_approved, _resolved_language,
    process_wave, select_backgrounds,
)
import universal_batch as ub


AUDIO_DIR = Path("/Users/tomi/Downloads/Audio_Wavs 2")


def test_parser_handles_glued_code_and_versions():
    glued = parse_audio_filename("Que Pasó_Bersuit VergarabatARF149800014.wav")
    assert glued["title"] == "Que Pasó"
    assert glued["artist"] == "Bersuit Vergarabat"
    assert glued["technical_code"] == "ARF149800014"
    live = parse_audio_filename("Eso Es Real (Live)_Los Pericos_ARF040000028.wav")
    assert live["title"] == "Eso Es Real (Live)"
    assert live["version"] == "live"
    assert live["lookup_title"] == "Eso Es Real"


@pytest.mark.skipif(not AUDIO_DIR.exists(), reason="Universal WAV fixture folder is not mounted")
def test_real_universal_folder_has_no_unmapped_or_duplicate_codes():
    entries = build_manifest(AUDIO_DIR)
    assert len(entries) == 31  # current folder is guarded against accidental 30/31 drift
    assert all(entry.title and entry.artist and entry.technical_code for entry in entries)
    assert len({entry.technical_code for entry in entries}) == len(entries)
    assert len({entry.sha256 for entry in entries}) == len(entries)
    assert any(entry.fuzzy_lookup and entry.title.startswith("Instant-Taneas") for entry in entries)


def test_render_profile_is_strict_and_maps_fade():
    profile = normalize_render_profile({
        "font": "poppins-bold",
        "font_scale": 1.3,
        "text_case": "lower",
        "transition": "fade",
        "background_type": "photo",
        "movement": "foto-estatica",
        "effect": "bokeh",
        "style": "neon",
        "background_id": 42,
    })
    assert profile["movement_style"] == "foto-estatica"
    assert pipeline_fields(profile)["line_transition"] == "dissolve_blur"
    with pytest.raises(RenderProfileError):
        normalize_render_profile({"font": "comic-sans"})
    with pytest.raises(RenderProfileError):
        normalize_render_profile({"font": "poppins-bold", "movement": "foto-parallax"})


def _entry(index: int) -> AudioManifestEntry:
    return AudioManifestEntry(
        source_path=f"/tmp/{index}.wav",
        filename=f"Song_{index}_Artist_ARF{index:09d}.wav",
        title=f"Song {index}",
        artist="Artist",
        lookup_title=f"Song {index}",
        version="",
        technical_code=f"ARF{index:09d}",
        fuzzy_lookup=False,
        size_bytes=100,
        sha256=f"sha-{index}",
        duration_seconds=180.0,
    )


def test_batch_transcription_requests_audio_auto_language(monkeypatch):
    calls = []
    api = Api("https://api.example", "token")
    monkeypatch.setattr(
        api,
        "request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"status": "transcribing_queued"},
    )
    entry = _entry(1)
    entry.version = "live"

    api.start_transcription(entry, "job123456789")

    body = calls[0][2]["json"]
    assert body["language"] == ""
    assert body["live"] is True


def test_batch_manifest_persists_confirmed_language(monkeypatch):
    api = Api("https://api.example", "token")
    monkeypatch.setattr(api, "request", lambda *_args, **_kwargs: {
        "status": "transcribed",
        "segments": [],
        "transcription_quality": {"metrics": {"language": "en"}},
    })
    entry = _entry(2)

    assert api.wait_for_transcription(entry, "job123456789", 0) == []
    assert entry.search_result["language"] == "en"
    assert entry.search_result["language_requested"] == "auto"


@pytest.mark.parametrize("payload, expected", [
    ({"detected_language": "en"}, "en"),
    ({"language": "pt"}, "pt"),
    ({"transcription_quality": {"metrics": {"language": "fr"}}}, "fr"),
    ({"detected_languages": ["en", "es"], "mixed_language": True}, None),
    ({"transcription_quality": {"metrics": {"language": "unknown"}}}, None),
])
def test_resolved_language_never_invents_a_forced_fallback(payload, expected):
    assert _resolved_language(payload) == expected


def test_background_pool_cycles_for_campaigns_larger_than_library():
    class FakeApi:
        def request(self, method, path):
            assert (method, path) == ("GET", "/backgrounds")
            return [
                {"id": 1, "file_type": "video", "tags": "umg"},
                {"id": 2, "file_type": "image", "tags": "umg"},
            ]

    selected = select_backgrounds(FakeApi(), count=8)
    assert len(selected) == 8
    assert [row["id"] for row in selected] == [1, 1, 1, 1, 2, 2, 2, 2]


def test_wave_submits_every_song_before_reconcile_and_one_failure_does_not_stop_batch():
    entries = [_entry(i) for i in range(4)]
    events = []

    class FakeApi:
        def upload_audio(self, entry):
            events.append(("upload", entry.title))
            if entry.title == "Song 1":
                raise RuntimeError("bad audio")
            return f"job-{entry.title}"

        def start_transcription(self, entry, job_id):
            events.append(("start", entry.title))
            entry.status = "transcribing_queued"

        def wait_for_transcription(self, entry, job_id, poll_seconds):
            events.append(("wait", entry.title))
            entry.status = "lyrics_review_pending"
            return [{"text": entry.title}]

        def generate(self, entry, job_id, segments, poll_seconds):
            events.append(("generate", entry.title))
            entry.status = "pending_review"

    saves = []
    process_wave(
        entries,
        api_factory=FakeApi,
        poll_seconds=0,
        concurrency=4,
        save=lambda: saves.append(True),
    )

    assert entries[1].status == "error"
    assert "bad audio" in entries[1].error
    assert all(
        entry.status == "lyrics_review_pending"
        for i, entry in enumerate(entries) if i != 1
    )
    assert not any(event[0] == "generate" for event in events)
    start_positions = [i for i, event in enumerate(events) if event[0] == "start"]
    wait_positions = [i for i, event in enumerate(events) if event[0] == "wait"]
    assert max(start_positions) < min(wait_positions)
    assert saves, "cada mutacion debe quedar persistida para poder reanudar"


def test_batch_resumes_same_job_after_ambiguous_transcription_response(monkeypatch):
    api = Api("https://example.test", "token")
    entry = _entry(7)
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            raise ub.requests.Timeout("response lost after durable commit")
        assert (method, path) == ("GET", "/batch/jobs/job-7")
        return {"job_id": "job-7", "status": "transcribing_queued"}

    monkeypatch.setattr(api, "request", fake_request)
    result = api.start_transcription(entry, "job-7")

    assert result["resumed_after_ambiguous_response"] is True
    assert entry.status == "transcribing_queued"
    assert [call[:2] for call in calls] == [
        ("POST", "/transcribe-uploaded"),
        ("GET", "/batch/jobs/job-7"),
    ]
    assert calls[0][2]["headers"]["Idempotency-Key"].startswith(
        "batch-transcribe-"
    )


def test_batch_retries_identical_transcription_request_only_when_not_committed(
    monkeypatch,
):
    api = Api("https://example.test", "token")
    entry = _entry(8)
    posts = []

    def fake_request(method, path, **kwargs):
        if method == "GET":
            assert path == "/batch/jobs/job-8"
            return {"job_id": "job-8", "status": "awaiting_upload"}
        posts.append(kwargs)
        if len(posts) == 1:
            raise ub.requests.ConnectionError("connection reset")
        return {"job_id": "job-8", "status": "transcribing_queued"}

    monkeypatch.setattr(api, "request", fake_request)
    monkeypatch.setattr(ub.time, "sleep", lambda _seconds: None)
    result = api.start_transcription(entry, "job-8")

    assert result["status"] == "transcribing_queued"
    assert len(posts) == 2
    assert posts[0]["json"] == posts[1]["json"]
    assert posts[0]["headers"] == posts[1]["headers"]


def test_capacity_preflight_fails_before_upload_when_campaign_is_not_enabled(monkeypatch):
    api = Api("https://example.test", "token")
    responses = {
        "/auth/me": {"plan": "1000", "allow_overage": True},
        "/usage": {"plan": "1000", "total_available": 1000},
        "/batch/capacity": {
            "campaign_enabled": False,
            "bypass": False,
            "user_backlog": {"remaining": 5},
            "tenant_backlog": {"remaining": 25},
            "daily": {"remaining": 500},
        },
    }
    monkeypatch.setattr(api, "request", lambda method, path: responses[path])
    with pytest.raises(BatchError, match="BATCH_CAMPAIGN_SCOPES"):
        api.validate_capacity(expected_count=1000, wave_size=30)


def test_capacity_preflight_accepts_bounded_campaign_window(monkeypatch):
    api = Api("https://example.test", "token")
    responses = {
        "/auth/me": {"plan": "1000", "allow_overage": True},
        "/usage": {"plan": "1000", "total_available": 0},
        "/batch/capacity": {
            "campaign_enabled": True,
            "bypass": False,
            "user_backlog": {"remaining": 30},
            "tenant_backlog": {"remaining": 30},
            "daily": {"remaining": 1200},
        },
    }
    monkeypatch.setattr(api, "request", lambda method, path: responses[path])
    api.validate_capacity(expected_count=1000, wave_size=30)


def test_waits_for_reviewers_before_starting_next_wave(monkeypatch):
    api = Api("https://example.test", "token")
    snapshots = iter([
        {
            "bypass": False,
            "user_backlog": {"remaining": 20},
            "tenant_backlog": {"remaining": 20},
        },
        {
            "bypass": False,
            "user_backlog": {"remaining": 30},
            "tenant_backlog": {"remaining": 30},
        },
    ])
    monkeypatch.setattr(api, "request", lambda method, path: next(snapshots))
    monkeypatch.setattr(ub.time, "sleep", lambda _seconds: None)
    api.wait_for_backlog_capacity(
        needed=30, poll_seconds=0, max_wait_seconds=10,
    )


def test_run_refuses_legacy_non_campaign_transcription(tmp_path):
    args = SimpleNamespace(
        folder=str(tmp_path),
        manifest=str(tmp_path / "manifest.json"),
        api_base="https://example.test",
        token="token",
        expected_count=2,
        allow_count_mismatch=False,
        resume=False,
        retry_errors=False,
        wave_size=1,
        concurrency=1,
        canary_size=1,
        continue_after_canary=False,
        poll_seconds=0,
        capacity_poll_seconds=0,
        capacity_wait_seconds=1,
        stage="transcription",
    )
    with pytest.raises(BatchError, match="campaign_uploader"):
        ub.run(args)


def test_render_stage_refuses_whole_wave_before_background_work():
    entries = [_entry(1), _entry(2)]
    entries[0].job_id = "job-1"
    entries[1].job_id = "job-2"

    class FakeApi:
        def request(self, method, path):
            assert method == "GET"
            return {
                "status": "lyrics_approved" if path.endswith("job-1")
                else "transcribed_pending"
            }

    with pytest.raises(BatchError, match="not approved"):
        _assert_wave_approved(FakeApi(), entries)


def test_render_stage_accepts_already_completed_jobs_when_resuming():
    entries = [_entry(1), _entry(2)]
    entries[0].job_id = "job-1"
    entries[1].job_id = "job-2"

    class FakeApi:
        def request(self, method, path):
            assert method == "GET"
            return {"status": "done" if path.endswith("job-1") else "pending_review"}

    _assert_wave_approved(FakeApi(), entries)
    assert [entry.status for entry in entries] == ["done", "pending_review"]


def test_forced_expiry_is_retried_once_with_shared_valid_token(monkeypatch):
    auth = AuthSession(
        "https://example.test", "still-valid-token",
        force_expire_after_requests=1,
    )
    api = Api("https://example.test", auth=auth)
    seen = []

    class Response:
        content = b"{}"

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        bearer = kwargs["headers"]["Authorization"]
        seen.append(bearer)
        return Response(401 if "forced-expired" in bearer else 200)

    monkeypatch.setattr(api.session, "request", fake_request)
    assert api.request("GET", "/auth/me") == {"ok": True}
    assert seen == [
        "Bearer forced-expired-canary-token",
        "Bearer still-valid-token",
    ]
    assert {"forced_expiry", "recovered_401"}.issubset(auth.events)


def test_presigned_put_never_carries_api_authorization(tmp_path, monkeypatch):
    """R2 rejects query-signed PUTs that also carry Authorization (400)."""
    import universal_batch
    from batch_manifest import AudioManifestEntry

    api = universal_batch.Api("https://api.test", "secret-token")
    calls = []

    class _Resp:
        status_code = 200
        headers = {"ETag": '"abc"'}

    def fake_put(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr(universal_batch.requests, "put", fake_put)
    monkeypatch.setattr(
        api.session, "put",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("session.put used for R2")),
    )
    wav = tmp_path / "Song_Artist_ISRC.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 64)
    entry = AudioManifestEntry(
        source_path=str(wav), filename=wav.name, title="Song", artist="Artist",
        lookup_title="Song", version="", technical_code="ISRC",
        fuzzy_lookup=False, size_bytes=68, sha256="x" * 64,
        duration_seconds=None,
    )
    monkeypatch.setattr(api, "request", lambda method, path, **kw: {
        "job_id": "job1", "use_multipart": False, "upload_url": "https://r2.test/k?X-Amz-Signature=s",
    })

    assert api.upload_audio(entry) == "job1"
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url.startswith("https://r2.test/")
    assert "Authorization" not in {k.title() for k in kwargs.get("headers", {})}
    assert "secret-token" not in str(kwargs.get("headers"))


def test_presigned_put_retries_transient_transport_errors(monkeypatch):
    import universal_batch

    api = universal_batch.Api("https://api.test", "tok")
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        headers = {"ETag": '"abc"'}

    def flaky_put(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise universal_batch.requests.ConnectionError("Broken pipe")
        assert kwargs["data"] == b"bytes"
        return _Resp()

    monkeypatch.setattr(universal_batch.requests, "put", flaky_put)
    monkeypatch.setattr(universal_batch.time, "sleep", lambda *_: None)
    assert api._put_presigned("https://r2.test/k?sig", b"bytes").status_code == 200
    assert calls["n"] == 3


def test_presigned_put_gives_up_after_bounded_retries(monkeypatch):
    import pytest
    import universal_batch

    api = universal_batch.Api("https://api.test", "tok")
    monkeypatch.setattr(
        universal_batch.requests, "put",
        lambda *a, **k: (_ for _ in ()).throw(universal_batch.requests.ConnectionError("down")),
    )
    monkeypatch.setattr(universal_batch.time, "sleep", lambda *_: None)
    with pytest.raises(universal_batch.BatchError):
        api._put_presigned("https://r2.test/k?sig", b"bytes")
