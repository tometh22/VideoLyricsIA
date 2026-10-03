import pytest

import storage


def test_content_addressed_audio_key_is_canonical_and_tenant_scoped():
    digest = "a" * 64
    key = storage.content_addressed_input_key(
        "Tenant / uno", "job:42", digest, "Mi canción (live).wav",
    )
    assert key == (
        "inputs/Tenant_uno/job_42/sha256/"
        f"{digest}/Mi_canci_n_live_.wav"
    )


@pytest.mark.parametrize("digest", ["", "a" * 63, "g" * 64])
def test_content_addressed_audio_key_rejects_noncanonical_digest(digest):
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        storage.content_addressed_input_key("tenant", "job", digest, "song.wav")


def test_content_addressed_audio_key_normalizes_digest_case():
    key = storage.content_addressed_input_key(
        "tenant", "job", "A" * 64, "song.wav",
    )
    assert "/sha256/" + "a" * 64 + "/" in key


def test_editor_preview_key_is_shared_and_versioned():
    digest = "B" * 64
    assert storage.editor_audio_preview_key(digest) == (
        "editor-previews/" + "b" * 64 + "/aac-stereo-96k-v1.m4a"
    )
    assert storage.editor_audio_preview_key(
        digest, "aac-stereo-128k-v2",
    ) == "editor-previews/" + "b" * 64 + "/aac-stereo-128k-v2.m4a"


@pytest.mark.parametrize("version", ["", "../escape", "a/b", "a" * 81])
def test_editor_preview_key_rejects_untrusted_format_version(version):
    with pytest.raises(ValueError, match="format_version"):
        storage.editor_audio_preview_key("a" * 64, version)


def test_object_etag_is_normalized(monkeypatch):
    class Client:
        def head_object(self, **kwargs):
            assert kwargs["Key"] == "inputs/key"
            return {"ETag": '"multipart-etag-2"'}

    monkeypatch.setattr(storage, "_get_client", lambda: Client())
    monkeypatch.setattr(storage, "R2_BUCKET", "bucket")
    assert storage.object_etag("inputs/key") == "multipart-etag-2"


def test_portal_copy_stamps_source_etag_in_single_and_multipart_paths(monkeypatch):
    from botocore.exceptions import ClientError

    class Client:
        def __init__(self):
            self.simple = []
            self.multipart = []
            self.too_large = False

        def copy_object(self, **kwargs):
            self.simple.append(kwargs)
            if self.too_large:
                raise ClientError({"Error": {"Code": "EntityTooLarge"}}, "CopyObject")

        def copy(self, **kwargs):
            self.multipart.append(kwargs)

    client = Client()
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    monkeypatch.setattr(storage, "object_exists", lambda _key: True)
    monkeypatch.setattr(storage, "R2_BUCKET", "bucket")

    assert storage.copy_object("source.mp4", "published/cut.mp4", source_etag="source-v1")
    assert client.simple[-1]["Metadata"] == {"genly-source-etag": "source-v1"}
    client.too_large = True
    assert storage.copy_object("source.mov", "published/cut.mov", source_etag="source-v2")
    assert client.multipart[-1]["ExtraArgs"]["Metadata"] == {
        "genly-source-etag": "source-v2",
    }
    assert client.multipart[-1]["ExtraArgs"]["MetadataDirective"] == "REPLACE"
