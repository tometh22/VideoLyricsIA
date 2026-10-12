import pytest

from tests.conftest import auth


@pytest.fixture(autouse=True)
def _no_real_genre_detection(monkeypatch):
    import genre_detect
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None: ("", "none"))


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
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None: ("rock", "cache"))
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
    monkeypatch.setattr(genre_detect, "detect", lambda a, t, l="", job_id=None: ("metal", "cache"))
    r = client.post("/lyric-looks/resolve", json={"lyric_look": "cinetico", "artist": "Almafuerte",
                                                   "song_title": "Niño Jefe"},
                    headers=auth(user_token)).json()
    assert r["genre"] == "metal" and r["family"] == "heavy" and r["genre_source"] == "detected"
