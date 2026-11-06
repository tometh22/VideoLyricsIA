"""Pedidos de cambio en prosa libre (UMG #112-#131, 29-09-2026) convertidos
en cambios que el operador aprueba."""
import json

import pytest

import change_request_interpreter as cri
from change_request_proposals import apply_operations, build_proposal
from database import ChangeRequestProposal, DeliveryChangeRequest, EditorDocument
from tests.conftest import auth
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds  # noqa: F401


def _seg(text, start, end, sid):
    return {"text": text, "start": start, "end": end, "segment_id": sid}


SONG = [
    _seg("Hace un año atrás y siempre tú", 57.0, 60.0, "a"),
    _seg("Antes que llegué a suceder lo que sospecho", 27.0, 30.0, "b"),
    _seg("Tu garantía de reloco se fundió", 43.5, 47.8, "c"),
    _seg("Debo cuidarme de caer en el brasero", 60.5, 61.8, "d"),
    _seg("Te lo pido por favor", 66.0, 68.0, "e"),
]
SONG.sort(key=lambda s: s["start"])


def _answer(*changes):
    return lambda _prompt: json.dumps({"changes": list(changes)})


def _line(text):
    return next(k for k, s in enumerate(SONG, start=1) if s["text"] == text)


def test_free_prose_becomes_a_concrete_change():
    comment = '0:58 La frase es "QUE hace un año atrás..." pero falta el "QUE" del principio.'
    k = _line("Hace un año atrás y siempre tú")
    result = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k,
         "new_lines": ["QUE hace un año atrás y siempre tú"], "why": "Falta el Que"}))
    [proposal] = result["proposals"]
    # La mayúscula del cliente marca la palabra; en la letra va normal.
    assert proposal["after"] == ["Que hace un año atrás y siempre tú"]
    assert result["manual"] == []


def test_the_model_cannot_invent_lyrics_or_drop_sung_words():
    comment = "0:28 quitar tilde a LLEGUE"
    k = _line("Antes que llegué a suceder lo que sospecho")
    invented = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k,
         "new_lines": ["Antes que llegue a pasar lo que sospecho"]}))
    assert invented["proposals"] == [] and invented["manual"][0]["reason"] == "invented_words"
    dropped = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k,
         "new_lines": ["Antes que llegue a suceder"]}))
    assert dropped["proposals"] == [] and dropped["manual"][0]["reason"] == "drops_sung_words"
    ok = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k,
         "new_lines": ["Antes que llegue a suceder lo que sospecho"]}))
    assert ok["proposals"][0]["after"] == ["Antes que llegue a suceder lo que sospecho"]


def test_a_change_far_from_the_cited_time_is_not_offered():
    comment = '0:10 la línea es "Te lo pido por favor, amor"'
    k = _line("Te lo pido por favor")
    result = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k, "new_lines": ["Te lo pido por favor, amor"]}))
    assert result["proposals"] == [] and result["manual"][0]["reason"] == "not_located"


def test_missing_phrase_goes_where_it_is_heard_and_never_over_the_next_line():
    comment = "Falta la frase: “dormite ya”, comienza en 0:48"
    k = _line("Tu garantía de reloco se fundió")
    heard = [{"word": w, "start": s, "end": s + 0.4} for w, s in (("dormite", 48.0), ("ya", 48.5))]
    evidence = {"hypotheses_by_family": [{"role": "independent", "kind": "word_stream", "family": "openai/whisper-1",
                                          "transformation": "live_independent_verify_raw", "events": heard}]}
    result = cri.interpret(comment, SONG, ask=_answer(
        {"type": "rewrite", "quote": comment, "from": k, "to": k,
         "new_lines": ["Tu garantía de reloco se fundió", "dormite ya"]}))
    after = cri.apply(SONG, result["proposals"], evidence)
    new = next(s for s in after if s["text"] == "dormite ya")
    following = next(s for s in after if s["start"] > new["start"])
    assert new["end"] <= following["start"]


def test_timing_comes_from_the_audio_not_from_the_model():
    comment = "1:00 Alargar el tiempo de esa línea cuando dice brasero"
    k = _line("Debo cuidarme de caer en el brasero")
    heard = [{"word": "brasero", "start": 62.2, "end": 63.4}]
    evidence = {"hypotheses_by_family": [{"role": "independent", "kind": "word_stream", "family": "openai/whisper-1",
                                          "transformation": "live_independent_verify_raw", "events": heard}]}
    result = cri.interpret(comment, SONG, ask=_answer(
        {"type": "timing", "quote": comment, "line": k, "edge": "end", "direction": "later"}))
    moved = cri.apply(SONG, result["proposals"], evidence)[k - 1]
    assert 63.4 <= moved["end"] <= 66.0


def test_a_cut_answer_keeps_its_complete_changes():
    raw = '{"changes": [{"type": "other", "quote": "fondo", "category": "visual"}, {"type": "rewrite", "quote": "x", "fr'
    assert [c["type"] for c in cri.parse_response(raw)] == ["other"]


def test_interpreted_changes_join_the_proposal_and_apply():
    comment = ('0:58 La frase es "QUE hace un año atrás..." pero falta el "QUE" del principio.\n'
               '1:00 Alargar el tiempo de esa línea cuando dice brasero')
    a, d = _line("Hace un año atrás y siempre tú"), _line("Debo cuidarme de caer en el brasero")
    heard = [{"word": "brasero", "start": 62.2, "end": 63.4}]
    evidence = {"hypotheses_by_family": [{"role": "independent", "kind": "word_stream", "family": "openai/whisper-1",
                                          "transformation": "live_independent_verify_raw", "events": heard}]}
    interp = cri.interpretation_for(comment, SONG, evidence=evidence, ask=_answer(
        {"type": "rewrite", "quote": comment.splitlines()[0], "from": a, "to": a,
         "new_lines": ["Que hace un año atrás y siempre tú"]},
        {"type": "timing", "quote": comment.splitlines()[1], "line": d, "edge": "end", "direction": "later"}))
    strict = build_proposal(comment=comment, segments=SONG, base_revision=3)
    assert strict["applicable_count"] == 0
    proposal = build_proposal(comment=comment, segments=SONG, base_revision=3, interpretation=interp)
    assert proposal["applicable_count"] == 2 and proposal["unresolved_count"] == 0
    assert proposal["parser_version"].endswith("+" + cri.SCHEMA)
    ids = [op["id"] for op in proposal["operations"] if op["applicable"]]
    result, _ = apply_operations(SONG, proposal, ids)
    texts = {s["text"] for s in result}
    assert "Que hace un año atrás y siempre tú" in texts
    assert next(s for s in result if "brasero" in s["text"])["end"] > 61.8


@pytest.fixture
def interpreter_case(client, admin_token, approved_job, db, all_r2_files_present, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHANGE_REQUEST_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CHANGE_REQUEST_INTERPRETER_ENABLED", "1")
    monkeypatch.setattr("main._dispatch_editor_quality_outbox", lambda *_: None)
    approved_job.segments_json = [{"start": 0, "end": 3, "text": "Hace un año atrás"}]
    approved_job.segments_revision = 0
    db.flush()
    from tests.test_deliveries import _signed_umg_qc_report
    approved_job.delivery_qc = _signed_umg_qc_report(approved_job, approved_job.approved_by)
    db.commit()
    response = client.post("/admin/deliveries/from-job/" + approved_job.job_id, headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    request = DeliveryChangeRequest(delivery_id=response.json()["delivery_id"],
                                    comment='0:01 falta el "QUE" del principio')
    db.add(request)
    db.commit()
    return request.id


def test_the_endpoint_answers_at_once_and_completes_in_the_background(client, admin_token, interpreter_case, db,
                                                                       monkeypatch):
    calls = []

    def fake_model(prompt, **_):
        calls.append(prompt)
        return json.dumps({"changes": [{"type": "rewrite", "quote": '0:01 falta el "QUE" del principio',
                                        "from": 1, "to": 1, "new_lines": ["Que hace un año atrás"]}]})

    monkeypatch.setattr(cri, "ask_model", fake_model)
    base = f"/admin/change-requests/{interpreter_case}/proposals"
    first = client.post(base, headers=auth(admin_token))
    assert first.status_code == 200, first.text
    assert first.json()["proposal"]["status"] == "interpreting"
    # TestClient corre la tarea de fondo al terminar la respuesta.
    current = client.get(base + "/current", headers=auth(admin_token)).json()["proposal"]
    assert len(calls) == 1
    db.expire_all()
    row = db.query(ChangeRequestProposal).filter_by(change_request_id=interpreter_case).one()
    assert current["status"] == "ready", (row.decision_history, row.base_revision)
    [op] = [op for op in current["operations"] if op["applicable"]]
    assert op["origin"] == "interpreter" and op["proposed_segments"][0]["text"] == "Que hace un año atrás"
    # Pedir de nuevo no vuelve a llamar al modelo.
    again = client.post(base, headers=auth(admin_token)).json()["proposal"]
    assert again["id"] == current["id"] and len(calls) == 1


def test_a_model_failure_leaves_the_request_as_it_was(client, admin_token, interpreter_case, db, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("quota")

    monkeypatch.setattr(cri, "ask_model", broken)
    base = f"/admin/change-requests/{interpreter_case}/proposals"
    client.post(base, headers=auth(admin_token))
    current = client.get(base + "/current", headers=auth(admin_token)).json()["proposal"]
    assert current["status"] == "needs_input"
    db.expire_all()
    row = db.query(ChangeRequestProposal).filter_by(change_request_id=interpreter_case).one()
    assert row.decision_history[-1]["decision"] == "interpretation_failed"
    doc = db.query(EditorDocument).filter_by(job_id=row.job_id).one()
    assert doc.current_segments[0]["text"] == "Hace un año atrás"
