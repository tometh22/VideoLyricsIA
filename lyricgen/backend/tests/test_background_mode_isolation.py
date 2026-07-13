"""Regression tests for creative-mode and authorization provenance."""

from __future__ import annotations

import pytest

import pipeline
from background_policy import resolve_atmospherics_policy


def test_scene_context_cannot_authorize_atmospherics(monkeypatch):
    captured = {}

    def fake_unique_prompt(**kwargs):
        captured.update(kwargs)
        return {"style": "video", "prompt": "ok"}

    monkeypatch.setattr(pipeline, "_get_unique_prompt", fake_unique_prompt)
    policy = resolve_atmospherics_policy("", mode="enforce")
    prompt_fn = pipeline._make_scene_prompt_fn(
        "lyrics mention smoke",
        "Artist",
        "Title",
        "rock",
        "",
        "auto",
        "",
        "job",
        False,
        match_lyrics=False,
        operator_prompt=None,
        creative_mode="auto",
        atmospherics_policy=policy,
    )

    prompt_fn(background_hint="visual bible says dense smoke and fog")

    assert captured["background_hint"] is None
    assert "smoke" in captured["scene_context"]
    assert captured["match_lyrics"] is False
    assert captured["creative_mode"] == "auto"
    assert captured["atmospherics_policy"]["allow_atmospherics"] is False


@pytest.mark.parametrize(
    ("creative_mode", "match_lyrics", "operator_prompt", "expected_lyrics"),
    [
        ("auto", False, None, ""),
        ("lyrics", True, None, "the real lyric sample"),
        ("prompt_improved", False, "an empty blue room", ""),
    ],
)
def test_only_lyrics_mode_exposes_lyrics_to_planner_under_v4(
    monkeypatch,
    tmp_path,
    creative_mode,
    match_lyrics,
    operator_prompt,
    expected_lyrics,
):
    captured = {}

    def fake_analyze(lyrics_text, artist, **kwargs):
        captured["lyrics_text"] = lyrics_text
        captured.update(kwargs)
        return {"style": "video", "prompt": f"unique {creative_mode} prompt"}

    monkeypatch.setattr(pipeline, "_analyze_lyrics_for_background", fake_analyze)
    monkeypatch.setattr(pipeline, "_USED_PROMPTS_FILE", str(tmp_path / "used.json"))
    policy = resolve_atmospherics_policy(operator_prompt, mode="enforce")

    pipeline._get_unique_prompt(
        lyrics_text="the real lyric sample",
        artist="Artist",
        song_title="Title",
        match_lyrics=match_lyrics,
        background_hint=operator_prompt,
        creative_mode=creative_mode,
        atmospherics_policy=policy,
    )

    assert captured["lyrics_text"] == expected_lyrics
    assert captured["creative_mode"] == creative_mode
    assert captured["background_hint"] == operator_prompt


def test_literal_mode_bypasses_planner_and_preserves_operator_text(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "_analyze_lyrics_for_background",
        lambda *args, **kwargs: pytest.fail("literal mode must not call Gemini planner"),
    )
    literal = "Locked camera on an empty cobalt room; sharp reflections."

    result = pipeline._get_unique_prompt(
        lyrics_text="unrelated lyrics",
        artist="Artist",
        song_title="Title",
        match_lyrics=False,
        background_hint=literal,
        bg_verbatim=True,
        creative_mode="prompt_literal",
        atmospherics_policy=resolve_atmospherics_policy(literal, mode="enforce"),
    )

    assert result["prompt"] == literal


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_and_shadow_preserve_legacy_match_lyrics_semantics(mode):
    policy = resolve_atmospherics_policy("", mode=mode)

    assert pipeline._planner_match_lyrics(False, "lyrics", policy) is False
    assert pipeline._planner_match_lyrics(
        False, "auto", policy, scene_planner=True
    ) is True


def test_enforce_uses_canonical_mode_for_lyrics_semantics():
    policy = resolve_atmospherics_policy("", mode="enforce")

    assert pipeline._planner_match_lyrics(True, "auto", policy) is False
    assert pipeline._planner_match_lyrics(False, "lyrics", policy) is True


def test_shadow_visual_bible_keeps_legacy_fallback_and_does_not_literal_shortcut(
    monkeypatch,
):
    monkeypatch.setattr(
        pipeline,
        "_get_genai_client",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    raw = "locked cobalt room"

    bible = pipeline._build_visual_bible(
        "lyrics still visible",
        "Artist",
        concept="legacy concept",
        background_hint=raw,
        bg_verbatim=True,
        creative_mode="prompt_literal",
        atmospherics_policy=resolve_atmospherics_policy(raw, mode="shadow"),
    )

    assert bible["world"] == "legacy concept"
