"""Retention is an inventory, never permission to delete shared publications."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import database
import delivery_retention as retention


class _Session:
    def __init__(self, rows):
        self.rows, self.commits, self.rollbacks, self.closed = rows, 0, 0, False

    def query(self, _model):
        return self

    def all(self):
        return list(self.rows)

    def commit(self):
        self.commits += 1
        raise AssertionError("inventory must never commit")

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _delivery(*, delivery_id=1, age_days=61, removed_at=None, job_id="job123", **overrides):
    now = datetime.now(timezone.utc)
    values = dict(id=delivery_id, job_id=job_id, tenant_snapshot="universal_music",
                  portal_id="argentina", file_types=["video", "thumbnail"],
                  published_file_keys=None, added_at=now - timedelta(days=age_days),
                  removed_at=removed_at, content_updated_at=now)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def inventory(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("retention inventory must not access storage")
    monkeypatch.setattr(retention.storage, "delete_object", forbidden)
    monkeypatch.setattr(retention.storage, "object_status", forbidden)
    monkeypatch.setattr(database, "DELIVERIES_DATABASE_URL", "")

    def run(rows, **kwargs):
        before = deepcopy([vars(row) for row in rows])
        session = _Session(rows)
        monkeypatch.setattr(retention, "DeliveriesSessionLocal", lambda: session)
        result = retention.cleanup_expired_deliveries(**kwargs)
        assert session.commits == 0
        assert session.rollbacks == 1 and session.closed
        assert [vars(row) for row in rows] == before
        assert result["deleted"] == result["hidden"] == 0
        return result
    return run


def test_expired_legacy_is_reported_but_never_hidden_or_deleted(inventory):
    result = inventory([_delivery()])
    assert result["would_expire"] == result["expired"] == 1
    assert result["would_delete"] == result["protected"] == 2
    assert result["dry_run"] is True and result["complete"] is True
    assert result["status"] == "dry_run"


@pytest.mark.parametrize("dry_run", [True, False])
def test_shared_database_is_protected_even_when_execution_requested(inventory, monkeypatch, dry_run):
    monkeypatch.setattr(database, "DELIVERIES_DATABASE_URL", "postgresql://isolated-fixture/shared")
    result = inventory([_delivery()], dry_run=dry_run)
    assert result["status"] == "shared_database_protected"
    assert result["execution_requested"] is (not dry_run)
    assert result["dry_run"] is True


def test_local_destructive_execution_is_also_fail_closed(inventory):
    result = inventory([_delivery()], dry_run=False)
    assert result["status"] == "destructive_cleanup_blocked"


def test_snapshot_keys_and_cross_portal_references_are_protected(inventory):
    keys = {"video": "frozen/video.published-123", "thumbnail": "frozen/thumb.published-123"}
    old = _delivery(published_file_keys=keys)
    fresh = _delivery(delivery_id=2, age_days=2, portal_id="chile", published_file_keys=keys)
    result = inventory([old, fresh])
    assert result["snapshot_deliveries"] == result["protected_deliveries"] == 2
    assert result["protected"] == result["shared_references"] == 2
    assert result["would_delete"] == 2 and result["would_expire"] == 1
    assert retention._delivery_keys(old) == list(keys.values())


def test_partial_snapshot_does_not_inventory_mutable_fallback(inventory):
    row = _delivery(published_file_keys={"video": "frozen/video"})
    assert retention._delivery_keys(row) == ["frozen/video"]
    result = inventory([row])
    assert result["unknown"] == 1 and result["protected"] == 1


@pytest.mark.parametrize("manifest", [{}, [], "corrupt", {"video": None, "thumbnail": 9}])
def test_malformed_snapshot_is_unknown_and_never_falls_back(inventory, manifest):
    row = _delivery(published_file_keys=manifest)
    assert retention._delivery_keys(row) == []
    result = inventory([row])
    assert result["unknown"] == 1 and result["protected"] == 0


def test_recent_republication_is_only_legacy_policy_candidate(inventory):
    row = _delivery(age_days=120, content_updated_at=datetime.now(timezone.utc))
    result = inventory([row])
    assert result["would_expire"] == 1
    assert result["policy"] == "inventory_only_pending_shared_domain_retention_review"


def test_removed_snapshots_and_extra_manifest_references_are_protected(inventory):
    row = _delivery(removed_at=datetime.now(timezone.utc) - timedelta(days=90),
                    published_file_keys={"video": "frozen/v", "thumbnail": "frozen/t",
                                         "future_type": "frozen/other"})
    result = inventory([row])
    assert result["protected"] == 3 and result["unknown"] == 0


def test_naive_legacy_timestamps_do_not_disable_inventory(inventory):
    row = _delivery(added_at=datetime.now() - timedelta(days=90))
    result = inventory([row])
    assert result["complete"] and result["would_expire"] == 1


def test_bad_timestamp_fails_closed_without_leaking_exception_text(inventory):
    row = _delivery(added_at="invalid timestamp with credentials")
    result = inventory([row])
    assert result["status"] == "inventory_failed" and not result["complete"]
    assert result["failed"] == 1
    assert "credentials" not in str(result)


def test_failed_rollback_still_closes_session_and_never_reports_success(monkeypatch):
    session = _Session([_delivery()])

    def failed_rollback():
        raise RuntimeError("disconnected database with private connection string")

    session.rollback = failed_rollback
    monkeypatch.setattr(retention, "DeliveriesSessionLocal", lambda: session)
    result = retention.cleanup_expired_deliveries()
    assert session.closed and session.commits == 0
    assert result["status"] == "inventory_failed" and not result["complete"]
    assert result["deleted"] == result["hidden"] == 0
    assert "private connection" not in str(result)
