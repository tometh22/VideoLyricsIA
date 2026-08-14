"""Pin the benchmark scorer math so future edits to score_benchmark.py can't
silently change what "better" means — this is the safety net the heuristic
ablation work depends on.

Uses synthetic nonsense fixtures (no copyrighted lyrics, no audio, no network).
The WER test skips when jiwer isn't installed; the matching/recall tests are
pure-python and always run.
"""
import importlib.util
from pathlib import Path

import pytest

_SB_PATH = Path(__file__).resolve().parent.parent / "scripts" / "score_benchmark.py"
_spec = importlib.util.spec_from_file_location("score_benchmark", _SB_PATH)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)


def seg(start, text, end=None):
    return {"start": start, "end": end if end is not None else start + 2, "text": text}


# ── _aoo: greedy one-to-one onset match ────────────────────────────────────────

def test_aoo_one_to_one_avoids_repeated_chorus_inflation():
    """The whole reason for the rewrite: a chorus that repeats must NOT make
    every occurrence match the first GT occurrence (which inflated p95 to ~77s
    on 'No Hay Santos')."""
    ground = [
        seg(10, "chorus alpha beta"),
        seg(50, "verse gamma delta"),
        seg(100, "chorus alpha beta"),
    ]
    output = [
        seg(10.5, "chorus alpha beta"),
        seg(100.4, "chorus alpha beta"),
    ]
    mean_off, p95, matched = sb._aoo(ground, output)
    assert matched == 2                 # each output claims a DIFFERENT GT line
    assert mean_off < 1.0               # ~0.45s, not ~45s
    assert p95 < 1.0


def test_aoo_extra_repeats_do_not_match_beyond_ground_truth():
    ground = [seg(10, "chorus alpha beta")]
    output = [seg(10.2, "chorus alpha beta"), seg(40, "chorus alpha beta")]
    _, _, matched = sb._aoo(ground, output)
    assert matched == 1                 # only one GT line to claim

def test_aoo_no_text_match_is_unmatched():
    ground = [seg(10, "alpha beta gamma")]
    output = [seg(10, "totally different words here")]
    assert sb._aoo(ground, output) == (2.0, 2.0, 0)


def test_live_cohort_cannot_be_hidden_by_studio_improvement():
    healthy = {
        "ground_segments": 10,
        "baseline": {"wer": 0.2, "aoo_mean_s": 0.5, "recall": 0.9},
        "improvement": {
            "wer": 0.1, "aoo_mean_s": 0.3, "recall": 0.9, "matched": 9,
        },
    }
    regressed_live = {
        "ground_segments": 10,
        "baseline": {"wer": 0.2, "aoo_mean_s": 0.5, "recall": 0.9},
        "improvement": {
            "wer": 0.8, "aoo_mean_s": 1.5, "recall": 0.5, "matched": 5,
        },
    }
    assert sb._cohort_no_regression([healthy]) is True
    assert sb._cohort_no_regression([regressed_live]) is False


# ── _recall: fraction of GT lines found ────────────────────────────────────────

def test_recall_partial():
    ground = [seg(0, "alpha beta gamma"), seg(5, "delta epsilon zeta"), seg(10, "eta theta iota")]
    output = [seg(0, "alpha beta gamma"), seg(5, "delta epsilon zeta")]
    assert sb._recall(ground, output) == pytest.approx(2 / 3)


def test_recall_collapse_detects_giant_single_segment():
    """One giant segment with everything concatenated must score LOW recall —
    this is the collapse failure mode the pipeline fallback now guards against."""
    ground = [seg(0, "alpha beta gamma"), seg(5, "delta epsilon zeta"), seg(10, "eta theta iota")]
    giant = [seg(0, "alpha beta gamma delta epsilon zeta eta theta iota", end=12)]
    assert sb._recall(ground, giant) < 0.5


def test_recall_empty_ground_is_zero():
    assert sb._recall([], [seg(0, "anything")]) == 0.0


# ── _composite ─────────────────────────────────────────────────────────────────

def test_composite_perfect_and_worst():
    assert sb._composite(0.0, 0.0) == pytest.approx(1.0)
    assert sb._composite(1.0, 10.0) == pytest.approx(0.0)
    # AOO normalizes at 2s; 1.0s -> 0.25 penalty
    assert sb._composite(0.0, 1.0) == pytest.approx(0.75)


def test_operator_percentiles_pin_release_targets():
    minutes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 20]
    assert sb._percentile(minutes, 0.50) == 5.5
    assert sb._percentile(minutes, 0.90) == 9
    assert sb._percentile([1, 2, 3, 4, 5, 100], 0.50) == 3.5
    assert sb._percentile([1, 2, 3, 4, 5, 100], 0.90) == 100


def test_report_exposes_operational_gate():
    rows = [
        {"job_id": f"job-{i}", "source": "test", "is_live": i < 10,
         "operator_review_minutes": 4 if i < 25 else 9,
         "operator_time_source": "active_edit_ms",
         "operator_pipeline_release": "test-release",
         "baseline": {"wer": 0.2, "aoo_mean_s": 0.4, "composite": 0.8}}
        for i in range(30)
    ]
    report = sb.render_report(rows)
    assert "30 songs" in report
    assert "p50: **4.00 min**" in report
    assert "p90: **9.00 min**" in report
    assert "Operational target: ✅ PASS" in report


# ── _wer (needs jiwer) ─────────────────────────────────────────────────────────

def test_wer_identical_is_zero():
    pytest.importorskip("jiwer")
    s = [seg(0, "alpha beta gamma"), seg(5, "delta epsilon")]
    assert sb._wer(s, s) == 0.0
