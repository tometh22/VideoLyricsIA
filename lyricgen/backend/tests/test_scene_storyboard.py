import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pipeline
import scenes
from background_policy import resolve_atmospherics_policy
from scene_storyboard import sequence_sections, storyboard_request, validate_shots


def sections():
    return [scenes.Section("coro", i * 18, (i + 1) * 18,
                           recurrence_key=f"coro_{i % 6}", text=f"line {i}") for i in range(13)]


def plan():
    return scenes.build_scene_plan(sequence_sections(sections()), {"world": "golden ocean"},
                                   lambda **kw: {"prompt": "placeholder"},
                                   creative_mode="prompt_improved")


def response_for(p):
    return {"shots": [{"id": s["recurrence_key"],
                        "prompt": f"A unique cinematic golden ocean shot number {i}, calm clear daylight."}
                       for i, s in enumerate(p["scenes"])]}


def test_story_never_rewinds_at_repeated_chorus_and_preserves_all_boundaries():
    old = sections()
    seq = sequence_sections(copy.deepcopy(old))
    assert len({s.recurrence_key for s in seq}) == 6
    indices = [int(s.recurrence_key.split("_")[1]) for s in seq]
    assert indices == sorted(indices)
    assert indices[0] == 1 and indices[-1] == 6
    assert [(s.start, s.end, s.text, s.type) for s in seq] == [
        (s.start, s.end, s.text, s.type) for s in old]
    assert sequence_sections(copy.deepcopy(seq)) == seq


@pytest.mark.parametrize("count", [1, 2, 6, 12])
def test_sequence_has_hard_six_cap_and_nonempty_groups(count):
    result = sequence_sections(sections(), count)
    assert len({s.recurrence_key for s in result}) == min(count, 6)


@pytest.mark.parametrize("bad", [None, {}, {"shots": []}])
def test_incomplete_storyboard_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_shots(bad, ["story_1", "story_2"])


@pytest.mark.parametrize("defect", ["duplicate", "order", "empty", "extra", "memory"])
def test_invalid_storyboard_is_rejected(defect):
    p = plan()
    raw = response_for(p)
    if defect == "duplicate":
        raw["shots"][1]["prompt"] = raw["shots"][0]["prompt"]
    elif defect == "order":
        raw["shots"].reverse()
    elif defect == "empty":
        raw["shots"][0]["prompt"] = ""
    elif defect == "memory":
        raw["shots"][0]["prompt"] += " Continue from the previous scene."
    else:
        raw["shots"].append(raw["shots"][0])
    with pytest.raises(ValueError):
        validate_shots(raw, [s["recurrence_key"] for s in p["scenes"]])


def test_full_story_and_all_camera_modes_reach_one_planner_without_people_permission():
    p = plan()
    story = "Opening boat. " + "long description " * 60 + "Closing birds."
    system, user = storyboard_request(p, story, False, 8)
    data = json.loads(user)
    assert data["operator_story"] == story
    assert len(data["shots"]) == 6 and data["clip_duration_seconds"] == 8
    assert "NO people" in system and "never squeeze" in system
    assert [s["camera_mode"] for s in data["shots"]] == [s["movement_style"] for s in p["scenes"]]


def test_one_model_call_produces_six_distinct_persisted_prompts(monkeypatch):
    p = plan()
    call = Mock(return_value=SimpleNamespace(text=json.dumps(response_for(p))))
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: SimpleNamespace(
        models=SimpleNamespace(generate_content=call)))
    monkeypatch.setattr(pipeline, "_call_with_timeout", lambda fn, **kw: fn())
    result = pipeline._apply_operator_storyboard(p, "The complete story", atmospherics_policy=
                                                resolve_atmospherics_policy("The complete story"))
    assert call.call_count == 1
    assert result["narrative_sequence"] and result["storyboard_version"] == 1
    assert len({s["prompt"] for s in result["scenes"]}) == 6


def test_invalid_joint_plan_stops_before_veo(monkeypatch, tmp_path):
    monkeypatch.setattr(scenes, "detect_sections", lambda *a: sections())
    monkeypatch.setattr(pipeline, "_build_visual_bible", lambda *a, **kw: {})
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(text='{}'))))
    monkeypatch.setattr(pipeline, "_call_with_timeout", lambda fn, **kw: fn())
    provider = Mock()
    monkeypatch.setattr(pipeline, "_generate_scene_clips", provider)
    with pytest.raises(ValueError, match="Incomplete narrative"):
        pipeline._generate_scene_background([], 234, str(tmp_path), style_hint="auto",
                                            lyrics_text="lyrics", artist="A", match_lyrics=False,
                                            background_hint="boat crossing the ocean", bg_verbatim=False)
    provider.assert_not_called()


@pytest.mark.parametrize("subject", ["weathered hands holding an ember", "a golden pregnant form"])
def test_planner_cannot_override_people_policy_or_partially_mutate_plan(monkeypatch, subject):
    p = plan()
    before = copy.deepcopy(p)
    raw = response_for(p)
    raw["shots"][3]["prompt"] = "A cinematic close-up of " + subject + " in warm soft light."
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(text=json.dumps(raw)))))
    monkeypatch.setattr(pipeline, "_call_with_timeout", lambda fn, **kw: fn())
    with pytest.raises(ValueError, match="no-people policy"):
        pipeline._apply_operator_storyboard(p, "original story", atmospherics_policy=
                                            resolve_atmospherics_policy("original story"))
    assert p == before


def test_narrative_lyric_edit_keeps_order_and_uses_cache_only(monkeypatch, tmp_path):
    p = plan()
    p["narrative_sequence"] = True
    monkeypatch.setattr(scenes, "detect_sections", lambda *a: sections())
    generate = Mock(return_value={s["recurrence_key"]: "cached.mp4" for s in p["scenes"]})
    monkeypatch.setattr(pipeline, "_generate_scene_clips", generate)
    monkeypatch.setattr(scenes, "stitch_timeline", lambda *a, **kw: "timeline.mp4")
    path, result = pipeline._restitch_scenes_for_edit(p, [], 234, str(tmp_path), artist="A", song_title="S")
    assert path == "timeline.mp4"
    assert generate.call_args.kwargs["regen_keys"] == set()
    assert [s["recurrence_key"] for s in result["sections"]] == [s["recurrence_key"] for s in p["sections"]]
