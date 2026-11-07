"""Version GETs must release bridge locks before ASGI dependency teardown."""
import asyncio

import pytest
from fastapi import HTTPException

from database import EditorDocument, EditorVersion, SessionLocal
from tests.test_editor_documents import _users_and_job


@pytest.mark.parametrize("operation", ["list", "detail", "missing", "query_failure"])
def test_version_reads_finish_transaction_before_return(operation, monkeypatch):
    import main

    user, _, job_id = _users_and_job("version_read_audit6")
    actor = {"id": user.id, "role": "user", "tenant_id": user.tenant_id,
             "features": {"editor_v2": True}}
    with SessionLocal() as lookup:
        version_id = lookup.query(EditorVersion).filter_by(job_id=job_id).first().id
    with SessionLocal() as db:
        if operation == "query_failure":
            def broken_query(*args, **kwargs):
                raise RuntimeError("simulated query failure")
            monkeypatch.setattr(main, "list_versions", broken_query)
        if operation in {"list", "query_failure"}:
            call = main.get_editor_versions(job_id, limit=50, offset=0, current_user=actor, db=db)
        else:
            call = main.get_editor_version(job_id, "missing" if operation == "missing" else version_id,
                                           current_user=actor, db=db)
        if operation == "missing":
            with pytest.raises(HTTPException) as error:
                asyncio.run(call)
            assert error.value.status_code == 404
        elif operation == "query_failure":
            with pytest.raises(RuntimeError, match="simulated query failure"):
                asyncio.run(call)
        else:
            payload = asyncio.run(call)
            assert payload["versions"] if operation == "list" else payload["id"] == version_id
        # This runs BEFORE closing the dependency/session, where a second
        # same-loop request previously blocked forever on the retained lock.
        assert not db.in_transaction()


def test_version_list_commits_lazy_document_creation():
    import main

    user, _, job_id = _users_and_job("version_lazy_audit6")
    actor = {"id": user.id, "role": "user", "tenant_id": user.tenant_id,
             "features": {"editor_v2": True}}
    with SessionLocal() as db:
        db.query(EditorVersion).filter_by(job_id=job_id).delete()
        db.query(EditorDocument).filter_by(job_id=job_id).delete()
        db.commit()
        result = asyncio.run(main.get_editor_versions(job_id, limit=50, offset=0, current_user=actor, db=db))
        assert result["versions"]
        assert not db.in_transaction()
    with SessionLocal() as check:
        assert check.query(EditorDocument).filter_by(job_id=job_id).one()
        assert check.query(EditorVersion).filter_by(job_id=job_id).count() == 1
