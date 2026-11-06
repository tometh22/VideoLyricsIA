"""Publication-copy fences with deterministic fault injection; no storage I/O."""
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import storage
from delivery_snapshots import copy_snapshot


def existing(etag="old", size=10):
    return {"status": "exists", "etag": etag, "size": size}


class Objects:
    def __init__(self):
        self.objects = {"t/j/lyric_video.mp4": existing(), "t/j/umg_master.mov": existing("master", 50)}
        self.copied = []
        self.heads = []
        self.after_copy = lambda *_: None

    def identity(self, key):
        self.heads.append(key)
        return dict(self.objects.get(key, {"status": "missing"}))

    def copy(self, source, target, *, expected_etag):
        if self.objects[source]["etag"] != expected_etag:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "CopyObject")
        self.copied.append((source, target, expected_etag))
        self.objects[target] = {**self.objects[source], "etag": "multipart-destination-etag"}
        self.after_copy(source, target)
        return True


@pytest.fixture
def objects(monkeypatch):
    value = Objects()
    monkeypatch.setattr(storage, "object_identity", value.identity)
    monkeypatch.setattr(storage, "copy_object", value.copy)
    return value


def test_all_sources_are_preflighted_before_any_copy_and_etag_is_not_checksum(objects):
    def during_copy(*_):
        assert objects.heads[:2] == ["t/j/lyric_video.mp4", "t/j/umg_master.mov"]
    objects.after_copy = during_copy
    keys = copy_snapshot("t", "j", ["video", "umg_master"])
    assert set(keys) == {"video", "umg_master"}
    assert [row[2] for row in objects.copied] == ["old", "master"]
    assert all(objects.objects[key]["etag"] == "multipart-destination-etag" for key in keys.values())
    assert objects.heads[-2:] == ["t/j/lyric_video.mp4", "t/j/umg_master.mov"]


@pytest.mark.parametrize("status", ["missing", "unavailable"])
def test_preflight_late_missing_or_unknown_file_starts_no_copy(objects, status):
    objects.objects["t/j/umg_master.mov"] = {"status": status}
    with pytest.raises(RuntimeError, match="verificar"):
        copy_snapshot("t", "j", ["video", "umg_master"])
    assert objects.copied == []


@pytest.mark.parametrize("change", [existing("changed", 10), existing("old", 11), {"status": "unavailable"}, {"status": "missing"}])
def test_source_changes_after_its_copy_are_detected_at_end(objects, change):
    def mutate(source, target):
        if source.endswith(".mov"):
            objects.objects["t/j/lyric_video.mp4"] = change
    objects.after_copy = mutate
    with pytest.raises(RuntimeError, match="cambiaron"):
        copy_snapshot("t", "j", ["video", "umg_master"])
    assert len(objects.copied) == 2


@pytest.mark.parametrize("destination", [{"status": "unavailable"}, {"status": "missing"}, existing("new", 9)])
def test_destination_unknown_missing_or_wrong_size_is_not_publishable(objects, destination):
    objects.after_copy = lambda source, target: objects.objects.update({target: destination})
    with pytest.raises(RuntimeError, match="copia"):
        copy_snapshot("t", "j", ["video"])


def test_change_between_preflight_and_copy_fails_conditional_copy(objects, monkeypatch):
    def copy(source, target, **kwargs):
        objects.objects[source] = existing("new", 10)
        return objects.copy(source, target, **kwargs)
    monkeypatch.setattr(storage, "copy_object", copy)
    with pytest.raises(ClientError, match="PreconditionFailed"):
        copy_snapshot("t", "j", ["video"])
    assert objects.copied == []


def test_allow_missing_only_accepts_stable_known_missing(objects):
    objects.objects.pop("t/j/umg_master.mov")
    keys = copy_snapshot("t", "j", ["video", "umg_master"], allow_missing=True)
    assert set(keys) == {"video"}
    objects.after_copy = lambda *_: objects.objects.update({"t/j/umg_master.mov": existing("new", 50)})
    with pytest.raises(RuntimeError, match="cambiaron"):
        copy_snapshot("t", "j", ["video", "umg_master"], allow_missing=True)


@pytest.mark.parametrize("status", ["unavailable", "missing"])
def test_allow_missing_does_not_accept_unknown(objects, status):
    objects.objects["t/j/lyric_video.mp4"] = {"status": status}
    if status == "missing":
        assert copy_snapshot("t", "j", ["video"], allow_missing=True) == {}
    else:
        with pytest.raises(RuntimeError):
            copy_snapshot("t", "j", ["video"], allow_missing=True)


@pytest.mark.parametrize("filename", ["video.mp4", "master.mov"])
def test_single_and_managed_copy_carry_same_source_precondition(monkeypatch, filename):
    calls = []
    client = SimpleNamespace(copy_object=lambda **kw: calls.append(("single", kw)),
                             copy=lambda **kw: calls.append(("managed", kw)))
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    monkeypatch.setattr(storage, "object_exists", lambda _: pytest.fail("conditional copy must not use unbounded preflight"))
    assert storage.copy_object(filename, "published", expected_etag='"version"')
    mode, params = calls[0]
    condition = params if mode == "single" else params["ExtraArgs"]
    assert condition["CopySourceIfMatch"] == '"version"'


@pytest.mark.parametrize("error", [ClientError({"Error": {"Code": "EntityTooLarge"}}, "CopyObject"),
                                  ReadTimeoutError(endpoint_url="https://synthetic.invalid")])
def test_multipart_fallback_never_drops_precondition(monkeypatch, error):
    calls = []
    def single(**kwargs):
        assert kwargs["CopySourceIfMatch"] == '"old"'
        raise error
    client = SimpleNamespace(copy_object=single, copy=lambda **kw: calls.append(kw))
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    assert storage.copy_object("video.mp4", "published", expected_etag="old")
    assert calls[0]["ExtraArgs"] == {"CopySourceIfMatch": '"old"'}


def test_sdk_managed_copy_supports_source_precondition():
    from boto3.s3.transfer import S3Transfer
    allowed = getattr(S3Transfer, "ALLOWED_COPY_ARGS", None)
    if allowed is None:
        pytest.skip("installed boto3 does not expose the managed-copy allowlist")
    assert "CopySourceIfMatch" in allowed


def test_precondition_failure_is_not_retried_without_condition(monkeypatch):
    def single(**kwargs):
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "CopyObject")
    client = SimpleNamespace(copy_object=single, copy=lambda **_: pytest.fail("must not retry failed identity"))
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    with pytest.raises(ClientError):
        storage.copy_object("video.mp4", "published", expected_etag="old")


@pytest.mark.parametrize("head,expected", [
    ({"ETag": '"e"', "ContentLength": 5}, existing("e", 5)),
    ({"ETag": '"e"'}, {"status": "unavailable"}),
    ({"ContentLength": 5}, {"status": "unavailable"}),
    ({"ETag": '""', "ContentLength": 5}, {"status": "unavailable"}),
    ({"ETag": '"e"', "ContentLength": True}, {"status": "unavailable"}),
    ({"ETag": '"e"', "ContentLength": -1}, {"status": "unavailable"}),
])
def test_identity_requires_valid_etag_and_size(monkeypatch, head, expected):
    monkeypatch.setattr(storage, "_get_metadata_client", lambda: SimpleNamespace(head_object=lambda **_: head))
    assert storage.object_identity("key") == expected


@pytest.mark.parametrize("code,status", [("404", "missing"), ("NoSuchKey", "missing"), ("403", "unavailable"), ("503", "unavailable")])
def test_identity_error_classification_is_tristate(monkeypatch, code, status):
    def head(**_):
        raise ClientError({"Error": {"Code": code}}, "HeadObject")
    monkeypatch.setattr(storage, "_get_metadata_client", lambda: SimpleNamespace(head_object=head))
    assert storage.object_identity("key") == {"status": status}


def test_metadata_client_has_independent_small_bounded_pool(monkeypatch):
    import boto3
    calls = []
    fake = object()
    monkeypatch.setattr(storage, "_metadata_client", None)
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: calls.append(kwargs) or fake)
    assert storage._get_metadata_client() is fake
    assert storage._get_metadata_client() is fake
    assert len(calls) == 1
    config = calls[0]["config"]
    assert (config.connect_timeout, config.read_timeout, config.max_pool_connections) == (3, 5, 8)
    assert config.retries["total_max_attempts"] == 1
