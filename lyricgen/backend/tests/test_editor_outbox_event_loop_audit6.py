"""A slow outbox publish must not stop unrelated requests on the API loop."""
import asyncio
from threading import Event

from database import SessionLocal
from tests.test_editor_documents import _users_and_job


def test_editor_patch_keeps_event_loop_available_during_outbox_dispatch(monkeypatch):
    import main

    user, _, job_id = _users_and_job("outbox_loop_audit6")
    actor = {"id": user.id, "role": "user", "tenant_id": user.tenant_id,
             "features": {"editor_v2": True}}
    started, release, finished = Event(), Event(), Event()
    progress_before_finish = []

    def slow_dispatch(event_id):
        assert event_id
        started.set()
        # Bounded watchdog: the broken implementation must fail, not hang.
        release.wait(timeout=1)
        finished.set()

    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", slow_dispatch)

    async def scenario(db):
        async def unrelated_request():
            while not started.is_set():
                await asyncio.sleep(0)
            progress_before_finish.append(not finished.is_set())
            release.set()
        other = asyncio.create_task(unrelated_request())
        result = await main.patch_editor_document(
            job_id, main.EditorPatchRequest(base_revision=0, checkpoint="manual", segments=[
                {"start": 0, "end": 1, "text": "Changed while outbox is slow"},
            ]), current_user=actor, db=db,
        )
        await other
        return result

    with SessionLocal() as db:
        result = asyncio.run(scenario(db))
    assert result["revision"] == 1
    assert progress_before_finish == [True]


def test_synchronous_outbox_dispatch_remains_synchronous(monkeypatch):
    import inspect
    import main
    import transactional_outbox
    seen = []
    monkeypatch.setattr(transactional_outbox, "dispatch_outbox_event",
                        lambda event_id: seen.append(event_id) or {"status": "dispatched"})
    assert not inspect.iscoroutinefunction(main._dispatch_editor_quality_outbox)
    assert main._dispatch_editor_quality_outbox("sync-event") is None
    assert seen == ["sync-event"]
