"""The real reaper control flow must close its caller resources before HEAD."""
from types import SimpleNamespace

import pytest

import delivery_integrity
import reaper


@pytest.fixture
def isolated_sweep(monkeypatch):
    events = []

    class Connection:
        closed = False

        def execute(self, statement, _params):
            events.append("unlock" if "unlock" in str(statement) else "lock")
            return SimpleNamespace(scalar=lambda: True)

        def commit(self):
            pass

        def close(self):
            self.closed = True
            events.append("lock_closed")

    connection = Connection()

    class Session:
        closed = False
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)

        def close(self):
            self.closed = True
            events.append("session_closed")

        def rollback(self):
            pass

    session = Session()
    monkeypatch.setattr(reaper, "SessionLocal", lambda: session)
    monkeypatch.setattr(reaper.time, "time", lambda: 1000)
    monkeypatch.setattr(reaper, "_last_multipart_sweep_ts", 1000)
    monkeypatch.setattr(reaper, "_last_delivery_retention_sweep_ts", 1000)
    monkeypatch.setattr(reaper, "_last_delivery_audit_ts", 0)
    monkeypatch.setattr(reaper, "_DELIVERY_AUDIT_INTERVAL_S", 1)
    for name in ("find_queues_without_consumer", "find_stuck_jobs", "find_orphan_polling_jobs",
                 "find_stalled_renders", "find_abandoned_transcribed", "find_abandoned_uploads",
                 "find_abandoned_edits", "find_stuck_transcriptions"):
        monkeypatch.setattr(reaper, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(reaper, "_alert_queues_without_consumer", lambda *args: None)
    monkeypatch.setattr(reaper, "remind_stale_pending_review", lambda *args: None)
    monkeypatch.setattr(delivery_integrity, "log_audit", lambda report: None)
    return session, connection, events


def test_empty_sweep_runs_due_audit_only_after_work_session_and_lock_closed(monkeypatch, isolated_sweep):
    session, connection, events = isolated_sweep

    def audit():
        assert session.closed and connection.closed
        assert events.index("unlock") < events.index("lock_closed") < events.index("session_closed")
        events.append("audit")
        return {}

    monkeypatch.setattr(delivery_integrity, "audit_active_deliveries", audit)
    assert reaper._reap_all_stuck_inner(180) == 0
    assert events[-1] == "audit"
    assert reaper._last_delivery_audit_ts == 1000


def test_failed_sweep_closes_resources_and_defers_audit(monkeypatch, isolated_sweep):
    session, connection, events = isolated_sweep

    def fail(_db, _threshold):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(reaper, "find_stuck_jobs", fail)
    monkeypatch.setattr(delivery_integrity, "audit_active_deliveries", lambda: events.append("audit"))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        reaper._reap_all_stuck_inner(180)
    assert session.closed and connection.closed
    assert "audit" not in events
    assert reaper._last_delivery_audit_ts == 0
