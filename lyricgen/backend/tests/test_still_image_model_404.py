"""Vertex stopped serving the Imagen family — the still path must not use it.

Incident 2026-09-01. Verified against the live production service account
(`lyricgen-vertex@gen-lang-client-0900526123`):

    POST .../publishers/google/models/imagen-4.0-generate-001:predict
      → 404 "Publisher model ... was not found or your project does not have
        access to it"
    ... same for imagen-4.0-ultra/fast, imagen-3.0-generate-001/002 and the
        legacy imagegeneration@006, in us-central1 / us-east4 / europe-west4 /
        asia-northeast1
    ... while veo-3.1-lite, gemini-2.5-flash AND gemini-2.5-flash-image all
        answer with the SAME credentials, project and region.

Nobody hit it because the paths that route to a still (`foto-parallax`,
`effect=foto_viva`, explicit `bg_mode=imagen`) went unused all August: the
last Imagen row in production `ai_provenance` is 2026-07-16. These tests pin
the trap shut.

Every test here fails if the bug is re-injected:
  - flip the default back to an `imagen-*` id           → test_default_*
  - delete the stale-env guard in _resolve_still_image_model → test_stale_*
  - route the call back through client.models.generate_images → test_generate_*
    (the fake client raises the REAL 404 from that method, exactly like prod)
"""
import os

import pytest
from google.genai.errors import ClientError

import pipeline


# The verbatim payload Vertex returns today for any Imagen id.
_VERTEX_404 = {
    "error": {
        "code": 404,
        "message": (
            "Publisher model `projects/gen-lang-client-0900526123/locations/"
            "us-central1/publishers/google/models/imagen-4.0-generate-001` was "
            "not found or your project does not have access to it."
        ),
        "status": "NOT_FOUND",
    }
}

_IMAGEN_IDS = [
    "imagen-4.0-generate-001",
    "imagen-4.0-ultra-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-3.0-generate-002",
    "imagegeneration@006",
]


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """Model selection must be decided by the code under test, not by whatever
    the developer happens to have exported."""
    for var in ("IMAGEN_MODEL", "IMAGEN_MODEL_PARALLAX", "STILL_IMAGE_MODEL",
                "ALLOW_VERTEX_IMAGEN"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def test_default_still_model_is_never_imagen():
    """With a clean env the still path must pick a model Vertex still serves."""
    chosen = pipeline._resolve_still_image_model(None)
    assert not chosen.lower().startswith(("imagen-", "imagegeneration")), (
        f"default still model is {chosen!r}, which Vertex 404s for this "
        "project since 2026-07-16 — every 'Foto fija' render would fail"
    )
    assert chosen == "gemini-2.5-flash-image"


@pytest.mark.parametrize("dead_model", _IMAGEN_IDS)
def test_stale_imagen_env_is_rewritten(monkeypatch, dead_model):
    """A stale IMAGEN_MODEL must not be able to re-arm the 404.

    This is not hypothetical: Railway production still has
    IMAGEN_MODEL_PARALLAX=imagen-4.0-ultra-generate-001 set, so a
    defaults-only fix would have left the trap armed in prod.
    """
    monkeypatch.setenv("IMAGEN_MODEL", dead_model)
    assert pipeline._resolve_still_image_model(None) == "gemini-2.5-flash-image"


@pytest.mark.parametrize("dead_model", _IMAGEN_IDS)
def test_explicit_imagen_argument_is_rewritten(dead_model):
    """The foto-parallax call site passes a model explicitly; guard that too."""
    assert pipeline._resolve_still_image_model(dead_model) == "gemini-2.5-flash-image"


def test_stale_imagen_fallback_env_does_not_defeat_the_guard(monkeypatch):
    """If STILL_IMAGE_MODEL itself names a dead model, fall back to the
    hardcoded live default instead of returning a guaranteed 404."""
    monkeypatch.setenv("STILL_IMAGE_MODEL", "imagen-4.0-generate-001")
    assert pipeline._resolve_still_image_model(None) == "gemini-2.5-flash-image"
    assert pipeline._resolve_still_image_model("imagen-3.0-generate-002") == (
        "gemini-2.5-flash-image"
    )


def test_live_model_override_is_honoured(monkeypatch):
    """The guard must only rewrite the dead family, not pin us to one model."""
    monkeypatch.setenv("STILL_IMAGE_MODEL", "gemini-3-pro-image")
    assert pipeline._resolve_still_image_model(None) == "gemini-3-pro-image"


def test_escape_hatch_lets_imagen_through(monkeypatch):
    """ALLOW_VERTEX_IMAGEN=1 exists to re-probe Google restoring access."""
    monkeypatch.setenv("ALLOW_VERTEX_IMAGEN", "1")
    monkeypatch.setenv("IMAGEN_MODEL", "imagen-4.0-generate-001")
    assert pipeline._resolve_still_image_model(None) == "imagen-4.0-generate-001"


def test_parallax_call_site_no_longer_hardcodes_a_dead_model():
    """`_ensure_background`'s foto-parallax branch used to default to
    imagen-4.0-ultra-generate-001 in code. A code default naming a 404 model
    is a trap even with the resolver in front of it."""
    import inspect

    src = inspect.getsource(pipeline._ensure_background)
    # Anchor on the actual env read, not the surrounding prose — a comment
    # mentioning the var name must not shadow the code being pinned.
    idx = src.find('os.environ.get("IMAGEN_MODEL_PARALLAX"')
    assert idx > 0, "foto-parallax branch must still read IMAGEN_MODEL_PARALLAX"
    window = src[idx:idx + 200]
    assert "imagen-" not in window, (
        "the foto-parallax model default must not name an Imagen id; Vertex "
        f"404s all of them. Got: {window!r}"
    )


# ---------------------------------------------------------------------------
# Runtime dispatch — the behavioural test
# ---------------------------------------------------------------------------

class _FakeModels:
    """Mimics the split Vertex surface: Imagen 404s, Gemini answers."""

    def __init__(self, image_bytes=b"\x89PNG\r\n\x1a\n" + b"x" * 64):
        self.image_bytes = image_bytes
        self.generate_content_calls = []
        self.generate_images_calls = []

    def generate_images(self, **kwargs):
        self.generate_images_calls.append(kwargs)
        # Exactly what production Vertex answers today.
        raise ClientError(404, _VERTEX_404)

    def generate_content(self, **kwargs):
        self.generate_content_calls.append(kwargs)
        return _FakeResponse(self.image_bytes)


class _FakeInline:
    def __init__(self, data):
        self.data = data
        self.mime_type = "image/png"


class _FakePart:
    def __init__(self, data=None, text=None):
        self.inline_data = _FakeInline(data) if data is not None else None
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, data):
        parts = [_FakePart(text="Here you go")]
        if data is not None:
            parts.append(_FakePart(data=data))
        self.candidates = [_FakeCandidate(parts)]


class _FakeClient:
    def __init__(self, models):
        self.models = models


def test_generate_still_uses_gemini_and_never_calls_imagen(monkeypatch, tmp_path):
    """The end-to-end guard.

    The fake client raises the real Vertex 404 from `generate_images`, so if
    anyone routes the still back through the Imagen surface this test blows up
    with the production error instead of writing a file.
    """
    models = _FakeModels()
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))

    out = tmp_path / "bg_imagen.jpg"
    pipeline._generate_imagen_image("a neon street at night", str(out))

    assert models.generate_images_calls == [], (
        "the still path called client.models.generate_images — that is the "
        "Imagen surface Vertex 404s for this project"
    )
    assert len(models.generate_content_calls) == 1
    assert out.exists() and out.read_bytes() == models.image_bytes

    call = models.generate_content_calls[0]
    assert call["model"] == "gemini-2.5-flash-image"
    modalities = list(call["config"].response_modalities or [])
    assert "IMAGE" in modalities, (
        "without response_modalities=IMAGE the Gemini model answers text-only "
        "and no still is produced"
    )


def test_generate_still_requests_16_9(monkeypatch, tmp_path):
    """The still is scale+crop'd to 1920x1080; asking for 16:9 up front keeps
    the crop from eating the composition."""
    from google import genai

    if "image_config" not in genai.types.GenerateContentConfig.model_fields:
        pytest.skip("installed google-genai predates image_config")

    models = _FakeModels()
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))
    pipeline._generate_imagen_image("x", str(tmp_path / "o.jpg"))

    cfg = models.generate_content_calls[0]["config"]
    assert cfg.image_config is not None
    assert cfg.image_config.aspect_ratio == "16:9"


def test_stale_parallax_env_still_reaches_gemini(monkeypatch, tmp_path):
    """Production's IMAGEN_MODEL_PARALLAX must not reach the wire."""
    monkeypatch.setenv("IMAGEN_MODEL", "imagen-4.0-ultra-generate-001")
    models = _FakeModels()
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))

    out = tmp_path / "o.jpg"
    pipeline._generate_imagen_image("x", str(out))

    assert models.generate_images_calls == []
    assert models.generate_content_calls[0]["model"] == "gemini-2.5-flash-image"
    assert out.exists()


def test_safety_block_raises_instead_of_writing_an_empty_still(monkeypatch, tmp_path):
    """A text-only answer (safety block) must raise so the caller falls back to
    the gradient, not leave a 0-byte jpg that dies later inside ffmpeg."""
    models = _FakeModels(image_bytes=None)
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))

    out = tmp_path / "o.jpg"
    with pytest.raises(RuntimeError, match="no image part"):
        pipeline._generate_imagen_image("x", str(out))
    assert not out.exists()


def test_prompt_rails_survive_the_provider_swap(monkeypatch, tmp_path):
    """The anti-illustration / anti-face / anti-text negatives added at the
    Imagen boundary (2026-07-24 and 2026-07-29) must still ride along on the
    Gemini call — they are the only rail on this path."""
    models = _FakeModels()
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))
    pipeline._generate_imagen_image("a neon street", str(tmp_path / "o.jpg"))

    sent = models.generate_content_calls[0]["contents"]
    assert "no recognizable faces" in sent
    assert "no illustration" in sent
    assert "no logos" in sent
    assert "Photorealistic photograph" in sent


def test_allow_people_drops_the_face_rail(monkeypatch, tmp_path):
    """Same contract as before the swap: allow_people=True removes the
    no-people clauses but keeps the logo/text negatives."""
    models = _FakeModels()
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(models))
    pipeline._generate_imagen_image(
        "a crowd", str(tmp_path / "o.jpg"), allow_people=True,
    )

    sent = models.generate_content_calls[0]["contents"]
    assert "no recognizable faces" not in sent
    assert "no logos" in sent


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------

def test_still_model_is_priced_in_the_cost_table():
    """An unpriced model falls through to DEFAULT_COST_PER_CALL ($0.01) and
    under-counts the cost panel — the exact bug the 2026-08 audit fixed for
    Imagen 4. Don't reintroduce it by swapping the model."""
    import provenance

    model = pipeline._resolve_still_image_model(None)
    assert (model, "google_vertex") in provenance.COST_PER_CALL, (
        f"{model} is the still-image default but has no COST_PER_CALL entry"
    )
    assert provenance.COST_PER_CALL[(model, "google_vertex")] == pytest.approx(0.039)


def test_image_provider_reports_the_live_model():
    """`ai_providers` feeds provenance/admin; it must not advertise a model
    that 404s."""
    import ai_providers

    os.environ.pop("IMAGEN_MODEL", None)
    provider = ai_providers.get_image_provider()
    assert provider.get_model_version() == "gemini-2.5-flash-image"
