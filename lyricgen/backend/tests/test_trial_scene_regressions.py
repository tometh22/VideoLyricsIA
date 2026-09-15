"""Offline regressions for the bounded Universal trial findings.

Provider, storage, validation, stitching and clock waits are replaced with
local doubles. These tests never generate AI media or invoke ffmpeg.
"""
import ast
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pipeline
import scenes
from background_policy import resolve_atmospherics_policy


def _plan():
    sections = [
        scenes.Section("coro", 0, 18, recurrence_key="coro_1"),
        scenes.Section("puente", 18, 36, recurrence_key="puente"),
        scenes.Section("outro", 36, 56, recurrence_key="outro"),
    ]
    return {
        "bible": {"world": "red boat crossing the bay"},
        "sections": [s.to_dict() for s in sections],
        "audio_duration": 56,
        "scenes": [
            {"recurrence_key": s.recurrence_key, "section_type": s.type,
             "prompt": f"prompt {s.recurrence_key}", "cache_token": "original",
             "clip_cache_key": f"cache/veo/{s.recurrence_key}.mp4",
             "thumb_key": f"thumb/{s.recurrence_key}.jpg", "status": "generated"}
            for s in sections
        ],
    }


@pytest.fixture
def offline_scenes(monkeypatch):
    import veo_breaker

    monkeypatch.setattr(veo_breaker, "is_open", lambda: False)
    monkeypatch.setattr(pipeline, "_scene_concurrency", lambda: 1)
    monkeypatch.setattr(pipeline, "_scene_validation_observe_skip", lambda: True)
    monkeypatch.setattr(pipeline, "_compute_allow_people", lambda *args: False)
    monkeypatch.setattr(pipeline, "_persist_scene_thumb", lambda *args: None)
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(pipeline.storage, "upload_file", lambda path, key: key)
    monkeypatch.setattr(scenes, "stitch_timeline", lambda *args, **kwargs: "/tmp/timeline.mp4")
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    deleted = Mock()
    monkeypatch.setattr(pipeline.storage, "delete_object", deleted)
    return deleted


def _write_clip(prompt, path, **kwargs):
    Path(path).write_text(prompt)
    kwargs["out_meta"]["cache_object_key"] = (
        kwargs.get("cache_key_override") or "cache/veo/new-target.mp4"
    )
    return path


def _eight_key_sections():
    # Same recurrence topology as the incident, without customer lyrics.
    keys = ["intro", "coro_1", "puente", "coro_1", "puente", "coro_1",
            "puente", "coro_3", "coro_5", "coro_6", "puente", "coro_7", "outro"]
    return [scenes.Section(key.split("_")[0], i * 18, (i + 1) * 18,
                           recurrence_key=key, text=f"original line {i}")
            for i, key in enumerate(keys)]


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 12])
def test_scene_cap_includes_choruses_and_preserves_song(monkeypatch, limit):
    monkeypatch.setattr(scenes, "MAX_UNIQUE_SCENES", limit)
    sections = _eight_key_sections()
    before = copy.deepcopy(sections)
    result = scenes._cap_unique_scenes(sections)
    assert len({s.recurrence_key for s in result}) == min(limit, 6)
    assert [(s.start, s.end, s.text, s.type, s.energy) for s in result] == [
        (s.start, s.end, s.text, s.type, s.energy) for s in before]
    for key in {s.recurrence_key for s in before}:
        assert len({result[i].recurrence_key for i, s in enumerate(before)
                    if s.recurrence_key == key}) == 1
    assert result[0].recurrence_key == "intro"
    if limit >= 2:
        assert result[-1].recurrence_key == "outro"
    if limit >= 4:
        assert result[2].recurrence_key == "puente"
        assert all(s.recurrence_key.startswith("coro") for s in result if s.type == "coro")
    snapshot = copy.deepcopy(result)
    assert scenes._cap_unique_scenes(result) == snapshot


def test_uncapped_builder_makes_at_most_six_prompts_and_provider_calls(
    monkeypatch, tmp_path, offline_scenes,
):
    sections = _eight_key_sections()
    prompt = Mock(return_value={"prompt": "one shot"})
    plan = scenes.build_scene_plan(sections, {}, prompt)
    provider = Mock(side_effect=_write_clip)
    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    clips = pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")
    assert prompt.call_count == provider.call_count == len(clips) == 6
    assert len(plan["sections"]) == 13
    assert all(s.recurrence_key in clips for s in sections)
    assert plan["degraded"] == {"failed": 0, "total": 6}


@pytest.mark.parametrize("regen_keys", [None, {f"key{i}" for i in range(8)}])
def test_oversized_fresh_plan_is_rejected_before_provider(
    monkeypatch, tmp_path, offline_scenes, regen_keys,
):
    plan = {"scenes": [{"recurrence_key": f"key{i}"} for i in range(8)]}
    provider = Mock()
    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    with pytest.raises(ValueError, match="six unique"):
        pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S",
                                       regen_keys=regen_keys)
    provider.assert_not_called()


def test_historical_eight_scene_plan_can_regenerate_only_one_target(
    monkeypatch, tmp_path, offline_scenes,
):
    plan = {"scenes": [{"recurrence_key": f"key{i}", "prompt": f"shot{i}",
                        "clip_cache_key": f"cache/veo/old{i}.mp4"} for i in range(8)]}
    provider = Mock(side_effect=_write_clip)
    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    clips = pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S",
                                           regen_keys={"key3"})
    assert len(clips) == 8
    assert sum(not call.kwargs["cache_only"] for call in provider.call_args_list) == 1


@pytest.mark.parametrize("value,expected", [(None, 8), ("", 8), ("8", 8), ("4", 4)])
def test_veo_duration_request_is_explicit(monkeypatch, value, expected):
    # Execute the production request-parameter block with no provider/network.
    tree = ast.parse(inspect.getsource(pipeline._generate_veo_video))
    nodes = tree.body[0].body
    begin = next(i for i, n in enumerate(nodes) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "veo_params" for t in n.targets))
    end = next(i for i in range(begin + 1, len(nodes)) if isinstance(nodes[i], ast.Try))
    if value is None:
        monkeypatch.delenv("VEO_CLIP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("VEO_CLIP_SECONDS", value)
    import os
    context = {"os": os, "logger": Mock()}
    exec(compile(ast.Module(body=nodes[begin:end], type_ignores=[]), "veo_params", "exec"), context)
    assert context["veo_params"]["durationSeconds"] == expected
    assert context["veo_params"]["sampleCount"] == 1


def test_initial_settings_roundtrip_includes_all_selected_visual_controls():
    # Execute the actual persistence map in isolation, without rendering or
    # database setup; then perform the same JSON round trip as render_params.
    tree = ast.parse(inspect.getsource(pipeline.run_pipeline))
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_new_rp"
                              for t in n.targets))
    values = {name: parameter.default for name, parameter in
              inspect.signature(pipeline.run_pipeline).parameters.items()}
    selected = {
        "font": "anton", "font_scale": 1.15, "text_case": "original",
        "text_contrast": "strong", "lyric_color": "#AABBCC",
        "lyric_sung_color": "#123456", "frame_format": "cine",
        "lyrics_animation": "karaoke", "line_transition": "wipe",
        "movement_style": "estatico", "animate_image": True,
        "custom_colors": "#112233,#445566", "effect": "snow",
        "style": "custom", "genre": "rock", "concept": "mar",
        "match_lyrics": False, "title_template": "centered", "title_size": 1.25,
        "title_artist_font": "anton", "title_song_font": "oswald",
        "title_song_break": "A\nB",
    }
    values.update(selected)
    values.update(_bg_drift_meta={}, _background_is_ai_generated=True,
                  _normalize_movement_style=pipeline._normalize_movement_style)
    result = eval(compile(ast.Expression(assignment.value), "render_params", "eval"), values)
    restored = json.loads(json.dumps(result))
    assert {key: restored[key] for key in selected} == selected


def test_terminal_unavailable_retries_once_with_identical_inputs(monkeypatch):
    calls, waits = [], []

    def provider(*args, **kwargs):
        calls.append((args, kwargs.copy()))
        if len(calls) == 1:
            raise pipeline.VeoOperationFailed({"code": 14, "message": "Service unavailable"})
        return "clip"

    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    monkeypatch.setattr("time.sleep", waits.append)
    assert pipeline._generate_scene_video_with_retry(
        "literal prompt", "clip.mp4", job_id="job", cache_namespace="outro",
    ) == "clip"
    assert calls[0] == calls[1]
    assert waits == [3]


@pytest.mark.parametrize("error,cache_only,expected_calls", [
    (pipeline.VeoOperationFailed({"code": 14}), False, 2),
    (pipeline.VeoOperationFailed({"code": 14}), True, 1),
    (pipeline.VeoOperationFailed({"code": 3}), False, 1),
    (pipeline.VeoAmbiguousSubmission("ambiguous"), False, 1),
    (pipeline.VeoTrackingUnavailable("cancelled"), False, 1),
    (pipeline.VeoBudgetExceeded("limit"), False, 1),
    (TimeoutError("poll timed out"), False, 1),
    (RuntimeError("Service unavailable but no confirmed terminal operation"), False, 1),
])
def test_retry_bound_and_nonretryable_failures(monkeypatch, error, cache_only, expected_calls):
    provider = Mock(side_effect=error)
    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with pytest.raises(type(error)):
        pipeline._generate_scene_video_with_retry("prompt", "clip.mp4", cache_only=cache_only)
    assert provider.call_count == expected_calls


def test_failed_outro_stays_degraded_after_lyrics_edit(monkeypatch, tmp_path, offline_scenes):
    plan = _plan()
    plan["scenes"][-1].pop("clip_cache_key")
    calls = []

    def provider(prompt, path, **kwargs):
        calls.append((prompt, kwargs["cache_only"]))
        if "outro" in prompt:
            if kwargs["cache_only"]:
                raise RuntimeError("cache_only miss")
            raise pipeline.VeoOperationFailed({"code": 14, "message": "Service unavailable"})
        return _write_clip(prompt, path, **kwargs)

    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    clips = pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S")
    assert len([p for p, cached in calls if "outro" in p and not cached]) == 2
    assert clips["outro"] == clips["coro_1"]
    assert plan["degraded"] == {"failed": 1, "total": 3}
    plan = json.loads(json.dumps(plan))
    calls.clear()
    pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S", regen_keys=set())
    assert all(cached for _, cached in calls)
    assert plan["scenes"][-1]["validation"]["substituted_from"] == "coro_1"
    assert "Service unavailable" in plan["scenes"][-1]["error"]
    assert plan["degraded"]["failed"] == 1
    assert scenes.scene_plan_requires_review(plan)


@pytest.mark.parametrize("failure", ["provider", "stitch", "cache_upload"])
def test_failed_regen_preserves_original_plan_and_never_gcs(
    monkeypatch, tmp_path, offline_scenes, failure,
):
    plan = _plan()
    before = copy.deepcopy(plan)

    def provider(prompt, path, **kwargs):
        if failure == "provider" and not kwargs["cache_only"]:
            raise pipeline.VeoOperationFailed({"code": 14})
        return _write_clip(prompt, path, **kwargs)

    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    if failure == "stitch":
        monkeypatch.setattr(scenes, "stitch_timeline", Mock(side_effect=RuntimeError("stitch failed")))
    if failure == "cache_upload":
        monkeypatch.setattr(pipeline.storage, "upload_file", Mock(side_effect=RuntimeError("R2 unavailable")))
    with pytest.raises(RuntimeError):
        pipeline._regenerate_scene_background(
            plan, "puente", str(tmp_path), artist="A", song_title="S",
            audio_duration=56, job_id="job", prompt_override="new bridge",
        )
    assert plan == before
    offline_scenes.assert_not_called()


def test_successful_puente_only_regen_clears_stale_failure(monkeypatch, tmp_path, offline_scenes):
    plan = _plan()
    target = plan["scenes"][1]
    target.update(status="failed", degraded=True, error="old unavailable",
                  last_regeneration_error="previous reroll failed",
                  validation={"passed": True, "substituted_from": "coro_1"})
    plan["degraded"] = {"failed": 1, "total": 3}
    plan["review_required"] = True
    calls = []

    def provider(prompt, path, **kwargs):
        calls.append(kwargs.copy())
        return _write_clip(prompt, path, **kwargs)

    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    _, result = pipeline._regenerate_scene_background(
        plan, "puente", str(tmp_path), artist="A", song_title="S",
        audio_duration=56, job_id="job", prompt_override="new bridge",
    )
    assert sum(not c["cache_only"] for c in calls) == 1
    assert result["scenes"][1]["clip_cache_key"] == "cache/veo/new-target.mp4"
    assert result["scenes"][1]["status"] == "generated"
    assert not scenes.scene_plan_requires_review(result)
    for index in (0, 2):
        for field in ("prompt", "cache_token", "clip_cache_key", "thumb_key"):
            assert result["scenes"][index][field] == plan["scenes"][index][field]
    assert result["bible"] == plan["bible"]
    assert result["sections"] == plan["sections"]
    offline_scenes.assert_not_called()


def test_failed_reroll_notice_survives_cache_only_reuse(monkeypatch, tmp_path, offline_scenes):
    plan = _plan()
    original_key = plan["scenes"][1]["clip_cache_key"]
    scenes.mark_scene_regeneration_failed(plan, "puente", RuntimeError("Service unavailable"))
    monkeypatch.setattr(pipeline, "_generate_veo_video", _write_clip)
    pipeline._generate_scene_clips(plan, str(tmp_path), artist="A", song_title="S", regen_keys=set())
    assert plan["scenes"][1]["clip_cache_key"] == original_key
    assert plan["degraded"]["failed"] == 1
    assert scenes.scene_plan_requires_review(plan)


def test_partial_plan_is_persisted_even_when_generation_raises(monkeypatch, tmp_path):
    plan = _plan()
    monkeypatch.setattr(pipeline, "_mark_scene_planning_incomplete", lambda job_id: None)
    monkeypatch.setattr(scenes, "detect_sections", lambda *args: scenes.sections_from_plan(plan))
    monkeypatch.setattr(scenes, "build_scene_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(pipeline, "_build_visual_bible", lambda *args, **kwargs: plan["bible"])
    monkeypatch.setattr(pipeline, "_make_scene_prompt_fn", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "_generate_scene_clips", Mock(side_effect=RuntimeError("no compatible fallback")))
    updates = Mock()
    monkeypatch.setattr(pipeline, "update_job", updates)
    with pytest.raises(RuntimeError, match="no compatible fallback"):
        pipeline._generate_scene_background([], 56, str(tmp_path), style_hint="auto",
                                            lyrics_text="lyrics", artist="A", job_id="job")
    persisted = updates.call_args.kwargs["scene_plan"]
    assert persisted["review_required"] is True
    assert persisted["scenes"][0]["clip_cache_key"] == "cache/veo/coro_1.mp4"


def test_improved_plan_assigns_ordered_context_and_preserves_recurrence():
    sections = scenes.sections_from_plan(_plan())
    sections.insert(2, scenes.Section("coro", 30, 36, recurrence_key="coro_1"))
    prompts = []

    def prompt_fn(**kwargs):
        prompts.append(kwargs["background_hint"])
        return {"prompt": kwargs["background_hint"]}

    plan = scenes.build_scene_plan(sections, {}, prompt_fn, creative_mode="prompt_improved")
    assert len(prompts) == 3  # recurring chorus still generates only once
    assert "recurring visual motif" in prompts[0]
    assert "intervening action" in prompts[1]
    assert "closing beat / destination" in prompts[2]
    assert "do not replay the entire story" in prompts[2]
    assert all(s["narrative_context"] for s in plan["scenes"])


def test_literal_plan_keeps_full_operator_text_without_ai(monkeypatch):
    literal = "Barco rojo sale del puerto; cruza la bahía; llega al faro."
    monkeypatch.setattr(pipeline, "_get_genai_client", Mock(side_effect=AssertionError("no AI")))
    prompt_fn = pipeline._make_scene_prompt_fn(
        "lyrics", "A", "S", "", "", "auto", "", None, False,
        operator_prompt=literal, bg_verbatim=True, creative_mode="prompt_literal",
    )
    plan = scenes.build_scene_plan(scenes.sections_from_plan(_plan()), {}, prompt_fn,
                                   creative_mode="prompt_literal")
    assert [s["prompt"] for s in plan["scenes"]] == [literal] * 3
    assert all("narrative_context" not in s for s in plan["scenes"])


@pytest.mark.parametrize("policy_mode", ["off", "shadow", "enforce"])
def test_improved_context_reaches_existing_planner_in_each_policy_mode(monkeypatch, policy_mode):
    captured = []

    def generate_content(**kwargs):
        captured.append(kwargs["contents"])
        return SimpleNamespace(text='{"style":"video","prompt":"a red boat arriving at a lighthouse"}')

    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(pipeline, "_generate_content_with_quota_retry", lambda fn, **kwargs: fn())
    pipeline._analyze_lyrics_for_background(
        "lyrics", "A", song_title="S", background_hint="red boat crossing bay",
        creative_mode="prompt_improved", scene_context="closing beat / destination",
        atmospherics_policy=resolve_atmospherics_policy("red boat crossing bay", mode=policy_mode),
    )
    assert len(captured) == 1
    assert "closing beat / destination" in captured[0]
    assert "red boat crossing bay" in captured[0]


@pytest.mark.parametrize("existing", [None, {"scenes": []}, _plan()])
def test_planning_exception_preserves_failure_sentinel_and_old_caches(monkeypatch, tmp_path, existing):
    import database

    db = Mock()
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(pipeline, "get_job_model", lambda db, job_id: SimpleNamespace(scene_plan=existing))
    updates = Mock()
    monkeypatch.setattr(pipeline, "update_job", updates)
    monkeypatch.setattr(scenes, "detect_sections", Mock(side_effect=RuntimeError("planner unavailable")))
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(pipeline, "_generate_scene_clips", provider)
    with pytest.raises(RuntimeError, match="planner unavailable"):
        pipeline._generate_scene_background([], 56, str(tmp_path), style_hint="auto",
                                            lyrics_text="lyrics", artist="A", job_id="job")
    persisted = updates.call_args.kwargs["scene_plan"]
    assert scenes.scene_plan_requires_review(persisted)
    assert persisted["generation_error"]
    if existing:
        assert persisted["scenes"] == existing["scenes"]
    assert "generation_error" not in (existing or {})
    provider.assert_not_called()


def test_healthy_reused_status_alone_is_not_a_failure():
    plan = {"scenes": [{"status": "reused", "validation": {"passed": True}}]}
    scenes.refresh_scene_plan_review(plan)
    assert not scenes.scene_plan_requires_review(plan)
    plan["scenes"][0]["validation"]["substituted_from"] = "coro_1"
    assert scenes.scene_plan_requires_review(plan)


@pytest.mark.parametrize("editing,expected_status", [(False, "error"), (True, "pending_review")])
def test_closed_trial_worker_never_exposes_done_or_calls_provider(monkeypatch, editing, expected_status):
    from fastapi import HTTPException
    import trial_policy

    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "trial-group")
    guard = Mock(side_effect=HTTPException(403, detail={"code": "trial_expired", "message": "expired"}))
    monkeypatch.setattr(trial_policy, "require_job_reserved", guard)
    updates = Mock()
    monkeypatch.setattr(pipeline, "update_job", updates)
    provider = Mock(side_effect=AssertionError("provider must not be reached"))
    monkeypatch.setattr(pipeline, "_get_genai_client", provider)
    monkeypatch.setattr(pipeline, "_generate_veo_video", provider)
    if editing:
        pipeline.run_edit_pipeline("source-job", "scene", {"scene_key": "puente"})
    else:
        pipeline.run_pipeline("source-job", "unused.mp3", "A", "auto")
    guard.assert_called_once_with("source-job")
    assert updates.call_args.kwargs == {
        "status": expected_status, "current_step": "trial_closed", "error": "expired",
    }
    provider.assert_not_called()


def test_worker_guard_disabled_has_no_lookup_and_database_failure_is_not_bypassed(monkeypatch):
    import trial_policy

    guard = Mock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(trial_policy, "require_job_reserved", guard)
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    assert pipeline._trial_worker_admitted("job")
    guard.assert_not_called()
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "trial-group")
    with pytest.raises(RuntimeError, match="database unavailable"):
        pipeline._trial_worker_admitted("job")


@pytest.fixture
def admission_trial(db, monkeypatch, admin_user_id):
    import uuid
    from contextlib import nullcontext
    import database
    import trial_policy as policy

    group = "trial_admit_" + uuid.uuid4().hex[:12]
    job_id = uuid.uuid4().hex[:12]
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", group)
    owner = db.query(database.User).filter(database.User.id == admin_user_id).one()
    owner.billing_group = group
    db.add(database.Job(job_id=job_id, user_id=owner.id, tenant_id="default",
                        artist="A", style="auto", filename="trial.mp3", status="queued"))
    db.flush()
    identity = {"id": owner.id, "billing_group": group}
    policy.activate(db, group, owner.id)
    # Exercise real SQL ledger reads, sharing the fixture's uncommitted rows
    # without allowing the worker context manager to close its transaction.
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(db))
    return identity, job_id


def test_render_worker_requires_own_current_grant_reservation(db, admission_trial):
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    # The clock-only helper deliberately remains usable by callers that do
    # not require a render reservation.
    policy.require_job_open(job_id)
    policy.reserve(db, identity, "other-job", 3)
    with pytest.raises(HTTPException) as exc:
        policy.require_job_reserved(job_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "trial_job_not_reserved"
    policy.reserve(db, identity, job_id, 3)
    policy.reserve(db, identity, "third-job", 3)
    assert policy.require_job_reserved(job_id)["state"] == "exhausted"


def test_transcription_admission_is_separate_from_render_reservation(db, admission_trial):
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    policy.reserve(db, identity, job_id, 3)
    with pytest.raises(HTTPException) as exc:
        policy.require_transcription_admitted(job_id)
    assert exc.value.detail["code"] == "trial_transcription_unadmitted"
    policy.admit_transcription(db, identity, job_id)
    assert policy.require_transcription_admitted(job_id)["state"] == "active"


@pytest.mark.parametrize("transcription", [False, True])
def test_old_grant_and_wrong_job_rows_never_authorize_worker(db, admission_trial, transcription):
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from database import AuditLog, CreditGrant
    import trial_policy as policy

    identity, job_id = admission_trial
    current = policy.latest_grant(db, identity["billing_group"])
    old = CreditGrant(billing_group=identity["billing_group"], amount=9,
                      reason="prior_trial", granted_at=datetime.now(timezone.utc) - timedelta(days=2))
    db.add(old)
    db.flush()
    action = policy.TRANSCRIPTION_ACTION if transcription else policy.RESERVATION_ACTION
    for grant_id, row_job in ((old.id, job_id), (current.id, "another-job")):
        db.add(AuditLog(user_id=identity["id"], action=action, detail={
            "grant_id": grant_id, "billing_group": identity["billing_group"],
            "job_id": row_job, "credits": 3,
        }))
    db.flush()
    gate = policy.require_transcription_admitted if transcription else policy.require_job_reserved
    with pytest.raises(HTTPException) as exc:
        gate(job_id)
    assert exc.value.status_code == (403 if transcription else 409)


def test_shared_transcription_cap_counts_retries_and_new_jobs(db, admission_trial):
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    for target in (job_id, job_id, "second", "third", "fourth", "fifth"):
        policy.admit_transcription(db, identity, target)
    grant = policy.latest_grant(db, identity["billing_group"])
    rows = policy.transcription_attempts(db, grant)
    assert len(rows) == 6
    assert sum(row["job_id"] == job_id for row in rows) == 2
    with pytest.raises(HTTPException) as exc:
        policy.admit_transcription(db, identity, "seventh")
    assert exc.value.detail["code"] == "trial_transcription_limit"
    assert len(policy.transcription_attempts(db, grant)) == 6
    # Admission is not retroactively revoked by reaching the cap.
    policy.require_transcription_admitted(job_id)


def test_exhausted_trial_blocks_unreserved_transcription_but_allows_reserved_retry(db, admission_trial):
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    for target in (job_id, "second", "third"):
        policy.reserve(db, identity, target, 3)
    with pytest.raises(HTTPException) as exc:
        policy.admit_transcription(db, identity, "new-unreserved")
    assert exc.value.detail["code"] == "trial_credits_exhausted"
    policy.admit_transcription(db, identity, job_id)
    assert policy.require_transcription_admitted(job_id)["state"] == "exhausted"


def test_rolled_back_transcription_admission_neither_spends_nor_authorizes(db, admission_trial):
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    transaction = db.begin_nested()
    policy.admit_transcription(db, identity, job_id)
    policy.require_transcription_admitted(job_id)
    transaction.rollback()
    assert policy.transcription_attempts(db, policy.latest_grant(db, identity["billing_group"])) == []
    with pytest.raises(HTTPException) as exc:
        policy.require_transcription_admitted(job_id)
    assert exc.value.detail["code"] == "trial_transcription_unadmitted"


@pytest.mark.parametrize("gate_name", ["require_job_reserved", "require_transcription_admitted"])
def test_worker_admission_checks_expiry_even_when_recorded(db, admission_trial, gate_name):
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    import trial_policy as policy

    identity, job_id = admission_trial
    policy.reserve(db, identity, job_id, 3)
    policy.admit_transcription(db, identity, job_id)
    grant = policy.latest_grant(db, identity["billing_group"])
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        getattr(policy, gate_name)(job_id)
    assert exc.value.detail["code"] == "trial_expired"


def test_transcription_admission_lock_precedes_reads_and_append(monkeypatch):
    import trial_policy as policy

    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "trial-group")
    events = []
    db = Mock()
    db.add.side_effect = lambda row: events.append("append")
    db.flush.side_effect = lambda: events.append("flush")
    monkeypatch.setattr(policy, "lock_budget", lambda *args: events.append("lock"))
    monkeypatch.setattr(policy, "require_open", lambda *args: (events.append("clock") or {"state": "active"}))
    monkeypatch.setattr(policy, "latest_grant", lambda *args: SimpleNamespace(id=1))
    monkeypatch.setattr(policy, "transcription_attempts", lambda *args: (events.append("count") or []))
    policy.admit_transcription(db, {"id": 1, "billing_group": "trial-group"}, "job")
    assert events == ["lock", "clock", "count", "append", "flush"]
    db.commit.assert_not_called()  # publication and commit belong to caller


def test_admission_helpers_disabled_skip_database_and_propagate_database_errors(monkeypatch):
    import database
    import trial_policy as policy

    session = Mock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(database, "SessionLocal", session)
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    policy.require_job_reserved("job")
    policy.require_transcription_admitted("job")
    db = Mock()
    policy.admit_transcription(db, {"billing_group": "ordinary"}, "job")
    assert not db.mock_calls
    session.assert_not_called()
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "trial-group")
    for gate in (policy.require_job_reserved, policy.require_transcription_admitted):
        with pytest.raises(RuntimeError, match="database unavailable"):
            gate("job")


@pytest.mark.parametrize("params,plan", [({"enable_scenes": True}, None), ({}, {"scenes": []})])
def test_saved_scenes_intent_requires_three_reserved_credits(db, admission_trial, monkeypatch, params, plan):
    from fastapi import HTTPException
    from database import Job
    import trial_policy as policy

    identity, job_id = admission_trial
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.render_params, job.scene_plan = params, plan
    db.flush()
    policy.reserve(db, identity, job_id, 1)
    with pytest.raises(HTTPException) as exc:
        policy.require_job_reserved(job_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "trial_job_not_reserved"
    policy.reserve(db, identity, job_id, 3)
    policy.require_job_reserved(job_id)
