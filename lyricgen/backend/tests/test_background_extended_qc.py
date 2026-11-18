"""Extended background QC: weapons, gore, body parts, overlays and letterbox.

Built from the 20 UMG change requests about the background (sep-nov 2026).
The invariants that matter most are the ones that kept production safe before:
the base Vision request is untouched, the extended call is fail-open (a 429 or
a garbled answer is "not evaluated", never an inconclusive verdict), and
nothing new rejects an asset unless BACKGROUND_EXTENDED_QC_MODE=enforce.
"""
import json
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

import background_technical_qc as tqc
import content_validator as cv
import delivery_background_qc as dbq

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
BASE = {"people": False, "atmospherics": False, "brand": False, "text": False, "anatomy": False}
CLEAN_EXT = {"weapon": False, "gore": False, "body_parts": False, "overlay": False}


# ── Flags ──────────────────────────────────────────────────────────────────
def test_mode_defaults_to_observe_and_fails_safe(monkeypatch):
    monkeypatch.delenv(cv.EXTENDED_QC_MODE_ENV, raising=False)
    assert cv.extended_qc_mode() == "observe"
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "blokc")
    assert cv.extended_qc_mode() == "observe"
    assert cv.extended_enforced_categories() == frozenset()


def test_enforce_defaults_to_measured_categories(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")
    monkeypatch.delenv(cv.EXTENDED_QC_ENFORCE_ENV, raising=False)
    assert cv.extended_enforced_categories() == frozenset(
        {"weapon", "gore", "overlay", "letterbox"})
    monkeypatch.setenv(cv.EXTENDED_QC_ENFORCE_ENV, "weapon, bogus ,body_parts")
    assert cv.extended_enforced_categories() == frozenset({"weapon", "body_parts"})


def test_extended_prompt_is_separate_from_the_gating_prompt():
    import inspect
    base_source = inspect.getsource(cv._check_frame_with_gemini)
    assert "weapon" not in base_source and "body_parts" not in base_source
    for field in ("weapon", "gore", "body_parts", "overlay"):
        assert f"detections.{field}=true" in cv.EXTENDED_PROMPT
    assert "16:9" in cv.EXTENDED_PROMPT


# ── The extended Vision call is fail-open ──────────────────────────────────
def _fake_vertex(monkeypatch, answer):
    import sys
    import types

    calls = []

    class _Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(text=answer if isinstance(answer, str) else json.dumps(answer))

    stub = types.ModuleType("pipeline")
    stub._get_genai_client = lambda: SimpleNamespace(models=_Models())
    stub._call_with_timeout = lambda fn, timeout_s=None, label=None: fn()
    monkeypatch.setitem(sys.modules, "pipeline", stub)
    monkeypatch.setattr(cv.time, "sleep", lambda _s: None)
    return calls


def _frame(tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"\xff\xd8fake")
    return str(path)


def test_extended_answer_is_parsed_and_reason_promotes(tmp_path, monkeypatch):
    calls = _fake_vertex(monkeypatch, {
        "detections": dict(CLEAN_EXT),
        "issues": [{"category": "weapon", "reason": "a pistol on the bench"}],
    })
    answer = cv._check_frame_extended(_frame(tmp_path))
    assert answer["detections"]["weapon"] is True
    assert set(answer["evaluated"]) == set(CLEAN_EXT)
    assert calls[0]["contents"][1] == cv.EXTENDED_PROMPT


@pytest.mark.parametrize("answer", [
    RuntimeError("429 RESOURCE_EXHAUSTED"),
    RuntimeError("permission denied"),
    "not json at all",
    {"detections": "nope"},
])
def test_extended_failures_are_not_evaluated(tmp_path, monkeypatch, answer):
    calls = _fake_vertex(monkeypatch, answer)
    assert cv._check_frame_extended(_frame(tmp_path)) is None
    # 429 gets exactly one retry (shared quota); other errors none.
    expected = 2 if isinstance(answer, Exception) and "429" in str(answer) else 1
    assert len(calls) == expected


def test_partial_answer_keeps_what_was_evaluated(tmp_path, monkeypatch):
    _fake_vertex(monkeypatch, {"detections": {"gore": True}, "issues": []})
    answer = cv._check_frame_extended(_frame(tmp_path))
    assert answer["evaluated"] == ["gore"]
    assert answer["detections"] == {"gore": True}


# ── validate_video wiring ──────────────────────────────────────────────────
def _fake_frames(monkeypatch, count=2):
    tmp_dir = tempfile.mkdtemp(prefix="genly_ext_test_")
    frames = []
    for i in range(count):
        path = os.path.join(tmp_dir, f"f{i}.jpg")
        open(path, "wb").close()
        frames.append(path)
    monkeypatch.setattr(cv, "_extract_frames", lambda _p: (frames, tmp_dir, count))
    return frames, tmp_dir


def _base_answer(**detections):
    return lambda _p: {"detections": {**BASE, **detections}, "issues": []}


def _no_letterbox(monkeypatch):
    monkeypatch.setattr("background_technical_qc.analyze_background",
                        lambda *_a, **_k: {"ran": True, "abstained": None, "findings": [], "metrics": {}})


def _letterbox_analysis(*_args, **_kwargs):
    return {"ran": True, "abstained": None, "metrics": {}, "findings": [{
        "category": "letterbox", "code": "BG_LETTERBOX",
        "reason": "Bandas negras fijas en top, bottom", "evidence": {},
    }]}


def test_off_mode_leaves_the_result_untouched(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "off")
    _fake_frames(monkeypatch, count=1)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: pytest.fail("extended call in off mode"))
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is True
    assert "extended_qc" not in result and "hard_violation" not in result
    assert set(result["detections"]) == set(BASE)


def test_observed_weapon_is_recorded_but_never_rejects(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "observe")
    _no_letterbox(monkeypatch)
    frames, tmp_dir = _fake_frames(monkeypatch, count=20)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    seen = []

    def _ext(path):
        seen.append(path)
        return {"detections": {**CLEAN_EXT, "weapon": True},
                "issues": [{"category": "weapon", "reason": "un revólver"}],
                "evaluated": list(CLEAN_EXT)}

    monkeypatch.setattr(cv, "_check_frame_extended", _ext)
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is True and result["hard_violation"] is False
    assert any(row["type"] == "weapon: un revólver" for row in result["observations"])
    assert result["detections"]["weapon"] is True
    # Capped and spread over the clip, endpoints included; frames still on
    # disk while the extended call ran, cleaned afterwards.
    assert len(seen) == 8 and seen[0] == frames[0] and seen[-1] == frames[-1]
    assert not os.path.exists(tmp_dir)


def test_enforced_weapon_rejects_even_under_global_observe(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")
    monkeypatch.setenv("BACKGROUND_VALIDATION_ENFORCEMENT", "observe")
    _no_letterbox(monkeypatch)
    _fake_frames(monkeypatch, count=4)
    # Base sees a brand (observed, would be waived) — Amor Suicida's shape.
    monkeypatch.setattr(cv, "_check_frame_with_gemini", lambda _p: {
        "detections": {**BASE, "brand": True},
        "issues": [{"category": "brand", "reason": "Bols Gin"}],
    })
    calls = []

    def _ext(path):
        calls.append(path)
        return {"detections": {**CLEAN_EXT, "weapon": True},
                "issues": [{"category": "weapon", "reason": "pistola en el banco"}],
                "evaluated": list(CLEAN_EXT)}

    monkeypatch.setattr(cv, "_check_frame_extended", _ext)
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is False
    assert result["inconclusive"] is False
    assert result["hard_violation"] is True
    assert any(row["type"].startswith("weapon:") for row in result["issues"])
    assert len(calls) == 1  # decisive: stops at the first enforced finding

    import pipeline
    assert pipeline._has_anatomy_violation(result) is True
    assert pipeline._mark_validation_observed(result)["passed"] is False


def test_enforced_body_parts_ignored_when_people_allowed(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")
    monkeypatch.setenv(cv.EXTENDED_QC_ENFORCE_ENV, "body_parts")
    _no_letterbox(monkeypatch)
    _fake_frames(monkeypatch, count=1)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: {
        "detections": {**CLEAN_EXT, "body_parts": True}, "issues": [], "evaluated": list(CLEAN_EXT)})
    result = cv.validate_video("bg.mp4", allow_people=True)
    assert result["passed"] is True and result["observations"] == []


def test_enforced_letterbox_rejects_before_spending_vision(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")
    monkeypatch.setattr("background_technical_qc.analyze_background", _letterbox_analysis)
    monkeypatch.setattr(cv, "_extract_frames", lambda _p: pytest.fail("frames extracted"))
    monkeypatch.setattr(cv, "_check_frame_with_gemini", lambda _p: pytest.fail("base Vision called"))
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is False
    assert result["inconclusive"] is False
    assert result["hard_violation"] is True
    assert result["issues"][0]["type"].startswith("letterbox:")


def test_quota_errors_keep_the_base_verdict(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")
    _no_letterbox(monkeypatch)
    _fake_frames(monkeypatch, count=3)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: None)
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is True
    assert result["inconclusive"] is False
    assert result["extended_qc"]["frames_unevaluated"] == 3


def test_inconclusive_base_skips_the_extended_call(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "observe")
    _no_letterbox(monkeypatch)
    _fake_frames(monkeypatch, count=2)

    def _quota(_p):
        raise cv.ValidatorCheckError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(cv, "_check_frame_with_gemini", _quota)
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: pytest.fail("extended on inconclusive"))
    result = cv.validate_video("bg.mp4")
    assert result["inconclusive"] is True
    assert result["hard_violation"] is False
    assert result["extended_qc"]["skipped"] == "base_verdict_rejects_asset"


def test_technical_scan_crash_is_an_abstention(monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "enforce")

    def _boom(*_a, **_k):
        raise RuntimeError("ffmpeg vanished")

    monkeypatch.setattr("background_technical_qc.analyze_background", _boom)
    _fake_frames(monkeypatch, count=1)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: {
        "detections": dict(CLEAN_EXT), "issues": [], "evaluated": list(CLEAN_EXT)})
    result = cv.validate_video("bg.mp4")
    assert result["passed"] is True
    assert result["extended_qc"]["technical"]["abstained"] == "error:RuntimeError"


def test_validate_image_runs_the_extended_call(tmp_path, monkeypatch):
    monkeypatch.setenv(cv.EXTENDED_QC_MODE_ENV, "observe")
    _no_letterbox(monkeypatch)
    monkeypatch.setattr(cv, "_check_frame_with_gemini", _base_answer())
    monkeypatch.setattr(cv, "_check_frame_extended", lambda _p: {
        "detections": {**CLEAN_EXT, "gore": True},
        "issues": [{"category": "gore", "reason": "carne cruda"}], "evaluated": list(CLEAN_EXT)})
    result = cv.validate_image(_frame(tmp_path))
    assert result["passed"] is True
    assert result["observations"] == [{"frame": 0, "type": "gore: carne cruda"}]


def test_scene_plan_with_hard_violation_is_decisive():
    import pipeline
    plan = {"scenes": [{"validation": {"passed": False, "hard_violation": True, "detections": {}}}]}
    assert pipeline._scene_plan_failure_is_decisive(plan) is True


# ── Deterministic letterbox detector on real encodes ───────────────────────
def _encode(tmp_path, name, vf):
    out = str(tmp_path / name)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=640x360:rate=10:duration=3", "-vf", vf,
         "-pix_fmt", "yuv420p", out],
        check=True, capture_output=True,
    )
    return out


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_letterboxed_background_is_flagged(tmp_path):
    # 2.39:1 picture inside a 16:9 frame — the IKV "borde superior e inferior".
    path = _encode(tmp_path, "lb.mp4", "scale=640:268,pad=640:360:0:46:black")
    result = tqc.analyze_background(path)
    assert result["ran"] is True
    assert [f["code"] for f in result["findings"]] == ["BG_LETTERBOX"]
    assert result["findings"][0]["evidence"]["flagged_sides"] == ["top", "bottom"]
    assert result["findings"][0]["evidence"]["effective_aspect"] == pytest.approx(2.39, abs=0.08)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_full_frame_background_is_clean(tmp_path):
    result = tqc.analyze_background(_encode(tmp_path, "full.mp4", "null"))
    assert result["ran"] is True
    assert result["findings"] == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_thin_dark_band_on_one_side_is_not_letterbox(tmp_path):
    # A dark table front at the bottom of a still (accepted job f517564b42d8).
    path = _encode(tmp_path, "floor.mp4", "drawbox=x=0:y=346:w=640:h=14:color=black:t=fill")
    assert tqc.analyze_background(path)["findings"] == []


def test_missing_asset_and_broken_probe_abstain(tmp_path):
    assert tqc.analyze_background(str(tmp_path / "nope.mp4"))["abstained"] == "asset_missing"
    asset = tmp_path / "x.mp4"
    asset.write_bytes(b"x")

    def _fail(*_a, **_k):
        raise subprocess.CalledProcessError(1, "ffprobe")

    result = tqc.analyze_background(str(asset), runner=_fail)
    assert result["ran"] is False
    assert result["findings"] == []
    assert result["abstained"].startswith("probe_failed")


# ── Delivery QC: what the operator sees ────────────────────────────────────
GUN_VALIDATION = {
    "passed": True, "enforcement": "observe", "observed_violation": True,
    "observed_issues": [{"frame": 1, "type": "brand: Detected prohibited brand"}],
    "observations": [
        {"frame": 1, "type": "weapon: Un revólver sobre la mesa"},
        {"frame": 2, "type": "weapon: Un revólver sobre la mesa"},
        {"frame": -1, "type": "letterbox: Bandas negras fijas en top, bottom"},
        {"frame": 3, "type": "text: SALE"},
        {"frame": 3, "type": "atmospherics: fog"},
    ],
    "extended_qc": {"mode": "observe"},
    "rejected_attempts": [{"observations": [{"type": "gore: sangre"}]}],
}


def test_findings_come_from_the_current_background_only():
    collected = dbq.collect_background_findings(GUN_VALIDATION)
    assert collected["extended_evaluated"] is True
    assert collected["findings"] == {
        "weapon": ["Un revólver sobre la mesa"],
        "letterbox": ["Bandas negras fijas en top, bottom"],
        "brand": ["Detected prohibited brand"],
    }


def test_scene_clip_findings_are_collected():
    scene_plan = {"scenes": [{"validation": {
        "passed": True, "hard_violation": False,
        "observations": [{"frame": 0, "type": "body_parts: piernas en primer plano"}],
    }}]}
    collected = dbq.collect_background_findings({"passed": True}, scene_plan)
    assert collected["findings"] == {"body_parts": ["piernas en primer plano"]}


def _no_scan(monkeypatch):
    monkeypatch.setattr("background_technical_qc.analyze_background",
                        lambda *_a, **_k: {"ran": True, "abstained": None, "findings": [], "metrics": {}})


def test_observe_issues_are_review_and_never_block(monkeypatch):
    monkeypatch.delenv(dbq.MODE_ENV, raising=False)
    _no_scan(monkeypatch)
    job = SimpleNamespace(validation_result=GUN_VALIDATION, scene_plan=None)
    issues, meta = dbq.build_background_issues(job, "render.mp4")
    assert [row["code"] for row in issues] == ["BG_WEAPON", "BG_BRAND", "BG_LETTERBOX"]
    assert all(row["result_status"] == "REVIEW" and row["blocking"] is False for row in issues)
    assert meta["validator_extended"] is True


def test_enforce_requires_acknowledgement_but_is_not_a_fail(monkeypatch):
    monkeypatch.setenv(dbq.MODE_ENV, "enforce")
    _no_scan(monkeypatch)
    issues, _meta = dbq.build_background_issues(
        SimpleNamespace(validation_result=GUN_VALIDATION, scene_plan=None), "render.mp4")
    assert all(row["blocking"] is True and row["result_status"] == "REVIEW" for row in issues)


def test_off_mode_adds_nothing(monkeypatch):
    monkeypatch.setenv(dbq.MODE_ENV, "off")
    issues, meta = dbq.build_background_issues(
        SimpleNamespace(validation_result=GUN_VALIDATION, scene_plan=None), "render.mp4")
    assert issues == [] and meta["mode"] == "off"


def test_render_letterbox_scan_becomes_media_issue(monkeypatch):
    monkeypatch.delenv(dbq.MODE_ENV, raising=False)
    monkeypatch.setattr("background_technical_qc.analyze_background", _letterbox_analysis)
    issues, meta = dbq.build_background_issues(
        SimpleNamespace(validation_result=None, scene_plan=None), "render.mp4")
    assert [row["code"] for row in issues] == ["MEDIA_LETTERBOX"]
    assert meta["render_scan"]["ran"] is True


def _report(monkeypatch, tmp_path, validation_result):
    from delivery_qc_runtime import build_runtime_report

    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    monkeypatch.setattr("delivery_qc_runtime.inspect_delivery_media", lambda *_a, **_k: {
        "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 1},
        "issues": [], "abstentions": [],
    })
    monkeypatch.setattr("delivery_qc_runtime.inspect_rendered_text", lambda *_a, **_k: {
        "observations": [], "issues": [], "abstentions": [],
    })
    _no_scan(monkeypatch)
    job = SimpleNamespace(
        artist="2 Minutos", song_title="Amor Suicida", filename="tema.wav",
        umg_spec=None, segments_revision=1, edit_count=0, transcription_quality={},
        validation_result=validation_result, scene_plan=None,
    )
    return build_runtime_report(job=job, video_path=str(asset), segments=[])


def test_runtime_report_shows_background_findings_before_publish(monkeypatch, tmp_path):
    monkeypatch.delenv(dbq.MODE_ENV, raising=False)
    report = _report(monkeypatch, tmp_path, GUN_VALIDATION)
    codes = {row["code"] for row in report["issues"]}
    assert {"BG_WEAPON", "BG_BRAND", "BG_LETTERBOX"} <= codes
    checks = {row["check_id"]: row for row in report["checks"]}
    assert checks["background_content"]["status"] == "REVIEW"
    assert checks["background_letterbox"]["status"] == "REVIEW"
    assert report["approval"]["blocked"] is False


def test_runtime_report_marks_legacy_validation_not_run(monkeypatch, tmp_path):
    monkeypatch.delenv(dbq.MODE_ENV, raising=False)
    report = _report(monkeypatch, tmp_path, {"passed": True, "issues": []})
    checks = {row["check_id"]: row for row in report["checks"]}
    assert checks["background_content"]["status"] == "NOT_RUN"
    assert checks["background_letterbox"]["status"] == "PASS"


def test_runtime_report_without_background_checks_when_off(monkeypatch, tmp_path):
    monkeypatch.setenv(dbq.MODE_ENV, "off")
    report = _report(monkeypatch, tmp_path, GUN_VALIDATION)
    ids = {row["check_id"] for row in report["checks"]}
    assert not ids & {"background_content", "background_letterbox"}
    assert not any(row["code"].startswith("BG_") for row in report["issues"])
