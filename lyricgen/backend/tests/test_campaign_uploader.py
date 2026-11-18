import importlib.util
import io
import json
from pathlib import Path
import time
import urllib.error


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "campaign_uploader.py"
SPEC = importlib.util.spec_from_file_location("campaign_uploader", SCRIPT)
uploader = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(uploader)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_forced_expired_upload_token_recovers_and_retries(monkeypatch):
    auth = uploader.CampaignAuth(
        "https://example.test", "campaign123",
        username="runner", password="secret",
        force_expire_after_requests=1,
    )
    auth.token = "initial-token"
    auth.expires_at = time.time() + 3600
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_header("X-batch-upload-token")))
        if request.full_url.endswith("/batch/uploads/item1/ticket"):
            if request.get_header("X-batch-upload-token") == "forced-expired-canary-token":
                raise urllib.error.HTTPError(
                    request.full_url, 401, "expired", {}, io.BytesIO(b'{}'),
                )
            return _Response({"complete": True})
        if request.full_url.endswith("/auth/login"):
            return _Response({"token": "account-jwt"})
        if request.full_url.endswith("/upload-session"):
            assert request.get_header("Authorization") == "Bearer account-jwt"
            return _Response({"pairing_code": "NEWCODE"})
        if request.full_url.endswith("/upload-sessions/exchange"):
            return _Response({"upload_token": "renewed-token", "expires_in": 43200})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(uploader.urllib.request, "urlopen", fake_urlopen)
    result = uploader.json_request(
        "https://example.test/batch/uploads/item1/ticket",
        method="POST", body={}, auth=auth,
    )

    assert result == {"complete": True}
    assert auth.token == "renewed-token"
    assert {"forced_expiry", "renewal", "recovered_401"}.issubset(auth.events)
    assert calls[-1][1] == "renewed-token"


def test_art_asset_mapping_keeps_audio_and_cover_roles_separate():
    audio = {"role": "audio", "filename": "cover.mp3", "relative_path": "album/cover.mp3"}
    cover = {"role": "cover", "filename": "cover.jpg", "relative_path": "album/cover.jpg"}
    audio_source = {"filename": "cover.mp3", "relative_path": "album/cover.mp3", "path": "/tmp/audio"}
    cover_source = {"filename": "cover.jpg", "relative_path": "album/cover.jpg", "path": "/tmp/cover"}

    assert uploader.source_for_art_asset(audio, [audio_source], [cover_source]) is audio_source
    assert uploader.source_for_art_asset(cover, [audio_source], [cover_source]) is cover_source


def test_art_manifest_entries_retain_folder_relative_audio_path(tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    path = folder / "nested" / "song.mp3"
    path.parent.mkdir()
    path.write_bytes(b"audio")
    entry = uploader.inspect_file(path)
    entry["relative_path"] = str(entry["path"].relative_to(folder))

    assert entry["relative_path"] == "nested/song.mp3"
