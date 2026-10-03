from types import SimpleNamespace

import pytest

import delivery_manifest


def job():
    return SimpleNamespace(
        job_id="abc123", tenant_id="tenant", s3_keys={"video": "tenant/abc123/lyric_video.mp4"},
        previous_versions=[], edit_count=0, segments_revision=0,
        render_params={}, umg_spec=None, input_audio_sha256=None,
        input_r2_key="audio", artist="Artist", song_title="Song",
    )


def test_snapshot_stamps_source_identity_and_reuses_verified_copy(monkeypatch):
    copied = []
    metadata = {}
    monkeypatch.setattr(delivery_manifest.storage, "head_object_size", lambda _: 1024)
    monkeypatch.setattr(delivery_manifest.storage, "object_etag",
                        lambda key: "copy-etag" if key.startswith("published/") else "source-etag")
    monkeypatch.setattr(delivery_manifest.storage, "object_source_etag",
                        lambda key: metadata.get(key))

    def copy(src, dst, *, source_etag=None):
        copied.append((src, dst, source_etag))
        metadata[dst] = source_etag
        return True

    monkeypatch.setattr(delivery_manifest.storage, "copy_object", copy)
    first = delivery_manifest.freeze_manifest(job(), ["video"])
    second = delivery_manifest.freeze_manifest(job(), ["video"])
    assert first.manifest_hash == second.manifest_hash
    assert len(copied) == 1
    assert copied[0][2] == "source-etag"
    assert first.source_etags == {"video": "source-etag"}


def test_snapshot_refuses_source_changed_during_copy(monkeypatch):
    reads = 0
    monkeypatch.setattr(delivery_manifest.storage, "head_object_size", lambda _: 1024)
    monkeypatch.setattr(delivery_manifest.storage, "object_source_etag", lambda _: "source-etag")
    monkeypatch.setattr(delivery_manifest.storage, "copy_object", lambda *a, **k: True)

    def etag(key):
        nonlocal reads
        if key.startswith("published/"):
            return "copy-etag"
        reads += 1
        return "source-etag" if reads == 1 else "changed-etag"

    monkeypatch.setattr(delivery_manifest.storage, "object_etag", etag)
    with pytest.raises(delivery_manifest.ManifestUnavailable):
        delivery_manifest.freeze_manifest(job(), ["video"])
