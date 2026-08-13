"""P1 2026-07-17 — recompute server-side de bg_cache_key en /generate.

La race del debounce de 10s de useBackgroundPreview hacía que /generate
viajara sin bg_cache_key y el render regenerara con Veo un fondo que el
preview ya había cacheado (doble Veo + doble validación, ~2-2.5 min).
El backend ahora recomputa el key con bg_preview.job_bg_cache_key —
la MISMA función que usa la validación defensiva del worker
(pipeline._validate_bg_cache_key), así que no pueden divergir.

Guard de alcance: el recompute aplica SOLO al fondo único AI de /generate.
Custom/library/escenas tienen su propio flujo, y /variant y /retry no
deben heredar el fast path (una variante con params idénticos QUIERE un
fondo distinto).
"""
import inspect
import json

from tests.conftest import auth
from tests.test_main_generate_with_job_id import (  # noqa: F401
    _make_user,
    _seed_transcribed_pending,
)

_SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "poco a poco pude notar"},
    {"start": 2.0, "end": 4.0, "text": "que en la vida hay mas de un error"},
]


def _post_generate(client, token, job_id, *, extra=None, files=None):
    data = {
        "job_id": job_id,
        "artist": "Intoxicados",
        "song_title": "No Tengo Ganas",
        "style": "oscuro",
        "segments_json": json.dumps(_SEGMENTS),
        "delivery_profile": "youtube",
    }
    data.update(extra or {})
    return client.post("/generate", data=data, files=files, headers=auth(token))


def _setup(client, monkeypatch):
    username, token = _make_user(client)
    from database import SessionLocal, User

    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        user_id, tenant_id = u.id, u.tenant_id
    finally:
        s.close()
    job_id = _seed_transcribed_pending(user_id, tenant_id)
    captured = {}

    def _fake_enqueue(**kwargs):
        captured.update(kwargs)
        return "thread:fake"

    monkeypatch.setattr("main.enqueue_pipeline", _fake_enqueue)
    return token, job_id, captured


def test_recompute_when_frontend_omits_key(client, monkeypatch):
    """Sin bg_cache_key en el form (la race del debounce), el backend lo
    recomputa — y coincide con el que validará el worker."""
    token, job_id, captured = _setup(client, monkeypatch)
    res = _post_generate(client, token, job_id)
    assert res.status_code == 200, res.text

    from bg_preview import job_bg_cache_key

    expected = job_bg_cache_key(
        artist="Intoxicados", song_title="No Tengo Ganas", style="oscuro",
        movement_style="", effect="", custom_colors="", genre="",
        concept="", background_hint=None, bg_verbatim=False,
        match_lyrics=True,
    )
    assert expected is not None
    assert captured["bg_cache_key"] == expected

    # Y el worker lo acepta (paridad main↔worker, la red de seguridad).
    from pipeline import _validate_bg_cache_key

    assert _validate_bg_cache_key(
        captured["bg_cache_key"], job_id=job_id, artist="Intoxicados",
        song_title="No Tengo Ganas", style="oscuro", movement_style="",
        effect="", custom_colors="", genre="", concept="",
        background_hint=None, bg_verbatim=False, match_lyrics=True,
    ) == captured["bg_cache_key"]


def test_explicit_key_passthrough(client, monkeypatch):
    """Si el frontend SÍ mandó el key, no se pisa con el recompute."""
    token, job_id, captured = _setup(client, monkeypatch)
    res = _post_generate(client, token, job_id,
                         extra={"bg_cache_key": "abcdef123456"})
    assert res.status_code == 200, res.text
    assert captured["bg_cache_key"] == "abcdef123456"


def test_no_recompute_with_custom_background_file(client, monkeypatch):
    """Fondo custom (upload) → sin fast path de cache: el recompute no
    corre y bg_cache_key queda None."""
    token, job_id, captured = _setup(client, monkeypatch)
    monkeypatch.setattr(
        "main._save_custom_background",
        lambda *a, **k: ("/tmp/fake_custom_bg.mp4", None),
    )
    res = _post_generate(
        client, token, job_id,
        files={"background_file": ("bg.mp4", b"\x00\x00\x00\x18ftyp", "video/mp4")},
    )
    assert res.status_code == 200, res.text
    assert captured["bg_cache_key"] is None


def test_no_recompute_with_scenes_enabled(client, monkeypatch):
    """Multi-escena tiene su propio cache por clip — un key de fondo único
    no debe cortocircuitarlo."""
    token, job_id, captured = _setup(client, monkeypatch)
    monkeypatch.setattr("main.has_scenes_access", lambda u: True)
    res = _post_generate(client, token, job_id, extra={"enable_scenes": "true"})
    assert res.status_code == 200, res.text
    assert captured["bg_cache_key"] is None
    assert captured["enable_scenes"] is True


def test_no_recompute_for_library_variation(client, monkeypatch):
    """Audit adversarial 2026-07-17: una variation de librería devuelve
    bg_path=None con variation_source_path seteado → sin el guard explícito
    el recompute corría igual. El guard debe EXCLUIRLA por sí mismo (no
    depender de _animate_user_image downstream)."""
    token, job_id, captured = _setup(client, monkeypatch)
    monkeypatch.setattr(
        "main._resolve_library_background",
        lambda *a, **k: (None, None, "/tmp/seed.png", "r2/seed", 4242),
    )
    res = _post_generate(client, token, job_id, extra={"background_id": "77"})
    assert res.status_code == 200, res.text
    assert captured["bg_cache_key"] is None


def test_malformed_segments_dont_500(client, monkeypatch):
    """Audit adversarial: segments_json malformado (json válido pero no
    lista de dicts) no debe tirar 500 en el recompute — se cae a fresh."""
    token, job_id, captured = _setup(client, monkeypatch)
    res = client.post("/generate", data={
        "job_id": job_id, "artist": "A", "song_title": "S", "style": "oscuro",
        "segments_json": json.dumps([None, {"no_text": 1}, "basura"]),
        "delivery_profile": "youtube",
    }, headers=auth(token))
    assert res.status_code == 200, res.text
    # No crashea; el key sale recomputado (con los segments filtrados) o None,
    # pero nunca 500.
    assert "bg_cache_key" in captured


def test_variant_and_retry_never_pass_bg_cache_key():
    """Guard de regresión (hallazgo del diseño): /variant con params
    idénticos al padre QUIERE un fondo distinto — si heredara el fast
    path, la variante devolvería el mismo visual. /retry tampoco lo pasa
    (sus segments/params vienen de la fila, no de un preview)."""
    import main

    for handler in (main.retry_job, main.create_variant):
        src = inspect.getsource(handler)
        assert "bg_cache_key" not in src, (
            f"{handler.__name__} no debe tocar bg_cache_key: el recompute "
            "vive exclusivamente en /generate"
        )
