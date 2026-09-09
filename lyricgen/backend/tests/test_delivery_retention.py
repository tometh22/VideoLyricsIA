"""Unit tests for the delayed hard-delete path for portal deliveries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import delivery_retention as retention


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0
        self.rollbacks = 0

    def query(self, _model):
        return _Query(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _delivery(*, delivery_id=1, age_days=61, removed_at=None, job_id="job123"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=delivery_id,
        job_id=job_id,
        tenant_snapshot="universal_music",
        file_types=["video", "thumbnail"],
        added_at=now - timedelta(days=age_days),
        removed_at=removed_at,
    )


def test_expired_delivery_is_hidden_and_only_delivery_outputs_are_deleted(monkeypatch):
    row = _delivery()
    session = _Session([row])
    deleted = []
    monkeypatch.setattr(retention, "DeliveriesSessionLocal", lambda: session)
    monkeypatch.setattr(retention.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(retention.storage, "delete_object", deleted.append)

    result = retention.cleanup_expired_deliveries(
        now=datetime.now(timezone.utc),
    )

    assert result["expired"] == 1
    assert result["hidden"] == 1
    assert result["deleted"] == 2
    assert result["failed"] == 0
    assert row.removed_at is not None
    assert deleted == [
        "universal_music/job123/lyric_video.mp4",
        "universal_music/job123/thumbnail.jpg",
    ]
    assert session.commits == 1


def test_failed_r2_delete_keeps_row_eligible_for_the_next_pass(monkeypatch):
    row = _delivery()
    session = _Session([row])
    calls = []

    def fail_once(key):
        calls.append(key)
        raise RuntimeError("temporary R2 outage")

    monkeypatch.setattr(retention, "DeliveriesSessionLocal", lambda: session)
    monkeypatch.setattr(retention.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(retention.storage, "delete_object", fail_once)

    result = retention.cleanup_expired_deliveries(
        now=datetime.now(timezone.utc),
    )

    assert result["failed"] == 2
    assert result["hidden"] == 0
    assert row.removed_at is None
    assert session.commits == 0
    assert calls

