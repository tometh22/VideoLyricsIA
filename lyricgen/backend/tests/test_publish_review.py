"""Resumen antes de publicar (PUBLISH_REVIEW_SUMMARY): líneas que nadie
escuchó y avisos precisos de la Revisión rápida sin mirar, en todos los
caminos de publicación. Letras inventadas."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import publish_review as pr
from database import AuditLog, DeliveryBatchItem, EditorDocument, Job, ProductEvent, SessionLocal
from delivery_freshness import render_fingerprint
from tests.conftest import auth
from tests.test_art_track_campaigns import _campaign_for, _seed_campaign_job, clean_art_rows  # noqa: F401
from tests.test_bulk_delivery_robustness import KEYS, storage_ready  # noqa: F401
from tests.test_change_request_review_api import _published_without_ticking, manual_request  # noqa: F401
from tests.test_deliveries import _portal_token_env, _publication_copy_succeeds, all_r2_files_present, approved_job  # noqa: F401


def seg(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


LYRICS = [
    seg(10.0, 13.0, "Camino por la orilla sin mirar atrás", segment_id="a"),
    seg(13.2, 16.0, "y el río me devuelve lo que fui", segment_id="b"),
    seg(16.0, 16.0, "", segment_id="vacia"),
    seg(20.0, 24.0, "Mañana vuelvo al barrio de los dos", segment_id="c"),
]


# --------------------------------------------------------------------------
# Puro
# --------------------------------------------------------------------------

def test_mode_defaults_to_observe_and_ignores_unknown_values(monkeypatch):
    monkeypatch.delenv("PUBLISH_REVIEW_SUMMARY", raising=False)
    assert pr.mode() == "observe"
    for value, expected in (("prompt", "prompt"), (" OFF ", "off"), ("enforce", "observe"), ("", "observe")):
        monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", value)
        assert pr.mode() == expected


@pytest.mark.parametrize("item, category", [
    ({"kind": "heard_different", "sources": ["official"]}, "official"),
    ({"kind": "missing", "sources": ["gemini", "official"]}, "official"),
    ({"kind": "heard_different", "sources": ["gemini", "witness"]}, "two_ears"),
    # Un solo oído automático: se ofrece escuchar, no es un aviso preciso.
    ({"kind": "heard_different", "sources": ["gemini"], "listen": True}, None),
    ({"kind": "missing", "sources": ["witness"]}, None),
    ({"kind": "terminal_period", "style_rule": True, "required": True}, "style"),
    ({"kind": "question_marks", "style_rule": True, "required": True}, "style"),
    ({"kind": "accent_missing", "style_rule": True, "required": True}, "style"),
    # "¿Que el mundo gira?" se sugiere, no se exige; los paréntesis tampoco.
    ({"kind": "question_marks", "style_rule": True, "required": False}, None),
    ({"kind": "parentheses", "style_rule": True, "required": False}, None),
    ({"kind": "question_marks", "style_rule": False, "required": True}, None),
    ({"kind": "orphan_word", "required": False}, None),
    ({"kind": "heard_different", "sources": ["official"], "dismissed": True}, None),
])
def test_only_high_precision_points_count(item, category):
    assert pr.warning_category(item) == category


def test_unheard_lines_use_half_of_each_line_and_skip_empty_lines():
    # Sonó 10-12 s (2/3 de la línea a), 13.2-14 s (0,8 de 2,8 s de b) y nada de c.
    total, unheard = pr.unheard_lines(LYRICS, [[10000, 12000], [13200, 14000]])
    assert total == 3
    assert [line["segment_id"] for line in unheard] == ["b", "c"]
    assert unheard[1] == {"line_index": 3, "segment_id": "c", "start": 20.0, "end": 24.0,
                          "text": "Mañana vuelvo al barrio de los dos"}
    assert pr.unheard_lines(LYRICS, [[0, 30000]])[1] == []


def test_token_changes_with_the_revision_the_lines_or_the_warnings():
    review = {"items": [{"id": "w1", "kind": "terminal_period", "style_rule": True, "required": True,
                         "occurrences": [{"line_index": 0, "start": 10, "end": 13, "before": "x.", "after": "x"}]}]}
    base = pr.summarize("job1", LYRICS, revision=4, ranges=[], listening_status="measured", review=review)
    assert base["needs_attention"] and base["lines_unheard"] == 3 and base["warnings_count"] == 1
    assert base["warnings_by_category"] == {"official": 0, "two_ears": 0, "style": 1}
    same = pr.summarize("job1", LYRICS, revision=4, ranges=[], listening_status="measured", review=review)
    assert same["token"] == base["token"]
    assert pr.summarize("job1", LYRICS, revision=5, ranges=[], listening_status="measured",
                        review=review)["token"] != base["token"]
    assert pr.summarize("job1", LYRICS, revision=4, ranges=[[10000, 13000]], listening_status="measured",
                        review=review)["token"] != base["token"]
    clean = pr.summarize("job2", LYRICS, revision=4, ranges=[[0, 30000]], listening_status="measured", review={})
    assert not clean["needs_attention"]
    assert pr.pending({"job1": base, "job2": clean}, {}) == [base]
    assert pr.pending({"job1": base}, {"job1": base["token"]}) == []
    assert pr.pending({"job1": base}, {"job1": clean["token"]}) == [base]
    assert pr.totals([base, clean]) == {"songs": 2, "songs_needing_attention": 1, "lines_unheard": 3,
                                        "lines_total": 6, "warnings": 1}


# --------------------------------------------------------------------------
# Con base
# --------------------------------------------------------------------------

def _document(db, job, segments=LYRICS, revision=3):
    db.query(EditorDocument).filter_by(job_id=job.job_id).delete()
    db.add(EditorDocument(job_id=job.job_id, tenant_id=job.tenant_id, current_segments=segments,
                          original_segments=segments, revision=revision))
    db.commit()


def _played(db, job, ranges, user_id=None):
    db.add(ProductEvent(tenant_id=job.tenant_id, user_id=user_id, job_id=job.job_id, name="editor_audio_played",
                        occurred_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                        properties={"ranges": ranges, "played_ms": 1, "playback_rate": 1}))
    db.commit()


def _drop_review_audits(db, jobs):
    for row in db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION).all():
        if (row.detail or {}).get("job_id") in jobs or not (row.detail or {}).get("job_id"):
            db.delete(row)


@pytest.fixture
def events_cleanup():
    jobs = []
    yield jobs
    with SessionLocal() as db:
        if jobs:
            db.query(ProductEvent).filter(ProductEvent.job_id.in_(jobs)).delete(synchronize_session=False)
            db.query(EditorDocument).filter(EditorDocument.job_id.in_(jobs)).delete(synchronize_session=False)
            _drop_review_audits(db, set(jobs))
            db.commit()


def _review_audits(db, job_id):
    return [row for row in db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION).order_by(AuditLog.id)
            if (row.detail or {}).get("job_id") == job_id]


def test_build_reads_playback_of_every_user_and_reports_the_status(db, approved_job, events_cleanup):
    events_cleanup.append(approved_job.job_id)
    assert pr.build_for_jobs(db, [approved_job])[approved_job.job_id]["listening_status"] == "no_editor_activity"
    _document(db, approved_job)
    db.add(ProductEvent(tenant_id=approved_job.tenant_id, job_id=approved_job.job_id, user_id=approved_job.user_id,
                        name="editor_activity_heartbeat", properties={"task": "edit"}))
    db.commit()
    summary = pr.build_for_jobs(db, [approved_job])[approved_job.job_id]
    # Sin tramos no se afirma que nadie escuchó: queda para el audit.
    assert summary["listening_status"] == "no_playback" and summary["listening_known"] is False
    assert summary["lines_unheard"] == 0 and summary["lines_unheard_raw"] == 3 and not summary["needs_attention"]
    _played(db, approved_job, [[9000, 16500]], user_id=approved_job.user_id)
    summary = pr.build_for_jobs(db, [approved_job])[approved_job.job_id]
    assert summary["listening_status"] == "measured" and summary["lines_unheard"] == 1
    _played(db, approved_job, [[21000, 23500]])  # otra persona, otra sesión
    summary = pr.build_for_jobs(db, [approved_job])[approved_job.job_id]
    assert summary["listening_status"] == "measured"
    assert summary["lines_total"] == 3 and summary["lines_unheard"] == 0
    assert summary["editor_revision"] == 3


def test_a_listener_whose_ranges_never_arrived_makes_the_coverage_unknown(db, approved_job, events_cleanup):
    """19-nov: el heartbeat del servidor decía task='listen' pero los tramos
    de /analytics/events no llegaban; no se acusa a nadie de no escuchar."""
    events_cleanup.append(approved_job.job_id)
    _document(db, approved_job)
    _played(db, approved_job, [[9000, 13000]])  # sin usuario: otra sesión
    db.add(ProductEvent(tenant_id=approved_job.tenant_id, job_id=approved_job.job_id, user_id=approved_job.user_id,
                        name="editor_activity_heartbeat", properties={"task": "listen"}))
    db.commit()
    summary = pr.build_for_jobs(db, [approved_job])[approved_job.job_id]
    assert summary["listening_status"] == "incomplete" and summary["listening_known"] is False
    assert summary["unheard"] == [] and summary["lines_unheard_raw"] == 2


def test_art_tracks_have_no_summary(db, approved_job):
    approved_job.render_params = {"art_track": True}
    assert pr.build_for_jobs(db, [approved_job]) == {}


def test_observe_publishes_and_records_without_asking(client, admin_token, approved_job, db, all_r2_files_present,
                                                      events_cleanup, monkeypatch):
    monkeypatch.delenv("PUBLISH_REVIEW_SUMMARY", raising=False)
    events_cleanup.append(approved_job.job_id)
    _document(db, approved_job)
    response = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    [audit] = _review_audits(db, approved_job.job_id)
    assert audit.detail["mode"] == "observe" and audit.detail["shown"] is False
    assert audit.detail["published_anyway"] is False and audit.detail["source"] == "admin_publish"
    assert audit.detail["lines_total"] == 3 and audit.detail["listening_status"] == "no_editor_activity"
    assert audit.detail["lines_unheard"] == 0 and audit.detail["lines_unheard_raw"] == 3
    assert audit.detail["delivery_id"] == response.json()["delivery_id"]
    assert audit.user_id is not None


def test_prompt_shows_the_summary_once_and_publish_anyway_is_audited(client, admin_token, approved_job, db,
                                                                     all_r2_files_present, events_cleanup, monkeypatch):
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "prompt")
    events_cleanup.append(approved_job.job_id)
    _document(db, approved_job, segments=[*LYRICS[:3], seg(20.0, 24.0, "Mañana vuelvo al barrio de los dos.",
                                                         segment_id="c")])
    _played(db, approved_job, [[9000, 16500]])
    url = f"/admin/deliveries/from-job/{approved_job.job_id}"
    first = client.post(url, headers=auth(admin_token), json={})
    assert first.status_code == 409, first.text
    detail = first.json()["detail"]
    assert detail["code"] == "publish_review_required"
    [summary] = detail["summaries"]
    assert summary["job_id"] == approved_job.job_id and summary["lines_unheard"] == 1
    assert summary["unheard"][0]["start"] == 20.0
    assert detail["totals"]["lines_unheard"] == 1
    assert not _review_audits(db, approved_job.job_id)  # nada se publicó ni se registró

    stale = client.post(url, headers=auth(admin_token), json={"publish_review_token": "0" * 64})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "publish_review_required"

    anyway = client.post(url, headers=auth(admin_token), json={"publish_review_token": summary["token"]})
    assert anyway.status_code == 200, anyway.text
    [audit] = _review_audits(db, approved_job.job_id)
    assert audit.detail["mode"] == "prompt" and audit.detail["shown"] is True
    assert audit.detail["published_anyway"] is True
    assert audit.detail["lines_unheard"] == 1 and audit.detail["warnings"] == summary["warnings_count"]
    assert audit.detail["token"] == summary["token"]


def test_prompt_does_not_ask_when_everything_was_heard_and_reviewed(client, admin_token, approved_job, db,
                                                                   all_r2_files_present, events_cleanup, monkeypatch):
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "prompt")
    events_cleanup.append(approved_job.job_id)
    _document(db, approved_job)
    _played(db, approved_job, [[0, 30000]])
    response = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    [audit] = _review_audits(db, approved_job.job_id)
    assert audit.detail["shown"] is False and audit.detail["lines_unheard"] == 0


def test_a_summary_failure_never_blocks_publication(client, admin_token, approved_job, db, all_r2_files_present,
                                                   events_cleanup, monkeypatch):
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "prompt")
    events_cleanup.append(approved_job.job_id)

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(pr, "build_for_jobs", broken)
    response = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    audit = db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION).order_by(AuditLog.id.desc()).first()
    assert audit.detail["error"] == "unavailable" and audit.detail["source"] == "admin_publish"


def test_off_skips_the_summary(client, admin_token, approved_job, db, all_r2_files_present, monkeypatch):
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "off")
    before = db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION).count()
    response = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    assert db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION).count() == before


def test_confirm_publication_asks_the_same_summary(client, admin_token, manual_request, db, monkeypatch, events_cleanup):
    job, document, cr, _delivery = _published_without_ticking(client, admin_token, manual_request, db)
    events_cleanup.append(job.job_id)
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "prompt")
    document.current_segments = LYRICS
    db.commit()
    _played(db, job, [[9000, 16500]])
    assert pr.build_for_jobs(db, [job])[job.job_id]["lines_unheard"] == 1
    body = {"reviewed_render_fingerprint": render_fingerprint(job), "reviewed_editor_revision": document.revision}
    first = client.post(f"/admin/change-requests/{cr.id}/confirm-publication", headers=auth(admin_token), json=body)
    assert first.status_code == 409, first.text
    [shown] = first.json()["detail"]["summaries"]
    confirmed = client.post(f"/admin/change-requests/{cr.id}/confirm-publication", headers=auth(admin_token),
                            json={**body, "publish_review_token": shown["token"]})
    assert confirmed.status_code == 200, confirmed.text
    [audit] = [row for row in _review_audits(db, job.job_id) if row.detail["source"] == "confirm_publication"]
    assert audit.detail["published_anyway"] is True
    assert audit.detail["change_request_id"] == cr.id


# --------------------------------------------------------------------------
# Envío por campaña: una sola confirmación con el total por canción
# --------------------------------------------------------------------------

def _campaign_song(client, admin_token, campaign, cleanup_jobs, segments=LYRICS):
    job_id = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    cleanup_jobs.append(job_id)
    with SessionLocal() as db:
        db.add(EditorDocument(job_id=job_id, tenant_id=campaign.tenant_id, current_segments=segments,
                              original_segments=segments, revision=2))
        db.commit()
    return job_id


def _send(client, admin_token, campaign, job_ids, key, **extra):
    return client.post(f"/batch/campaigns/{campaign.id}/deliveries", headers=auth(admin_token),
                       json={"job_ids": job_ids, "destination_portal": "chile", "idempotency_key": key, **extra})


def test_campaign_send_asks_once_for_the_whole_selection(client, admin_token, storage_ready, events_cleanup, monkeypatch):
    monkeypatch.setenv("PUBLISH_REVIEW_SUMMARY", "prompt")
    campaign = _campaign_for(client, admin_token, "publish-review-bulk")
    unheard = _campaign_song(client, admin_token, campaign, events_cleanup)
    heard = _campaign_song(client, admin_token, campaign, events_cleanup)
    with SessionLocal() as db:
        _played(db, db.query(Job).filter_by(job_id=heard).one(), [[0, 30000]])
        _played(db, db.query(Job).filter_by(job_id=unheard).one(), [[100000, 101000]])

    preview = client.get(f"/batch/campaigns/{campaign.id}/deliveries/publish-review?job_ids={unheard},{heard}",
                         headers=auth(admin_token))
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["mode"] == "prompt" and [s["job_id"] for s in body["summaries"]] == [unheard]
    assert body["totals"]["songs"] == 2 and body["totals"]["songs_needing_attention"] == 1
    assert body["totals"]["lines_unheard"] == 3

    refused = _send(client, admin_token, campaign, [unheard, heard], f"publish-review-{uuid.uuid4().hex}")
    assert refused.status_code == 409, refused.text
    assert [s["job_id"] for s in refused.json()["detail"]["summaries"]] == [unheard]
    with SessionLocal() as db:
        assert db.query(DeliveryBatchItem).filter(DeliveryBatchItem.job_id.in_([unheard, heard])).count() == 0

    tokens = {s["job_id"]: s["token"] for s in body["summaries"]}
    sent = _send(client, admin_token, campaign, [unheard, heard], f"publish-review-{uuid.uuid4().hex}",
                 publish_review_tokens=tokens)
    assert sent.status_code == 202, sent.text
    operation = sent.json()["operation_id"]
    with SessionLocal() as db:
        rows = {row.detail["job_id"]: row.detail for row in db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION)
                if (row.detail or {}).get("delivery_batch_id") == operation}
    assert set(rows) == {unheard, heard}
    assert rows[unheard]["published_anyway"] is True and rows[unheard]["lines_unheard"] == 3
    assert rows[heard]["shown"] is False and rows[heard]["lines_unheard"] == 0
    assert rows[unheard]["source"] == "campaign_bulk" and rows[unheard]["portal_id"] == "chile"


def test_campaign_preview_is_empty_outside_prompt(client, admin_token, storage_ready, events_cleanup, monkeypatch):
    monkeypatch.delenv("PUBLISH_REVIEW_SUMMARY", raising=False)
    campaign = _campaign_for(client, admin_token, "publish-review-observe")
    job_id = _campaign_song(client, admin_token, campaign, events_cleanup)
    preview = client.get(f"/batch/campaigns/{campaign.id}/deliveries/publish-review?job_ids={job_id}",
                         headers=auth(admin_token))
    assert preview.json() == {"mode": "observe", "available": True, "summaries": [], "totals": pr.totals([])}
    sent = _send(client, admin_token, campaign, [job_id], f"publish-review-{uuid.uuid4().hex}")
    assert sent.status_code == 202, sent.text
    with SessionLocal() as db:
        [audit] = [row for row in db.query(AuditLog).filter_by(action=pr.AUDIT_ACTION)
                   if (row.detail or {}).get("job_id") == job_id]
    assert audit.detail["mode"] == "observe" and audit.detail["shown"] is False
