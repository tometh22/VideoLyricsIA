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


def test_object_etag_is_normalized(monkeypatch):
    class Client:
        def head_object(self, **kwargs):
            assert kwargs["Key"] == "inputs/key"
            return {"ETag": '"multipart-etag-2"'}

    monkeypatch.setattr(storage, "_get_client", lambda: Client())
    monkeypatch.setattr(storage, "R2_BUCKET", "bucket")
    assert storage.object_etag("inputs/key") == "multipart-etag-2"
