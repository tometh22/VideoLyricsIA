"""An accepted /edit marks the job's deliveries as changing right away."""
from database import Delivery, SessionLocal
from tests.conftest import auth
from tests.test_edit_metadata import _admin_identity, _capture_enqueue_calls, _create_pending_review_job


def _publish_row(db, job_id, tenant_id, user_id, portal="argentina"):
    row = Delivery(job_id=job_id, label="Campaña", file_types=["video"], artist_snapshot="A", song_title_snapshot="S",
                   tenant_snapshot=tenant_id, added_by_user_id=user_id, portal_id=portal, published_revision=1)
    db.add(row); db.commit()
    return row.id


def _state(delivery_id):
    with SessionLocal() as db:
        row = db.get(Delivery, delivery_id)
        return row.stale_since is not None, row.stale_reason


def _cleanup(ids):
    with SessionLocal() as db:
        db.query(Delivery).filter(Delivery.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_accepting_an_edit_marks_every_portal_row_of_the_job_in_flight(client, admin_token, db, monkeypatch):
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_publish_row(db, job_id, tenant_id, user_id, "argentina"), _publish_row(db, job_id, tenant_id, user_id, "chile")]
    try:
        assert [_state(i) for i in ids] == [(False, None), (False, None)]
        response = client.post(f"/edit/{job_id}", headers=auth(admin_token), json={"edit_type": "metadata", "artist": "Sin Gamulán"})
        assert response.status_code == 202, response.text
        assert [_state(i) for i in ids] == [(True, "editing"), (True, "editing")]
    finally:
        _cleanup(ids)


def test_a_rejected_edit_marks_nothing(client, admin_token, db, monkeypatch):
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, s3_keys={"video": "t/j/lyric_video.mp4"})
    ids = [_publish_row(db, job_id, tenant_id, user_id)]
    try:
        response = client.post(f"/edit/{job_id}", headers=auth(admin_token), json={"edit_type": "metadata", "artist": "   "})
        assert response.status_code >= 400
        assert _state(ids[0]) == (False, None)
    finally:
        _cleanup(ids)
