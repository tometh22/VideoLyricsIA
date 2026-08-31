from pathlib import Path
from types import SimpleNamespace

import pytest

from batch_manifest import AudioManifestEntry, build_manifest, parse_audio_filename
from batch_profiles import RenderProfileError, normalize_render_profile, pipeline_fields
from universal_batch import Api, BatchError, process_wave, select_backgrounds
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
            entry.status = "transcribed"
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
    assert all(entry.status == "pending_review" for i, entry in enumerate(entries) if i != 1)
    start_positions = [i for i, event in enumerate(events) if event[0] == "start"]
    wait_positions = [i for i, event in enumerate(events) if event[0] == "wait"]
    assert max(start_positions) < min(wait_positions)
    assert saves, "cada mutacion debe quedar persistida para poder reanudar"


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


def test_run_stops_after_canary_until_explicit_continue(monkeypatch, tmp_path):
    entries = [_entry(1), _entry(2)]
    monkeypatch.setattr(ub, "build_manifest", lambda _folder: entries)
    monkeypatch.setattr(ub.Api, "validate_capacity", lambda *a, **k: None)
    monkeypatch.setattr(ub.Api, "wait_for_backlog_capacity", lambda *a, **k: None)
    monkeypatch.setattr(ub, "select_backgrounds", lambda _api, count: [{"id": 1}] * count)
    monkeypatch.setattr(ub, "assign_profiles", lambda _entries, _assets: None)
    waves = []

    def fake_process(wave, **kwargs):
        waves.append([entry.title for entry in wave])
        for entry in wave:
            entry.status = "pending_review"
            entry.job_id = f"job-{entry.title}"
        kwargs["save"]()
        return wave

    monkeypatch.setattr(ub, "process_wave", fake_process)
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
    )
    assert ub.run(args) == 0
    assert waves == [["Song 1"]]
    assert entries[1].status == "pending"
