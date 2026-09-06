import hashlib
from pathlib import Path

from scripts.capture_reviewer_campaign_audio import ByteBudget, download_one, fetch_assets


class Response:
    status_code = 200
    headers = {}

    def __init__(self, body=b"audio"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, **kwargs):
        yield self.body


def fixture():
    song = {"job_id": "123456abcdef", "audio_sha256": hashlib.sha256(b"audio").hexdigest(), "audio_revision": 1}
    return song, {**song, "mix_url": "https://private.invalid/path?secret=yes"}


def test_atomic_verified_download(tmp_path):
    song, asset = fixture()
    result = download_one(song, asset, tmp_path, ByteBudget(), get=lambda *a, **k: Response())
    assert result["status"] == "downloaded_verified"
    assert Path(result["path"]).read_bytes() == b"audio"
    assert "secret" not in str(result)


def test_valid_cache_no_download(tmp_path):
    song, asset = fixture()
    (tmp_path / (song["job_id"] + "-mix.wav")).write_bytes(b"audio")
    result = download_one(song, asset, tmp_path, ByteBudget(), get=lambda *a, **k: 1 / 0)
    assert result["status"] == "cached_verified"


def test_changed_current_revision_even_cached_blocks(tmp_path):
    song, asset = fixture()
    (tmp_path / (song["job_id"] + "-mix.wav")).write_bytes(b"audio")
    asset["audio_revision"] = 2
    assert download_one(song, asset, tmp_path, ByteBudget())["reason"] == "current_audio_identity_changed"


def test_bad_existing_is_preserved(tmp_path):
    song, asset = fixture()
    (tmp_path / (song["job_id"] + "-mix.wav")).write_bytes(b"previous")
    result = download_one(song, asset, tmp_path, ByteBudget(), get=lambda *a, **k: Response())
    assert Path(result["preserved_previous_path"]).read_bytes() == b"previous"
    assert Path(result["path"]).read_bytes() == b"audio"


def test_bad_download_does_not_replace_existing(tmp_path):
    song, asset = fixture()
    target = tmp_path / (song["job_id"] + "-mix.wav")
    target.write_bytes(b"previous")
    result = download_one(song, asset, tmp_path, ByteBudget(), get=lambda *a, **k: Response(b"wrong"))
    assert result["reason"] == "downloaded_audio_sha_mismatch"
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.glob("*.download-*")) == []


def test_byte_limit_and_private_errors(tmp_path):
    song, asset = fixture()
    result = download_one(song, asset, tmp_path, ByteBudget(limit=2), get=lambda *a, **k: Response())
    assert result["reason"] == "campaign_download_byte_limit"

    def fail(*args, **kwargs):
        raise RuntimeError(asset["mix_url"])

    result = download_one(song, asset, tmp_path, ByteBudget(), get=fail)
    assert result["reason"] == "RuntimeError"
    assert "secret" not in str(result)


def test_bounded_fetch_ids():
    try:
        fetch_assets(["a" * 12] * 31, "unused")
    except ValueError as error:
        assert str(error) == "invalid_bounded_job_ids"
    else:
        raise AssertionError("must not connect")
