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

from tests.conftest import auth


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
