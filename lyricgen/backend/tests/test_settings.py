"""Tests for settings and jobs endpoints."""

from tests.conftest import auth


def test_settings_get_empty(client, user_token):
    res = client.get("/settings", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json() == {}


def test_settings_save_and_load(client, user_token):
    settings = {"titleFormat": "{artista} - {cancion}", "hashtags": "#test"}
    res = client.post("/settings", headers=auth(user_token), json=settings)
    assert res.status_code == 200
    assert res.json()["ok"] is True

    res = client.get("/settings", headers=auth(user_token))
    assert res.status_code == 200
    data = res.json()
    assert data["titleFormat"] == "{artista} - {cancion}"
    assert data["hashtags"] == "#test"


def test_settings_overwrite(client, user_token):
    client.post("/settings", headers=auth(user_token), json={"key1": "val1"})
    client.post("/settings", headers=auth(user_token), json={"key2": "val2"})

    res = client.get("/settings", headers=auth(user_token))
    data = res.json()
    assert "key2" in data


def test_jobs_list_empty(client, user_token):
    res = client.get("/jobs", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json() == []


def test_status_not_found(client, user_token):
    res = client.get("/status/nonexistent123", headers=auth(user_token))
    assert res.status_code == 404


def test_download_invalid_type(client, user_token):
    res = client.get("/download/somejob/invalid?token=fake")
    assert res.status_code in (400, 401)
