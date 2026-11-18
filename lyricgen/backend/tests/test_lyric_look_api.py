import pytest

from tests.conftest import auth


@pytest.fixture(autouse=True)
def _no_real_genre_detection(monkeypatch):
    import genre_detect
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None, **_kw: ("", "none"))


def test_catalog_requires_auth_and_lists_fonts(client, user_token):
    assert client.get("/lyric-looks/catalog").status_code in (401, 403)
    r = client.get("/lyric-looks/catalog", headers=auth(user_token))
    assert r.status_code == 200
    body = r.json()
    assert any(f["id"] == "metal-mania" for f in body["fonts"])
    assert "heavy" in body["recommended"]


def test_resolve_is_deterministic_and_reports_overrides(client, user_token):
    payload = {"lyric_look": "cinetico", "lyric_genre": "metal", "lyric_variant": 2,
               "artist": "Almafuerte", "song_title": "Niño Jefe",
               "lyric_accent_color": "#00FF88"}
    a = client.post("/lyric-looks/resolve", json=payload, headers=auth(user_token)).json()
    b = client.post("/lyric-looks/resolve", json=payload, headers=auth(user_token)).json()
    assert a == b
    assert a["family"] == "heavy" and a["variant"] == 2
    assert a["colors"]["accent"] == "#00FF88" and a["overridden"]["accent"]
    assert a["genre_source"] == "operator"


def test_resolve_auto_variant_returns_a_number(client, user_token):
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "artist": "X",
                                                   "song_title": "Y"},
                    headers=auth(user_token))
    assert r.status_code == 200 and isinstance(r.json()["variant"], int)


def test_resolve_rejects_unknown_look(client, user_token):
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "glitter"},
                    headers=auth(user_token))
    assert r.status_code == 422


def test_detect_genre_uses_the_cache_and_never_500s(client, user_token, monkeypatch):
    import genre_detect
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None, **_kw: ("rock", "cache"))
    r = client.post("/lyric-looks/detect-genre",
                    json={"artist": "Divididos", "song_title": "El 38"},
                    headers=auth(user_token))
    assert r.status_code == 200 and r.json() == {"genre": "rock", "family": "heavy",
                                                 "source": "cache"}


def test_generate_kwargs_carry_only_set_variant_fields():
    import main
    assert main._lyric_look_kwargs("", lyric_genre="metal") == {}
    assert main._lyric_look_kwargs("cine", lyric_genre="", lyric_variant="") == {"lyric_look": "cine"}
    assert main._lyric_look_kwargs("cine", lyric_genre="Rock", lyric_variant="3",
                                   lyric_font_main="anton", lyric_accent_color="#ff0000") == {
        "lyric_look": "cine", "lyric_genre": "rock", "lyric_variant": 3,
        "lyric_font_main": "anton", "lyric_accent_color": "#FF0000"}


def test_campaign_fields_expose_the_variant_controls():
    import campaign_creative as cc
    for f in ("lyric_genre", "lyric_variant", "lyric_font_main", "lyric_font_key",
              "lyric_alt_color", "lyric_accent_color"):
        assert f in cc.FIELDS and f in cc.RENDER_KEYS and cc.DEFAULTS[f] == ""


def test_edit_persists_look_text_colour_and_variant_fields():
    import main
    from types import SimpleNamespace
    job = SimpleNamespace(render_params={"lyric_look": "cine", "lyric_variant": 3,
                                         "lyric_variant_sig": ["x"]})
    body = SimpleNamespace(lyric_color="#ffeeaa", lyric_genre="metal", lyric_variant=None,
                           lyric_font_main="anton", lyric_font_key=None,
                           lyric_alt_color="", lyric_accent_color=None)
    edit_params = {}
    main._apply_lyric_variant_edit(body, job, edit_params)
    rp = job.render_params
    assert rp["lyric_color"] == "#FFEEAA" and edit_params["lyric_color"] == "#FFEEAA"
    assert rp["lyric_genre"] == "metal" and rp["lyric_font_main"] == "anton"
    assert "lyric_variant" not in rp and edit_params["lyric_variant"] == ""   # genre change → auto
    assert "lyric_alt_color" not in rp and edit_params["lyric_alt_color"] == ""
    assert "lyric_variant_sig" not in rp


def test_variant_overridable_fields_include_the_look_controls():
    import main
    for f in ("lyric_look", "lyric_color", "lyric_genre", "lyric_variant", "lyric_font_main",
              "lyric_font_key", "lyric_alt_color", "lyric_accent_color"):
        assert f in main._VARIANT_OVERRIDABLE_FIELDS


def test_resolve_without_genre_uses_the_same_detection_as_the_render(client, user_token, monkeypatch):
    import genre_detect
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None, **_kw: ("metal", "cache"))
    # "Otra variante" (variant ≥ 1) with an automatic genre uses the detection.
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "cinetico", "artist": "Almafuerte",
                                                   "song_title": "Niño Jefe", "lyric_variant": 1},
                    headers=auth(user_token)).json()
    assert r["genre"] == "metal" and r["family"] == "heavy" and r["genre_source"] == "detected"
    # Automatic variant = the look's original design, whatever was detected.
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "cinetico", "artist": "Almafuerte",
                                                   "song_title": "Niño Jefe"},
                    headers=auth(user_token)).json()
    assert r["variant"] == 0 and r["genre"] == "" and r["description"] == "Diseño original"


# --- Regressions of the 12-oct review ------------------------------------------------

def _user_with_job(client, render_params):
    import uuid
    from database import SessionLocal, User, Job
    username = f"lookuser_{uuid.uuid4().hex[:6]}"
    token = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com"}).json()["token"]
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        job_id = uuid.uuid4().hex[:12]
        db.add(Job(job_id=job_id, user_id=u.id, tenant_id=u.tenant_id, artist="Almafuerte",
                   song_title="Niño Jefe", style="oscuro", filename="song.wav",
                   status="done", current_step="done", progress=100,
                   delivery_profile="youtube", render_params=render_params))
        db.commit()
        return token, job_id
    finally:
        db.close()


def test_resolve_accepts_a_null_job_id(client, user_token):
    # The wizard has no job before transcription and campaigns never do.
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": None},
                    headers=auth(user_token))
    assert r.status_code == 200, r.text
    r = client.post("/lyric-looks/detect-genre", json={"artist": "A", "job_id": None},
                    headers=auth(user_token))
    assert r.status_code == 200, r.text


def test_resolve_never_pays_for_detection(client, user_token, monkeypatch):
    import genre_detect
    calls = []
    monkeypatch.setattr(genre_detect, "detect",
                        lambda a, t, l="", job_id=None, **kw: calls.append(kw) or ("", "none"))
    client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "artist": "X", "song_title": "Y"},
                headers=auth(user_token))
    assert calls == []              # the original design needs no genre at all
    client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "artist": "X", "song_title": "Y",
                                              "lyric_variant": 2},
                headers=auth(user_token))
    assert calls and all(kw.get("cached_only") for kw in calls)


def test_resolve_automatic_genre_ignores_the_stored_operator_genre(client):
    token, job_id = _user_with_job(client, {
        "lyric_look": "neon", "lyric_genre": "metal", "lyric_genre_detected": "pop",
        "lyric_variant": 3, "lyric_variant_sig": ["x"]})
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id,
                                                   "lyric_variant": 1},
                    headers=auth(token)).json()
    # "Automático" = the detected genre, not the metal the operator had set.
    assert r["genre"] == "pop" and r["genre_source"] == "detected"
    # Automatic genre + automatic variant: the stored variant belonged to the
    # operator's metal (the edit drops it with the genre) → original design,
    # which is what the edit pipeline renders.
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id},
                    headers=auth(token)).json()
    assert r["variant"] == 0 and r["genre"] == "" and r["description"] == "Diseño original"
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id,
                                                   "lyric_genre": "metal"},
                    headers=auth(token)).json()
    assert r["genre"] == "metal" and r["variant"] == 3     # stored variant kept


def test_resolve_previews_a_legacy_job_as_designed(client):
    token, job_id = _user_with_job(client, {"lyric_look": "cinetico",
                                            "lyric_genre_detected": "metal"})
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "cinetico", "job_id": job_id},
                    headers=auth(token)).json()
    assert r["variant"] == 0 and r["genre"] == "" and r["background"] == "flat"


@pytest.mark.parametrize("look_id", ["neon", "golpe", "recorte", "diptico", "diario", "nube", "puntada"])
def test_resolve_fresh_avoids_the_parent_variant(client, look_id):
    import lyric_recipes as R
    seed = R.seed_key_for("Almafuerte", "Niño Jefe")
    v = R.choose_auto_variant(look_id, "metal", seed, set())
    sig = [list(x) if isinstance(x, tuple) else x
           for x in R.recipe_for(look_id, "metal", seed, v).signature]
    token, job_id = _user_with_job(client, {"lyric_look": look_id, "lyric_genre": "metal",
                                            "lyric_variant": v, "lyric_variant_sig": sig})
    same = client.post("/lyric-looks/resolve", json={"lyric_look": look_id, "job_id": job_id,
                                                      "lyric_genre": "metal"},
                       headers=auth(token)).json()
    fresh = client.post("/lyric-looks/resolve", json={"lyric_look": look_id, "job_id": job_id,
                                                       "lyric_genre": "metal", "fresh": True},
                        headers=auth(token)).json()
    assert same["variant"] == v and fresh["variant"] != v


def test_detect_genre_does_not_charge_another_tenants_job(client, user_token, monkeypatch):
    import genre_detect
    seen = []
    monkeypatch.setattr(genre_detect, "detect",
                        lambda a, t, l="", job_id=None, **kw: seen.append(job_id) or ("rock", "ai"))
    _token, other_job = _user_with_job(client, {})
    client.post("/lyric-looks/detect-genre", json={"artist": "A", "job_id": other_job},
                headers=auth(user_token))
    assert seen == [None]


def test_editorial_designs_resolve_as_original_over_source_video(client, user_token):
    import lyric_looks
    import main
    for code in lyric_looks.EDITORIAL_LAYOUTS:
        response = client.post('/lyric-looks/resolve', json={
            'lyric_look': code, 'lyric_genre': 'rock',
            'artist': 'Almafuerte', 'song_title': 'Niño jefe',
        }, headers=auth(user_token))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['look'] == code and result['variant'] == 0
        assert result['background'] == 'source'
        assert main._lyric_look_kwargs(code, lyric_variant=0)['lyric_look'] == code


# --- 11-nov-2026: the wizard's look reaches the first render ------------------------

def test_resolve_previews_the_original_for_an_automatic_variant(client, user_token):
    # Wizard (no job yet): look picked, genre/variant automatic → the look as
    # designed, so the pinned variant the wizard submits is 0 = original.
    for look_id in ("cine", "romantico", "neon", "perspectiva", "arco", "bloque"):
        r = client.post("/lyric-looks/resolve", json={
            "lyric_look": look_id, "artist": "Diego Faria", "song_title": "Rosa de Fogo",
            "lyric_variant": ""}, headers=auth(user_token)).json()
        assert r["variant"] == 0 and r["genre"] == "", (look_id, r)
        assert r["description"] == "Diseño original", (look_id, r)


def test_resolve_fresh_automatic_variant_is_never_the_original(client):
    # /variant wizard: an automatic variant must be a NEW recipe (≥ 1), since
    # the render reads a pinned variant 0 + automatic genre as the original.
    import lyric_recipes as R
    seed = R.seed_key_for("Almafuerte", "Niño Jefe")
    sig = [list(x) if isinstance(x, tuple) else x for x in R.recipe_for("neon", "", seed, 0).signature]
    # Detected genre outside Neón's native family: variant 0 there is a
    # recipe the old code could pick (and the render would then draw the
    # original instead).
    token, job_id = _user_with_job(client, {"lyric_look": "neon", "lyric_variant": 0,
                                            "lyric_variant_sig": sig,
                                            "lyric_genre_detected": "metal"})
    for i in range(12):
        fresh = client.post("/lyric-looks/resolve", json={
            "lyric_look": "neon", "job_id": job_id, "fresh": True,
            "artist": "Almafuerte", "song_title": f"Tema {i}"},
            headers=auth(token)).json()
        assert fresh["variant"] >= 1 and fresh["description"] != "Diseño original", (i, fresh)


@pytest.mark.parametrize("segments", ["[]", '[{"start": 0, "end": 1, "text": "rosa de fogo"}]'])
@pytest.mark.parametrize("variant", ["", "0"])
def test_generate_forwards_the_wizard_look_to_the_render(client, monkeypatch, segments, variant):
    # "Generar sin revisar" (segments []) and "Revisar letra" → approve
    # (edited segments) share POST /generate: the look must reach the worker.
    from tests.test_main_generate_with_job_id import _make_user, _seed_transcribed_pending
    from database import SessionLocal, User
    username, token = _make_user(client)
    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        user_id, tenant_id = u.id, u.tenant_id
    finally:
        s.close()
    job_id = _seed_transcribed_pending(user_id, tenant_id)
    captured = {}
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: captured.update(kw) or "thread:fake")
    monkeypatch.setattr("main._enforce_memory_pressure", lambda: None)
    data = {"job_id": job_id, "artist": "Intoxicados", "song_title": "No Tengo Ganas",
            "style": "oscuro", "segments_json": segments, "delivery_profile": "youtube",
            "lyric_look": "cine", "lyric_color": ""}
    if variant:
        data["lyric_variant"] = variant
    res = client.post("/generate", data=data, headers=auth(token))
    assert res.status_code == 200, res.text
    assert captured["lyric_look"] == "cine"
    assert captured.get("lyric_variant") == (int(variant) if variant else None)
    assert "lyric_genre" not in captured
