from pathlib import Path
import json
from pydantic import ValidationError
import pytest
import ass_render as ass
from editor_preview import build_text_preview, preview_font, TextPreviewRequest
from lyric_overlay import compose_lyric_overlay

FONT = str(Path(__file__).parents[1] / "fonts" / "Montserrat-Bold.ttf")
TEXT = "¡Si es bueno para mí, es malo para vos!"


@pytest.mark.parametrize("font_id", ["montserrat-bold", "poppins-bold", "anton", "fredoka", "outfit-bold"])
@pytest.mark.parametrize("scale", [0.6, 1, 1.5])
def test_preview_matches_the_final_track(font_id, scale):
    import pipeline
    rows = [{"start": 0, "end": 3, "text": TEXT, "pos": {"x": .5, "y": .5}}]
    result = build_text_preview(rows, {"font": font_id, "font_scale": scale})
    path = pipeline._resolve_font(font_id)
    family, bold = ass.font_family(path)
    factor = ass.font_size_factor(family)
    # Independent baseline from the former production composition, including
    # its actual margins, wrapping mode, faux-bold and font normalization.
    expected = ass.build_ass(
        width=1920, height=1080, font_name=family, bold=bold,
        base_fontsize=ass.lyric_fontsize(40, 1, scale, factor), outline=2.5, shadow=3,
        lines=ass.segments_to_lines(rows, text_scale=1, font_scale=scale,
                                   font_factor=factor, case_fn=str.upper),
    )
    assert result["ass"] == expected
    assert result["fonts"] == [Path(path).name]


def test_shared_composer_keeps_titles_and_approved_timing():
    rows = [{"start": 1.2, "end": 2.3, "text": "Exacta"}]
    titles = [ass.AssLine(text="Título", start_s=0, end_s=1, fontsize=90)]
    track = compose_lyric_overlay(rows, width=1920, height=1080, text_scale=1,
                                  font_path=FONT, case_fn=str.upper, title_lines=titles)
    assert "0:00:01.20,0:00:02.30" in track
    assert "Título" in track


def test_preview_preserves_word_animation_and_layout():
    result = build_text_preview([{"start": 1, "end": 4, "text": "hola mundo", "scale": 1.2,
        "pos": {"x": .25, "y": .7}, "rot": 8,
        "words": [{"word": "hola", "start": 1, "end": 2}, {"word": "mundo", "start": 2, "end": 4}]}],
        {"font": "montserrat-bold", "lyrics_animation": "karaoke"})
    assert "\\kf" in result["ass"]
    assert "\\pos(480,756)" in result["ass"]
    assert "\\frz-8" in result["ass"]


def test_look_uses_its_real_composer_and_fonts():
    import lyric_looks
    import lyric_look_runtime
    rows = [{"start": 0, "end": 3, "text": TEXT}]
    look_id = next(iter(lyric_looks.LOOKS))
    result = build_text_preview(rows, {"lyric_look": look_id, "lyric_variant": 0})
    opts, _ = lyric_look_runtime.prepare_opts(job_id="", lyric_look=look_id,
        render_params={"lyric_variant": 0}, detect_genre=False)
    look = lyric_look_runtime.resolve_for_render(look_id, opts)
    expected = lyric_looks.build_look_ass(rows, look, width=1920, height=1080, duration=3,
        case_fn=str.upper, font_scale=1, primary_override="", title_lines=[], beats=[])
    assert result["ass"] == expected
    assert set(result["fonts"]) == {Path(p).name for p in lyric_looks.font_paths(look)}


@pytest.mark.parametrize("filename", ["../main.py", "/etc/passwd", "main.py", "missing.ttf"])
def test_fonts_are_restricted_to_bundled_files(filename):
    with pytest.raises(ValueError):
        preview_font(filename)


def test_preview_does_not_save_or_call_paid_generation(monkeypatch):
    import jobs
    monkeypatch.setattr(jobs, "merge_render_params", lambda *a, **k: pytest.fail("Preview cannot save a job"))
    result = build_text_preview([{"start": 0, "end": 1, "text": TEXT}], {})
    assert result["resolved_font"] == "montserrat-bold"
    assert TextPreviewRequest(segments=[]).settings.model_dump(exclude_unset=True) == {}


def test_api_checks_auth_job_scope_and_input(client, user_token, db, monkeypatch):
    import uuid
    from database import Job
    monkeypatch.setenv("EDITOR_V2_GLOBALLY_ENABLED", "1")
    headers = {"Authorization": f"Bearer {user_token}"}
    user = client.get("/auth/me", headers=headers).json()
    job_id = uuid.uuid4().hex[:12]
    db.add(Job(job_id=job_id, user_id=user["id"], tenant_id=user["tenant_id"],
               filename="test.wav", artist="Test", style="oscuro", status="transcribed_pending",
               segments_json=[{"start": 0, "end": 1, "text": TEXT}]))
    db.commit()
    body = {"segments": [{"start": 0, "end": 1, "text": TEXT}], "settings": {"font_scale": 1.5}}
    path = f"/editor/{job_id}/text-preview"
    assert client.post(path, json=body).status_code == 401
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert "[Events]" in result.json()["ass"]
    assert client.post("/editor/does-not-exist/text-preview", json=body, headers=headers).status_code == 404
    assert client.post(path, json={**body, "settings": {"font": ["bad"]}}, headers=headers).status_code == 422
    assert client.get("/editor/preview/fonts/Montserrat-Bold.ttf", headers=headers).status_code == 200
    assert client.get("/editor/preview/fonts/main.py", headers=headers).status_code == 404
    assert client.get("/editor/preview/fonts/Montserrat-Bold.ttf").status_code == 401


def test_browser_render_fixture_matches_the_actual_track():
    import json
    path = Path(__file__).parents[2] / "frontend/e2e/fixtures/native-preview.json"
    expected = build_text_preview([{"start": 0, "end": 3, "text": TEXT}],
                                  {"font": "montserrat-bold", "font_scale": 1.5})
    assert json.loads(path.read_text()) == expected


def test_preview_respects_the_umg_master_dimensions():
    from render_spec import RenderSpec
    spec = RenderSpec.umg_intermediate_master({"frame_size": "DCI-4K", "fps": 24, "prores_profile": 3})
    result = build_text_preview([{"start": 0, "end": 3, "text": TEXT}], {}, spec=spec)
    assert result["width"] == spec.width
    assert result["height"] == spec.height
    assert f"PlayResX: {spec.width}" in result["ass"]
    assert f"PlayResY: {spec.height}" in result["ass"]


def test_preview_accepts_the_actual_editor_automatic_variant_payload():
    path = Path(__file__).parents[2] / "frontend/e2e/fixtures/native-preview-auto-request.json"
    payload = json.loads(path.read_text())
    parsed = TextPreviewRequest.model_validate(payload)
    assert parsed.settings.lyric_variant is None
    result = build_text_preview(parsed.segments, parsed.settings.model_dump(exclude_unset=True))
    assert "[Events]" in result["ass"]
    assert result["fonts"] == ["Montserrat-Bold.ttf"]


@pytest.mark.parametrize("variant", [-1, 10000, "arbitrary", 1.5, [], {}])
def test_preview_still_rejects_invalid_variants(variant):
    with pytest.raises(ValidationError):
        TextPreviewRequest(segments=[], settings={"lyric_variant": variant})


def test_preview_keeps_document_limit_bounded():
    from editor import MAX_SEGMENTS
    with pytest.raises(ValidationError):
        TextPreviewRequest(segments=[{}] * (MAX_SEGMENTS + 1))
