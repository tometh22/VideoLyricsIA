"""A job whose R2 files a client portal still serves cannot be deleted."""
import jobs
from database import Delivery, Job as JobModel, SessionLocal
from tests.conftest import auth
from tests.test_edit_metadata import _admin_identity, _create_pending_review_job


def _deliver(db, job_id, tenant_id, user_id, keys=None):
    row = Delivery(job_id=job_id, label="Campaña", file_types=["video"], artist_snapshot="A", song_title_snapshot="S",
                   tenant_snapshot=tenant_id, added_by_user_id=user_id, portal_id="argentina", published_file_keys=keys)
    db.add(row); db.commit()
    return row.id


def _drop(ids):
    with SessionLocal() as db:
        db.query(Delivery).filter(Delivery.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def _exists(job_id):
    with SessionLocal() as db:
        return db.query(JobModel).filter_by(job_id=job_id).first() is not None


def test_a_job_served_from_its_own_files_is_not_deleted(db, monkeypatch):
    monkeypatch.setattr(jobs.storage, "delete_object", lambda key: (_ for _ in ()).throw(AssertionError("must not touch R2")))
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="error", s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_deliver(db, job_id, tenant_id, user_id, keys=None)]
    try:
        assert jobs.delete_job(db, job_id, tenant_id) == (False, "published_in_portal")
        assert _exists(job_id)
    finally:
        _drop(ids)
        jobs.delete_job(db, job_id, tenant_id)


def test_a_delivery_with_a_snapshot_does_not_depend_on_the_working_files(db, monkeypatch):
    deleted = []
    monkeypatch.setattr(jobs.storage, "delete_object", lambda key: deleted.append(key))
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="error", s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_deliver(db, job_id, tenant_id, user_id, keys={"video": "t/j/lyric_video.mp4.published-abc"})]
    try:
        assert jobs.delete_job(db, job_id, tenant_id) == (True, "ok")
        assert "t/j/lyric_video.mp4" in deleted and not _exists(job_id)
    finally:
        _drop(ids)


def test_a_removed_delivery_does_not_protect_the_job(db, monkeypatch):
    monkeypatch.setattr(jobs.storage, "delete_object", lambda key: None)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="error", s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_deliver(db, job_id, tenant_id, user_id, keys=None)]
    try:
        from datetime import datetime, timezone
        with SessionLocal() as other:
            other.get(Delivery, ids[0]).removed_at = datetime.now(timezone.utc); other.commit()
        assert jobs.delete_job(db, job_id, tenant_id) == (True, "ok")
    finally:
        _drop(ids)


def test_bulk_delete_skips_only_the_published_ones(db, monkeypatch):
    monkeypatch.setattr(jobs.storage, "delete_object", lambda key: None)
    user_id, tenant_id = _admin_identity(db)
    published = _create_pending_review_job(db, tenant_id, user_id, status="error", s3_keys={"video": "t/p/lyric_video.mp4"})
    junk = _create_pending_review_job(db, tenant_id, user_id, status="error")
    ids = [_deliver(db, published, tenant_id, user_id, keys=None)]
    try:
        result = jobs.bulk_delete_jobs(db, [published, junk], tenant_id)
        assert result["deleted"] == [junk] and result["skipped"] == {published: "published_in_portal"}
    finally:
        _drop(ids)
        jobs.delete_job(db, published, tenant_id)


def test_an_unreadable_deliveries_database_blocks_the_delete(db, monkeypatch):
    def broken():
        raise RuntimeError("deliveries db down")

    monkeypatch.setattr("database.scoped_deliveries_db", broken)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="error")
    try:
        assert jobs.delete_job(db, job_id, tenant_id) == (False, "published_in_portal")
    finally:
        monkeypatch.undo()
        jobs.delete_job(db, job_id, tenant_id)


def test_the_route_explains_why_it_refuses(client, admin_token, db, monkeypatch):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="error", s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_deliver(db, job_id, tenant_id, user_id, keys=None)]
    try:
        response = client.delete(f"/jobs/{job_id}", headers=auth(admin_token))
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "published_in_portal"
        assert "Retirá la entrega" in response.json()["detail"]["message"]
    finally:
        _drop(ids)
        jobs.delete_job(db, job_id, tenant_id)
