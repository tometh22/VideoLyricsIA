"""Local synthetic audit regressions; no live services or paid calls."""
import math
import pytest
import ass_render


def test_word_timing_survives_valid_line_move_without_out_of_window_words():
    row = {"segment_id": "moved", "text": "hola mundo", "start": 0, "end": 1,
           "words": [{"word": "hola", "start": 5, "end": 6},
                     {"word": "mundo", "start": 6, "end": 7}]}
    line = ass_render.segments_to_lines([row], text_scale=1, animation="word_reveal")[0]
    assert all(math.isfinite(w["start"]) and math.isfinite(w["end"])
               and row["start"] <= w["start"] < w["end"] <= row["end"] for w in line.words), line.words


@pytest.mark.parametrize("bad_start", [None, "missing-time"])
def test_persistable_bad_word_metadata_does_not_disable_preview(client, monkeypatch, bad_start):
    from database import SessionLocal, Job, EditorDocument
    from editor import save_document
    from tests.test_editor_documents import _users_and_job, _token_for
    from main import app
    from starlette.testclient import TestClient
    monkeypatch.setenv("EDITOR_V2_GLOBALLY_ENABLED", "1")
    user, _, job_id = _users_and_job("synthetic_final_preview_audit")
    row = {"segment_id": "invalid-word-metadata", "text": "hola", "start": 0, "end": 1,
           "words": [{"word": "hola", "start": bad_start, "end": 1}]}
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        save_document(db, job, document, user.id, 0, [row], "manual")
        db.commit()
    token = _token_for(user)
    with TestClient(app, raise_server_exceptions=False) as api:
        response = api.post(f"/editor/{job_id}/text-preview",
            headers={"Authorization": f"Bearer {token}"},
            json={"segments": [row], "settings": {"lyrics_animation": "karaoke"}})
    assert response.status_code == 200, (response.status_code, response.text)


def test_valid_large_editor_document_is_supported_by_preview_contract():
    from editor import normalize_segments
    from editor_preview import TextPreviewRequest
    rows = [{"segment_id": str(i), "text": f"Frase sintética {i}", "start": i * 2, "end": i * 2 + 1} for i in range(501)]
    saved = normalize_segments(rows)
    assert len(saved) == 501
    preview = TextPreviewRequest(segments=saved, settings={"font": "montserrat-bold"})
    assert len(preview.segments) == 501


def test_legacy_delete_last_line_survives_reload_and_rejects_a_stale_restore(client, monkeypatch):
    import main
    from tests.test_editor_documents import _users_and_job, _token_for
    monkeypatch.setenv("EDITOR_V2_GLOBALLY_ENABLED", "1")
    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _event_id: None)
    user, _, job_id = _users_and_job("synthetic_delete_last")
    headers = {"Authorization": f"Bearer {_token_for(user)}"}
    original = client.get(f"/editor/{job_id}", headers=headers).json()
    path = f"/jobs/{job_id}/save-segments"
    saved = client.post(path, headers=headers, json={"segments": [], "base_revision": original["revision"]})
    assert saved.status_code == 200, saved.text
    reloaded = client.get(f"/editor/{job_id}", headers=headers).json()
    assert reloaded["segments"] == []
    assert reloaded["revision"] > original["revision"]
    stale = client.post(path, headers=headers, json={"segments": original["segments"], "base_revision": original["revision"]})
    assert stale.status_code == 409
    assert client.get(f"/editor/{job_id}", headers=headers).json()["segments"] == []


def test_editor_save_reorders_a_rebased_insertion_without_retiming_it(client, monkeypatch):
    import main
    from tests.test_editor_documents import _users_and_job, _token_for
    monkeypatch.setenv("EDITOR_V2_GLOBALLY_ENABLED", "1")
    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _event_id: None)
    user, _, job_id = _users_and_job("synthetic_rebase_order")
    headers = {"Authorization": f"Bearer {_token_for(user)}"}
    rows = [{"segment_id": "a", "start": 1, "end": 2, "text": "Primera"},
            {"segment_id": "b", "start": 3, "end": 4, "text": "Segunda"},
            {"segment_id": "new", "start": 0, "end": 3.5, "text": "Texto recuperado", "review": True}]
    response = client.patch(f"/editor/{job_id}", headers=headers, json={"base_revision": 0, "segments": rows})
    assert response.status_code == 200, response.text
    reloaded = client.get(f"/editor/{job_id}", headers=headers).json()
    assert reloaded["segments"] == [rows[2], rows[0], rows[1]]
