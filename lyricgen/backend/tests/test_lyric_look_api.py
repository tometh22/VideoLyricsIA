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
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "cinetico", "artist": "Almafuerte",
                                                   "song_title": "Niño Jefe"},
                    headers=auth(user_token)).json()
    assert r["genre"] == "metal" and r["family"] == "heavy" and r["genre_source"] == "detected"


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
    assert calls and all(kw.get("cached_only") for kw in calls)


def test_resolve_automatic_genre_ignores_the_stored_operator_genre(client):
    token, job_id = _user_with_job(client, {
        "lyric_look": "neon", "lyric_genre": "metal", "lyric_genre_detected": "pop",
        "lyric_variant": 3, "lyric_variant_sig": ["x"]})
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id},
                    headers=auth(token)).json()
    # "Automático" = the detected genre, not the metal the operator had set.
    assert r["genre"] == "pop" and r["genre_source"] == "detected"
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


def test_resolve_fresh_avoids_the_parent_variant(client):
    import lyric_recipes as R
    seed = R.seed_key_for("Almafuerte", "Niño Jefe")
    v = R.choose_auto_variant("neon", "metal", seed, set())
    sig = [list(x) if isinstance(x, tuple) else x
           for x in R.recipe_for("neon", "metal", seed, v).signature]
    token, job_id = _user_with_job(client, {"lyric_look": "neon", "lyric_genre": "metal",
                                            "lyric_variant": v, "lyric_variant_sig": sig})
    same = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id,
                                                      "lyric_genre": "metal"},
                       headers=auth(token)).json()
    fresh = client.post("/lyric-looks/resolve", json={"lyric_look": "neon", "job_id": job_id,
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
