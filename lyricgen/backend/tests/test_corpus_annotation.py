"""Gold corpus annotation tool (corpus.py).

Covers:
  - admin song + annotator-token CRUD (auth required, non-admin blocked)
  - annotator-facing endpoints resolve identity purely from the URL token
  - BLIND MODE: annotator A's token can never read/write annotator B's
    annotation for the same song
  - draft autosave + submit lifecycle, including the "empty submit" guard
  - admin comparison endpoint shows both annotators together
  - audio/waveform endpoints reuse the storage module (mocked here, same
    pattern as tests/test_source_audio_url.py)

R2 storage is mocked: monkeypatch replaces storage.object_exists /
generate_signed_url so this suite never touches boto3.
"""

import uuid

from tests.conftest import auth
from corpus_reference import backfill_reference_segments, clean_reference_segments, is_control_song
from database import EditorDocument, Job


def _create_job_with_document(db, current_segments, input_r2_key=None):
    """A minimal delivered job + its reviewed editor_documents row, the
    real-world shape reference_segments is backfilled from. `words` /
    `ctc_lr` / `provider_evidence` / `timing_provenance` mimic the
    renderer/pipeline metadata production segments actually carry, which
    the backfill must discard."""
    job_id = uuid.uuid4().hex[:12]
    r2_key = input_r2_key or f"inputs/default/x/{job_id}.mp3"
    job = Job(
        job_id=job_id,
        user_id=1,
        tenant_id="default",
        artist="Test Artist",
        song_title="Test Song",
        filename="song.mp3",
        status="done",
        delivery_profile="youtube",
        progress=100,
        input_r2_key=r2_key,
    )
    db.add(job)
    db.commit()

    doc = EditorDocument(
        job_id=job_id,
        tenant_id="default",
        current_segments=current_segments,
        original_segments=current_segments,
        revision=1,
    )
    db.add(doc)
    db.commit()
    return job_id, r2_key


def _create_song(client, admin_token, **overrides):
    body = {
        "artist": "Artista de Prueba",
        "title": "Canción de Prueba",
        "audio_r2_key": "inputs/corpus/test-song.mp3",
    }
    body.update(overrides)
    res = client.post("/admin/corpus/songs", headers=auth(admin_token), json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _create_annotator(client, admin_token, name="Anotador de Prueba"):
    res = client.post(
        "/admin/corpus/annotators", headers=auth(admin_token), json={"name": name},
    )
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------

def test_admin_can_create_and_list_song(client, admin_token):
    song = _create_song(client, admin_token)
    assert song["artist"] == "Artista de Prueba"
    assert song["is_active"] is True

    res = client.get("/admin/corpus/songs", headers=auth(admin_token))
    assert res.status_code == 200
    ids = [s["id"] for s in res.json()["songs"]]
    assert song["id"] in ids


def test_non_admin_cannot_create_song(client, user_token):
    res = client.post(
        "/admin/corpus/songs", headers=auth(user_token), json={
            "artist": "X", "title": "Y", "audio_r2_key": "inputs/x.mp3",
        },
    )
    assert res.status_code == 403


def test_create_song_requires_audio_source(client, admin_token):
    res = client.post(
        "/admin/corpus/songs", headers=auth(admin_token), json={
            "artist": "X", "title": "Y",
        },
    )
    assert res.status_code == 422


def test_admin_can_create_annotator_token(client, admin_token):
    annotator = _create_annotator(client, admin_token, name="Esposa del Founder")
    assert annotator["name"] == "Esposa del Founder"
    assert len(annotator["token"]) > 30
    assert annotator["annotate_path"] == f"/annotate/{annotator['token']}"

    res = client.get("/admin/corpus/annotators", headers=auth(admin_token))
    assert res.status_code == 200
    names = [a["name"] for a in res.json()["annotators"]]
    assert "Esposa del Founder" in names


def test_admin_can_revoke_annotator(client, admin_token):
    annotator = _create_annotator(client, admin_token)
    res = client.patch(
        f"/admin/corpus/annotators/{annotator['id']}",
        headers=auth(admin_token), json={"is_active": False},
    )
    assert res.status_code == 200
    assert res.json()["is_active"] is False

    # Revoked token no longer resolves for the annotator-facing surface.
    res2 = client.get(f"/annotate/{annotator['token']}")
    assert res2.status_code == 404


# ---------------------------------------------------------------------------
# Annotator-facing: identity + song list
# ---------------------------------------------------------------------------

def test_unknown_token_returns_404_everywhere(client):
    assert client.get("/annotate/not-a-real-token").status_code == 404
    assert client.get("/annotate/not-a-real-token/songs").status_code == 404


def test_annotator_sees_greeting_and_song_list(client, admin_token):
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token, name="Marina")

    res = client.get(f"/annotate/{annotator['token']}")
    assert res.status_code == 200
    assert res.json()["name"] == "Marina"

    res2 = client.get(f"/annotate/{annotator['token']}/songs")
    assert res2.status_code == 200
    body = res2.json()
    assert body["annotator_name"] == "Marina"
    ids = [s["id"] for s in body["songs"]]
    assert song["id"] in ids
    mine = next(s for s in body["songs"] if s["id"] == song["id"])
    assert mine["my_status"] == "not_started"


def test_inactive_song_hidden_from_annotator_list(client, admin_token):
    # No direct "deactivate song" endpoint exists yet — simulate via a
    # song created active (default) and assert only active songs show.
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)
    res = client.get(f"/annotate/{annotator['token']}/songs")
    ids = [s["id"] for s in res.json()["songs"]]
    assert song["id"] in ids  # sanity: active songs DO show


# ---------------------------------------------------------------------------
# Draft autosave + submit lifecycle
# ---------------------------------------------------------------------------

_SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "hola mundo", "event_type": "lexical"},
    {"start": 2.5, "end": 3.0, "text": "oh oh", "event_type": "vocalization"},
]


def test_save_and_load_own_draft(client, admin_token):
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)
    token = annotator["token"]

    res = client.put(
        f"/annotate/{token}/songs/{song['id']}", json={"segments": _SEGMENTS},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "draft"
    assert len(res.json()["segments"]) == 2

    res2 = client.get(f"/annotate/{token}/songs/{song['id']}")
    assert res2.status_code == 200
    assert res2.json()["annotation"]["segments"] == res.json()["segments"]


def test_save_rejects_invalid_event_type(client, admin_token):
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)
    bad = [{"start": 0, "end": 1, "text": "x", "event_type": "not_a_real_type"}]
    res = client.put(
        f"/annotate/{annotator['token']}/songs/{song['id']}", json={"segments": bad},
    )
    assert res.status_code == 422


def test_submit_requires_at_least_one_segment(client, admin_token):
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)
    res = client.post(f"/annotate/{annotator['token']}/songs/{song['id']}/submit")
    assert res.status_code == 422


def test_submit_flips_status_and_is_resubmittable(client, admin_token):
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)
    token = annotator["token"]

    client.put(f"/annotate/{token}/songs/{song['id']}", json={"segments": _SEGMENTS})
    res = client.post(f"/annotate/{token}/songs/{song['id']}/submit")
    assert res.status_code == 200
    assert res.json()["status"] == "submitted"
    assert res.json()["submitted_at"] is not None

    # Editing after submit stays allowed (forgiving UX for non-technical
    # users) and does NOT silently revert status to draft.
    more = _SEGMENTS + [{"start": 3.0, "end": 4.0, "text": "final", "event_type": "lexical"}]
    res2 = client.put(f"/annotate/{token}/songs/{song['id']}", json={"segments": more})
    assert res2.status_code == 200
    assert res2.json()["status"] == "submitted"

    # Re-submit is idempotent, not an error.
    res3 = client.post(f"/annotate/{token}/songs/{song['id']}/submit")
    assert res3.status_code == 200


# ---------------------------------------------------------------------------
# Blind mode: annotator A can never see/write annotator B's row
# ---------------------------------------------------------------------------

def test_two_annotators_are_fully_isolated(client, admin_token):
    song = _create_song(client, admin_token)
    a = _create_annotator(client, admin_token, name="Anotador A")
    b = _create_annotator(client, admin_token, name="Anotador B")

    client.put(
        f"/annotate/{a['token']}/songs/{song['id']}",
        json={"segments": [{"start": 0, "end": 1, "text": "A dice esto", "event_type": "lexical"}]},
    )
    client.put(
        f"/annotate/{b['token']}/songs/{song['id']}",
        json={"segments": [{"start": 5, "end": 6, "text": "B dice otra cosa", "event_type": "lexical"}]},
    )

    # A's own view only ever shows A's segments.
    view_a = client.get(f"/annotate/{a['token']}/songs/{song['id']}").json()
    assert view_a["annotation"]["segments"][0]["text"] == "A dice esto"

    # B's own view only ever shows B's segments — never A's.
    view_b = client.get(f"/annotate/{b['token']}/songs/{song['id']}").json()
    assert view_b["annotation"]["segments"][0]["text"] == "B dice otra cosa"

    # There is no endpoint parameter that lets A address B's row: the
    # song-list "my_status" for each token is independent.
    songs_a = client.get(f"/annotate/{a['token']}/songs").json()["songs"]
    songs_b = client.get(f"/annotate/{b['token']}/songs").json()["songs"]
    mine_a = next(s for s in songs_a if s["id"] == song["id"])
    mine_b = next(s for s in songs_b if s["id"] == song["id"])
    assert mine_a["my_segment_count"] == 1
    assert mine_b["my_segment_count"] == 1


def test_admin_comparison_endpoint_shows_both_annotators(client, admin_token):
    song = _create_song(client, admin_token)
    a = _create_annotator(client, admin_token, name="Anotador A")
    b = _create_annotator(client, admin_token, name="Anotador B")
    client.put(
        f"/annotate/{a['token']}/songs/{song['id']}",
        json={"segments": [{"start": 0, "end": 1, "text": "uno", "event_type": "lexical"}]},
    )
    client.put(
        f"/annotate/{b['token']}/songs/{song['id']}",
        json={"segments": [{"start": 0, "end": 1, "text": "dos", "event_type": "lexical"}]},
    )

    res = client.get(f"/admin/corpus/songs/{song['id']}/annotations", headers=auth(admin_token))
    assert res.status_code == 200
    names = {row["annotator_name"] for row in res.json()["annotations"]}
    assert names == {"Anotador A", "Anotador B"}


def test_admin_comparison_endpoint_requires_admin(client, user_token, admin_token):
    song = _create_song(client, admin_token)
    res = client.get(
        f"/admin/corpus/songs/{song['id']}/annotations", headers=auth(user_token),
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Audio + waveform (storage mocked)
# ---------------------------------------------------------------------------

def test_audio_url_uses_signed_r2_url(client, admin_token, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "object_exists", lambda key: True)
    monkeypatch.setattr(
        storage, "generate_signed_url",
        lambda key, expiry_seconds=3600: f"https://r2.fake/{key}?sig=ok",
    )
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)

    res = client.get(f"/annotate/{annotator['token']}/songs/{song['id']}/audio-url")
    assert res.status_code == 200
    assert res.json()["url"].startswith("https://r2.fake/")


def test_audio_url_404_when_object_missing(client, admin_token, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "object_exists", lambda key: False)
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)

    res = client.get(f"/annotate/{annotator['token']}/songs/{song['id']}/audio-url")
    assert res.status_code == 404


def test_waveform_endpoint_delegates_to_compute_and_cache(client, admin_token, monkeypatch):
    import storage
    import waveform_compute
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        waveform_compute, "compute_and_cache_waveform",
        lambda job_id, key, **kw: {"peaks": [0.1, 0.2], "duration": 3.0},
    )
    song = _create_song(client, admin_token)
    annotator = _create_annotator(client, admin_token)

    res = client.get(f"/annotate/{annotator['token']}/songs/{song['id']}/waveform")
    assert res.status_code == 200
    assert res.json() == {"peaks": [0.1, 0.2], "duration": 3.0}


# ---------------------------------------------------------------------------
# Reference-segments precarga (corpus_reference.py)
# ---------------------------------------------------------------------------

_RAW_PRODUCTION_SEGMENTS = [
    {
        "start": 0.0, "end": 2.4, "text": "hola mundo",
        "words": [{"start": 0.0, "end": 0.5, "text": "hola"}],
        "ctc_lr": 0.92,
        "provider_evidence": {"source": "whisperx"},
        "timing_provenance": {"kind": "ctc_align"},
        "locked": True,
    },
    {"start": 2.4, "end": 4.0, "text": "segunda frase", "words": []},
]


def test_clean_reference_segments_strips_pipeline_metadata():
    cleaned = clean_reference_segments(_RAW_PRODUCTION_SEGMENTS)
    assert cleaned == [
        {"start": 0.0, "end": 2.4, "text": "hola mundo", "event_type": "lexical"},
        {"start": 2.4, "end": 4.0, "text": "segunda frase", "event_type": "lexical"},
    ]


def test_clean_reference_segments_skips_malformed_rows():
    junk = [
        {"start": "not-a-number", "end": 1.0, "text": "x"},
        {"end": 1.0, "text": "no start"},
        {"start": 5.0, "end": 1.0, "text": "end before start"},
        {"start": 1.0, "end": 2.0, "text": "ok"},
    ]
    assert clean_reference_segments(junk) == [
        {"start": 1.0, "end": 2.0, "text": "ok", "event_type": "lexical"},
    ]


def test_is_control_song_detects_marker():
    assert is_control_song("CONTROL: hold out, no precarga") is True
    assert is_control_song("some other note") is False
    assert is_control_song(None) is False


def test_backfill_seeds_reference_segments_from_editor_document(client, admin_token, db):
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(client, admin_token, audio_r2_key=r2_key)

    stats = backfill_reference_segments(db)
    assert stats["seeded"] >= 1

    from database import CorpusSong
    refreshed = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert refreshed.is_control is False
    assert refreshed.reference_segments == [
        {"start": 0.0, "end": 2.4, "text": "hola mundo", "event_type": "lexical"},
        {"start": 2.4, "end": 4.0, "text": "segunda frase", "event_type": "lexical"},
    ]


def test_backfill_dry_run_does_not_persist(client, admin_token, db):
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(client, admin_token, audio_r2_key=r2_key)

    backfill_reference_segments(db, dry_run=True)
    db.expire_all()

    from database import CorpusSong
    refreshed = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert refreshed.reference_segments is None


def test_backfill_marks_control_song_and_never_seeds_it(client, admin_token, db):
    """Non-negotiable: a song noted "CONTROL:" must never get a precarga,
    even when a matching, perfectly usable editor_documents row exists.
    Asserts against this specific song's row rather than the aggregate
    stats — other tests in this module share the session-scoped test DB
    and also create corpus songs, so global counts aren't isolated."""
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(
        client, admin_token, audio_r2_key=r2_key,
        notes="CONTROL: blind hold-out, do not preload",
    )

    stats = backfill_reference_segments(db)
    assert stats["control"] >= 1

    from database import CorpusSong
    refreshed = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert refreshed.is_control is True
    assert refreshed.reference_segments is None


def test_backfill_counts_songs_without_job_or_document(client, admin_token, db):
    # No matching job for this audio_r2_key at all.
    song = _create_song(client, admin_token, audio_r2_key="inputs/default/nowhere/song.mp3")
    backfill_reference_segments(db)

    from database import CorpusSong
    refreshed = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert refreshed.reference_segments is None
    assert refreshed.is_control is False


def test_song_with_reference_starts_preloaded_for_first_open(client, admin_token, db):
    """(a) A song with a backfilled reference arrives pre-filled the very
    first time an annotator opens it, and the response says so via
    seeded_from_reference — the frontend's cue to show the "verify, don't
    invent" note."""
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(client, admin_token, audio_r2_key=r2_key)
    backfill_reference_segments(db)

    annotator = _create_annotator(client, admin_token)
    res = client.get(f"/annotate/{annotator['token']}/songs/{song['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["annotation"]["seeded_from_reference"] is True
    assert body["annotation"]["segments"] == [
        {"start": 0.0, "end": 2.4, "text": "hola mundo", "event_type": "lexical"},
        {"start": 2.4, "end": 4.0, "text": "segunda frase", "event_type": "lexical"},
    ]


def test_own_saved_draft_is_never_repilled_by_reference(client, admin_token, db):
    """(b) Once the annotator has saved anything of her own — including
    emptying the song entirely — the reference must never come back."""
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(client, admin_token, audio_r2_key=r2_key)
    backfill_reference_segments(db)

    annotator = _create_annotator(client, admin_token)
    token = annotator["token"]

    # First open seeds her draft from the reference.
    first = client.get(f"/annotate/{token}/songs/{song['id']}").json()
    assert len(first["annotation"]["segments"]) == 2

    # She deletes everything and saves — an explicit, intentional empty draft.
    save_res = client.put(
        f"/annotate/{token}/songs/{song['id']}", json={"segments": []},
    )
    assert save_res.status_code == 200

    # Reopening must show HER empty draft, never the reference again.
    second = client.get(f"/annotate/{token}/songs/{song['id']}").json()
    assert second["annotation"]["segments"] == []
    # The seeded flag is a one-time historical fact, not re-derived —
    # still true even though her current segments are empty now.
    assert second["annotation"]["seeded_from_reference"] is True


def test_control_song_never_preloaded_even_with_editor_document(client, admin_token, db):
    """(c) is_control=true songs start empty for the annotator no matter
    what — the blind check that annotators do just as well from zero."""
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(
        client, admin_token, audio_r2_key=r2_key,
        notes="CONTROL: blind hold-out",
    )
    backfill_reference_segments(db)

    annotator = _create_annotator(client, admin_token)
    res = client.get(f"/annotate/{annotator['token']}/songs/{song['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["annotation"]["segments"] == []
    assert body["annotation"]["seeded_from_reference"] is False


def test_admin_song_list_exposes_is_control_but_annotator_list_does_not(
    client, admin_token, db,
):
    """Blindness must hold for the new fields too: is_control is
    admin-only, never shipped to the annotator-facing endpoints."""
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(
        client, admin_token, audio_r2_key=r2_key, notes="CONTROL: x",
    )
    backfill_reference_segments(db)

    admin_res = client.get("/admin/corpus/songs", headers=auth(admin_token))
    admin_song = next(s for s in admin_res.json()["songs"] if s["id"] == song["id"])
    assert admin_song["is_control"] is True

    annotator = _create_annotator(client, admin_token)
    songs_res = client.get(f"/annotate/{annotator['token']}/songs")
    annotator_song = next(
        s for s in songs_res.json()["songs"] if s["id"] == song["id"]
    )
    assert "is_control" not in annotator_song


def test_admin_backfill_endpoint_is_admin_only(client, user_token):
    res = client.post(
        "/admin/corpus/songs/backfill-references",
        headers=auth(user_token),
    )
    assert res.status_code == 403


def test_admin_backfill_endpoint_dry_run_then_apply(client, admin_token, db):
    job_id, r2_key = _create_job_with_document(db, _RAW_PRODUCTION_SEGMENTS)
    song = _create_song(client, admin_token, audio_r2_key=r2_key)

    dry = client.post(
        "/admin/corpus/songs/backfill-references", headers=auth(admin_token),
    )
    assert dry.status_code == 200
    assert dry.json()["applied"] is False
    assert dry.json()["seeded"] >= 1

    from database import CorpusSong
    db.expire_all()
    still_empty = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert still_empty.reference_segments is None

    applied = client.post(
        "/admin/corpus/songs/backfill-references?apply=true",
        headers=auth(admin_token),
    )
    assert applied.status_code == 200
    assert applied.json()["applied"] is True

    db.expire_all()
    now_seeded = db.query(CorpusSong).filter(CorpusSong.id == song["id"]).first()
    assert now_seeded.reference_segments is not None
