from types import SimpleNamespace

import delivery_integrity


def delivery(**overrides):
    values = {
        "id": 11, "job_id": "abc123", "portal_id": "chile",
        "tenant_snapshot": "tenant", "file_types": ["video"],
        "file_sizes": {"video": 1024},
        "published_file_keys": {"video": "published/tenant/abc123/hash/lyric_video.mp4"},
        "published_file_etags": {"video": "etag-1"},
        "published_manifest_hash": "hash", "published_revision": 2,
        "stale_since": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_exact_manifest_is_healthy(monkeypatch):
    monkeypatch.setattr(delivery_integrity.storage, "object_status", lambda _: "exists")
    monkeypatch.setattr(delivery_integrity.storage, "object_etag", lambda _: "etag-1")
    monkeypatch.setattr(delivery_integrity.storage, "head_object_size", lambda _: 1024)
    result = delivery_integrity.inspect_delivery(delivery())
    assert result["status"] == "ok"


def test_legacy_and_network_failure_are_unknown(monkeypatch):
    assert delivery_integrity.inspect_delivery(delivery(published_manifest_hash=None))["status"] == "unknown"
    monkeypatch.setattr(delivery_integrity.storage, "object_status", lambda _: "unavailable")
    assert delivery_integrity.inspect_delivery(delivery())["status"] == "unknown"


def test_missing_object_or_changed_bytes_fail(monkeypatch):
    monkeypatch.setattr(delivery_integrity.storage, "object_status", lambda _: "missing")
    assert delivery_integrity.inspect_delivery(delivery())["status"] == "failed"
    monkeypatch.setattr(delivery_integrity.storage, "object_status", lambda _: "exists")
    monkeypatch.setattr(delivery_integrity.storage, "object_etag", lambda _: "other")
    monkeypatch.setattr(delivery_integrity.storage, "head_object_size", lambda _: 1024)
    assert delivery_integrity.inspect_delivery(delivery())["status"] == "failed"


def test_key_from_a_different_manifest_fails_even_if_object_exists(monkeypatch):
    monkeypatch.setattr(delivery_integrity.storage, "object_status", lambda _: "exists")
    row = delivery(published_file_keys={
        "video": "published/tenant/abc123/other-hash/lyric_video.mp4",
    })
    result = delivery_integrity.inspect_delivery(row)
    assert result["status"] == "failed"
    assert result["files"][0]["reason"] == "manifest_key_missing"
