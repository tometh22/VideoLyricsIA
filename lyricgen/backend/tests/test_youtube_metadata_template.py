"""The YouTube metadata template (title format, description header/footer,
mandatory tags, hashtags, language) configured in Settings must be applied
at generation time — it used to be read from a legacy file the app never
writes, so the config was silently ignored.
"""

import json

import pytest


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, payload):
        self._payload = payload

    def generate_content(self, **kwargs):
        # Capture the prompt so we can assert the language made it in.
        _FakeModels.last_prompt = kwargs.get("contents", "")
        return _FakeResponse(json.dumps(self._payload))


class _FakeClient:
    def __init__(self, payload):
        self.models = _FakeModels(payload)


@pytest.fixture
def fake_gemini(monkeypatch):
    """Patch the Gemini client to return fixed metadata JSON."""
    import pipeline
    payload = {
        "title": "AI Title",
        "description": "AI description body",
        "tags": ["ai-tag-1", "ai-tag-2"],
    }
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: _FakeClient(payload))
    return payload


def test_db_settings_template_applied(fake_gemini):
    from youtube_upload import generate_youtube_metadata

    settings = {
        "titleFormat": "{artista} - {cancion} (Letra Oficial)",
        "descriptionHeader": "Suscribite al canal de {artista}",
        "descriptionFooter": "© 2026 Sello. Todos los derechos reservados.",
        "hashtags": "#oficial #{artista}",
        "mandatoryTags": "sello oficial, vevo",
        "metadataLanguage": "en",
    }
    md = generate_youtube_metadata(
        "Intoxicados", "Fuego", job_id=None, settings=settings,
    )

    # Title format from settings, placeholders filled.
    assert md["title"] == "Intoxicados - Fuego (Letra Oficial)"
    # Header + AI body + footer + hashtags, in order.
    parts = md["description"].split("\n\n")
    assert parts[0] == "Suscribite al canal de Intoxicados"
    assert "AI description body" in parts[1]
    assert parts[2] == "© 2026 Sello. Todos los derechos reservados."
    assert parts[3] == "#oficial #Intoxicados"
    # Mandatory tags prepended to the AI tags.
    assert md["tags"][:2] == ["sello oficial", "vevo"]
    assert "ai-tag-1" in md["tags"]
    # Language flowed into the prompt ("English", not the default "Spanish").
    assert "in English" in _FakeModels.last_prompt


def test_empty_settings_keeps_ai_metadata(fake_gemini):
    from youtube_upload import generate_youtube_metadata

    md = generate_youtube_metadata("A", "B", job_id=None, settings={})
    # No template → AI values pass through untouched.
    assert md["title"] == "AI Title"
    assert md["description"] == "AI description body"
    assert md["tags"] == ["ai-tag-1", "ai-tag-2"]


def test_upload_uses_settings_language_and_tag_override(fake_gemini, monkeypatch):
    """upload_to_youtube applies the DB language to defaultLanguage and
    honors the previewed tags (what you approve is what publishes)."""
    import youtube_upload as yu

    captured = {}

    class _FakeReq:
        def next_chunk(self):
            return None, {"id": "vid123"}

    class _FakeVideos:
        def insert(self, part, body, media_body):
            captured["body"] = body
            return _FakeReq()

    class _FakeThumbs:
        def set(self, **kw):
            class _E:
                def execute(self_):
                    return {}
            return _E()

    class _FakeYT:
        def videos(self):
            return _FakeVideos()

        def thumbnails(self):
            return _FakeThumbs()

    monkeypatch.setattr(yu, "_get_youtube_client", lambda: _FakeYT())
    monkeypatch.setattr(yu, "MediaFileUpload", lambda *a, **k: object())
    # A real file path for os.path.getsize; the video file itself is unused.
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(b"\x00" * 16)
    tmp.close()

    result = yu.upload_to_youtube(
        tmp.name, None, "Artista", "Cancion", "", "unlisted", None,
        title_override="Mi Título",
        tags_override=["custom1", "custom2"],
        settings={"metadataLanguage": "pt"},
    )
    os.unlink(tmp.name)

    assert result["video_id"] == "vid123"
    assert captured["body"]["snippet"]["title"] == "Mi Título"
    assert captured["body"]["snippet"]["tags"] == ["custom1", "custom2"]
    assert captured["body"]["snippet"]["defaultLanguage"] == "pt"
