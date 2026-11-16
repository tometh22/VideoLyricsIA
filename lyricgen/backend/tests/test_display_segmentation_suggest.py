"""DISPLAY_SEGMENTATION_MODE=suggest: proposals reach the reviewer, never the document."""
from copy import deepcopy
import uuid

import pytest

import display_segmentation as ds
from tests.test_display_segmentation import SOURCE, TEXT, line


def _report_without_mode(report):
    return {k: v for k, v in report.items() if k not in {"mode", "status"}}


def test_suggest_is_a_valid_mode_and_never_applies_even_when_authorized(monkeypatch):
    rows = [line()]
    before = deepcopy(rows)
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "suggest")
    assert ds.mode() == "suggest"
    output, report, proposal = ds.run(rows, source=SOURCE, fresh=True,
                                      authorization={"authorized": True})
    assert output == rows == before
    assert report["mode"] == "suggest" and report["status"] == "suggest"
    assert report["proposed_count"] == 1 and report["applied_count"] == 0
    assert proposal["changes"]


def test_shadow_report_is_unchanged_by_the_new_mode(monkeypatch):
    rows = [line()]
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "shadow")
    shadow_out, shadow, shadow_proposal = ds.run(rows, source=SOURCE, fresh=True, authorization={})
    assert shadow_out == rows and shadow["status"] == "shadow" and shadow["mode"] == "shadow"
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "suggest")
    _out, suggest, suggest_proposal = ds.run(rows, source=SOURCE, fresh=True, authorization={})
    assert _report_without_mode(shadow) == _report_without_mode(suggest)
    assert shadow_proposal == suggest_proposal
    # Unknown values still fail closed.
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "suggestion")
    assert ds.mode() == "off"


def test_split_suggestion_carries_exact_text_cuts_and_parts():
    rows = [line()]
    items = ds.suggestions(rows)
    assert [item["kind"] for item in items] == ["split"]
    item = items[0]
    assert item["segment_id"] == "parent" and item["text"] == TEXT and item["index"] == 0
    tokens = TEXT.split()
    assert [p["text"] for p in item["parts"]] == [
        " ".join(tokens[a:b]) for a, b in (p["token_range"] for p in item["parts"])]
    assert " ".join(p["text"] for p in item["parts"]).split() == tokens
    assert item["cuts"] == [p["token_range"][0] for p in item["parts"][1:]]
    assert len(item["id"]) == 16 and ds.suggestions(rows)[0]["id"] == item["id"]
    # A line inserted above shifts the index, not the identity (dismissals stick).
    shifted = ds.suggestions([{"segment_id": "intro", "start": 0.0, "end": 1.0, "text": "Hola"}, line()])
    assert shifted[0]["index"] == 1 and shifted[0]["id"] == item["id"]
    assert rows == [line()]  # read-only


def paused_line(text, *pauses, identity="p"):
    """Word clocks with a sung pause before each token index in `pauses`."""
    words, cursor = [], 1.0
    for k, token in enumerate(text.split()):
        if k in pauses:
            cursor += 0.6
        words.append({"word": token, "start": cursor, "end": cursor + 0.3})
        cursor += 0.35
    return {"segment_id": identity, "text": text, "start": 1.0, "end": cursor, "words": words}


@pytest.mark.parametrize("text,pause_at", [
    # Sued Silva L7 regression, synthetic: verb | preposition + complement.
    ("Con los ojos de la fe pasa a mirar quien todavía tiene vida acá", 7),
    # verb phrase | lowercase infinitive complement.
    ("Esta noche yo quiero de verdad bailar contigo hasta que salga el sol", 6),
    # Left side ends in a word that cannot close a phrase (not in _DANGLING).
    ("Sinto mais forte e mais feliz quem sabe que muito pouco eu sei", 7),
    # Right side opens with a lowercase article that continues the phrase.
    ("Caminamos bajo el cielo gris mirando la luna que se va", 6),
])
def test_split_suggestion_never_strands_a_phrase(text, pause_at):
    row = paused_line(text, pause_at)
    # The validated shadow/enforce planner is unchanged: it still proposes
    # the pause, which is why the guard lives only in reviewer suggestions.
    shadow_cuts = [b["token"] for c in ds.plan([row], source=SOURCE)["changes"] for b in c["boundaries"]]
    assert shadow_cuts == [pause_at]
    assert all(pause_at not in item.get("cuts", []) for item in ds.suggestions([row]))


def test_split_suggestion_guard_respects_closing_punctuation():
    # "…gaveta," closes the phrase: a lowercase preposition may open the next card.
    row = paused_line("Pela glock na gaveta, pelo gambé pulando a janela", 4)
    items = ds.suggestions([row])
    assert [(i["kind"], i["cuts"]) for i in items] == [("split", [4])]
    assert [p["text"] for p in items[0]["parts"]] == ["Pela glock na gaveta,", "pelo gambé pulando a janela"]
    # Sued Silva L7 shape: pauses after "fé," and after "passe". The
    # suggestion cuts at the comma, never between "passe" and "a enxergar".
    row = paused_line("Com os olhos da fé, passe a enxergar quem ainda tem vida aí", 5, 6)
    items = ds.suggestions([row])
    assert [p["text"] for p in items[0]["parts"]] == [
        "Com os olhos da fé,", "passe a enxergar quem ainda tem vida aí"]


def test_merge_suggestion_only_for_open_ended_line_that_fits_one_card():
    rows = [
        {"segment_id": "a", "start": 0.0, "end": 1.6, "text": "Sinto mais forte, mais feliz quem"},
        {"segment_id": "b", "start": 1.7, "end": 2.4, "text": "Sabe"},
        {"segment_id": "c", "start": 3.0, "end": 4.0, "text": "En"},
        {"segment_id": "d", "start": 4.1, "end": 6.0, "text": "la noche que te vi"},
        {"segment_id": "e", "start": 7.0, "end": 8.0, "text": "Ay"},
        {"segment_id": "f", "start": 8.1, "end": 9.0, "text": "por la,"},  # punctuation closes
        {"segment_id": "g", "start": 9.1, "end": 10.0, "text": "calle"},
    ]
    items = ds.suggestions(rows)
    assert [(i["kind"], i["segment_id"], i["next_segment_id"]) for i in items] == [
        ("merge", "a", "b"), ("merge", "c", "d")]
    assert items[0]["merged_text"] == "Sinto mais forte, mais feliz quem Sabe"


def test_merge_is_not_suggested_across_a_long_gap_a_lock_or_an_overflow():
    far = [{"segment_id": "a", "start": 0, "end": 1, "text": "por la"},
           {"segment_id": "b", "start": 3, "end": 4, "text": "calle"}]
    locked = [{"segment_id": "a", "start": 0, "end": 1, "text": "por la", "locked": True},
              {"segment_id": "b", "start": 1.1, "end": 2, "text": "calle"}]
    long = [{"segment_id": "a", "start": 0, "end": 1, "text": "y caminamos juntos por la"},
            {"segment_id": "b", "start": 1.1, "end": 2, "text": "calle de la ciudad"}]
    assert ds.suggestions(far) == ds.suggestions(locked) == ds.suggestions(long) == []


def test_protected_quality_line_gets_no_split_suggestion():
    assert ds.suggestions([line()], protected_indices={0}) == []
    assert ds.suggestions("not a list") == [] and ds.suggestions([]) == []


def _job_with_document(rows):
    from auth import create_user, start_login_session
    from database import Job, SessionLocal
    from editor import ensure_document
    db = SessionLocal()
    try:
        tenant = "suggest_team"
        user = create_user(db, f"suggest_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        job_id = f"sg_{uuid.uuid4().hex[:9]}"
        db.add(Job(job_id=job_id, user_id=user.id, tenant_id=tenant, artist="A", song_title="S",
                   filename="t.wav", style="oscuro", status="transcribed_pending",
                   current_step="editing", delivery_profile="youtube"))
        db.commit()
        ensure_document(db, job_id, tenant, rows)
        db.commit()
        return job_id, start_login_session(db, user)
    finally:
        db.close()


def test_editor_document_exposes_suggestions_only_in_suggest_mode(client, monkeypatch):
    from tests.conftest import auth
    rows = [line(start=0.0), {"segment_id": "x", "start": 20.0, "end": 21.0, "text": "por la"},
            {"segment_id": "y", "start": 21.1, "end": 22.0, "text": "calle"}]
    job_id, token = _job_with_document(rows)

    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "shadow")
    shadow = client.get(f"/editor/{job_id}", headers=auth(token))
    assert shadow.status_code == 200, shadow.text
    assert "display_segmentation_suggestions" not in shadow.json()
    assert "display_segmentation_mode" not in shadow.json()

    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "suggest")
    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["display_segmentation_mode"] == "suggest"
    payload = body["display_segmentation_suggestions"]
    assert payload["schema"] == ds.SUGGESTION_SCHEMA and payload["revision"] == body["revision"]
    assert [(i["kind"], i["segment_id"]) for i in payload["items"]] == [("split", "parent"), ("merge", "x")]
    # Nothing was applied: the document still has the machine lines.
    assert [s["text"] for s in body["segments"]] == [r["text"] for r in rows]
    assert body["segments"][0]["words"] == rows[0]["words"]

    # Suggestions follow the CURRENT lines: once the reviewer splits by hand,
    # the stale suggestion for that line disappears.
    words = rows[0]["words"]
    saved = client.patch(f"/editor/{job_id}", headers=auth(token), json={
        "base_revision": body["revision"], "checkpoint": "manual",
        "segments": [
            {"segment_id": "p1", "start": rows[0]["start"], "end": words[5]["end"],
             "text": " ".join(TEXT.split()[:6]), "words": words[:6]},
            {"segment_id": "p2", "start": words[6]["start"], "end": rows[0]["end"],
             "text": " ".join(TEXT.split()[6:]), "words": words[6:]},
            *rows[1:],
        ],
    })
    assert saved.status_code == 200, saved.text
    reloaded = client.get(f"/editor/{job_id}", headers=auth(token)).json()
    assert reloaded["revision"] == saved.json()["revision"]
    items = reloaded["display_segmentation_suggestions"]["items"]
    assert items and all(item["segment_id"] != "parent" for item in items)
    assert reloaded["display_segmentation_suggestions"]["revision"] == reloaded["revision"]
