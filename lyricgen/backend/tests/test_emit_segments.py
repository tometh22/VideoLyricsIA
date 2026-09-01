"""Tests for the `_emit_segments` chokepoint inside main._run_transcription_for_job.

The chokepoint is the architectural invariant for Bugs B + D: every
return path that ships segments MUST go through `_emit_segments`, which
(a) calls `set_timing_source` so the job is never tagged with NULL,
(b) runs `normalize_words` so FA word-stamps with score are preserved
while Whisper-raw words are stripped, and (c) applies `_snap` (split +
beat-snap + chorus repetitions).

We test the invariant two ways:

1. STRUCTURAL (AST scan): `test_no_raw_segment_returns_in_orchestrator`
   walks `main.py` and FAILS if it finds any `return {...}` literal
   inside `_run_transcription_for_job` that builds a dict containing a
   `segments` key. The only way to ship segments must be via
   `_emit_segments(...)`. Future refactors that accidentally add a raw
   return are caught in CI before they reach staging.

2. SEMANTIC: importing `timing_sources` and asserting the allow-list
   includes the values we expect for each known return path, so adding
   a new path requires adding the source name to the registry.
"""
import ast
from pathlib import Path

import pytest

from timing_sources import (
    VALID_TIMING_SOURCES,
    DEPRECATED_TIMING_SOURCES,
    FORCED_ALIGN,
    WHISPERX_RECONCILED,
    WHISPERX,
    WHISPER_LRCLIB,
    WHISPER_LRCLIB_REC,
    WHISPER_GEMINI_REC,
    WHISPER_RAW,
)


_MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


def _find_orchestrator(tree):
    """Locate `async def _run_transcription_for_job` at module scope.
    Fails the test explicitly if the function was renamed — surfaces
    the structural change so we update the AST test on purpose."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_transcription_for_job":
            return node
    raise AssertionError(
        "_run_transcription_for_job not found in main.py — was it renamed? "
        "Update this test's _find_orchestrator() to match.")


def _dict_has_key(node, key: str) -> bool:
    for k in (node.keys or []):
        if isinstance(k, ast.Constant) and k.value == key:
            return True
    return False


def test_no_raw_segment_returns_in_orchestrator():
    """Bug D structural guard. Every segments-bearing return inside the
    orchestrator MUST go through `_emit_segments(...)`. The AST scan
    fails the test if it sees a raw `return {"segments": ...}`."""
    src = _MAIN_PATH.read_text()
    tree = ast.parse(src)
    func = _find_orchestrator(tree)
    raw_returns = []
    for n in ast.walk(func):
        if not isinstance(n, ast.Return):
            continue
        if isinstance(n.value, ast.Dict) and _dict_has_key(n.value, "segments"):
            raw_returns.append((n.lineno, ast.dump(n.value)[:120]))
    assert not raw_returns, (
        "Bug D regression risk — raw segment returns found inside "
        "_run_transcription_for_job. Wrap them in _emit_segments(...) instead.\n"
        + "\n".join(f"  line {lineno}: {dump}..." for lineno, dump in raw_returns)
    )


def test_orchestrator_imports_timing_sources():
    """The chokepoint helper references `VALID_TIMING_SOURCES` and the
    individual source constants from `timing_sources`. If we ever ditch
    the registry, this test fails — forcing us to revisit the AST guard."""
    src = _MAIN_PATH.read_text()
    assert "VALID_TIMING_SOURCES" in src, \
        "main.py must reference VALID_TIMING_SOURCES — registry is the source of truth"


def test_all_expected_source_constants_are_valid():
    """Each known emission point in the orchestrator uses a constant
    from `timing_sources`. Asserting they're all in `VALID_TIMING_SOURCES`
    catches typos at test time instead of at runtime."""
    expected = [
        FORCED_ALIGN, WHISPERX_RECONCILED, WHISPERX,
        WHISPER_LRCLIB, WHISPER_LRCLIB_REC,
        WHISPER_GEMINI_REC, WHISPER_RAW,
    ]
    for src in expected:
        assert src in VALID_TIMING_SOURCES, f"{src!r} missing from VALID_TIMING_SOURCES"


def test_deprecated_sources_not_emitted_by_orchestrator():
    """Bug regression: `lrclib_synced`, `lrclib_synced_with_offset`,
    `lrclib_plain` were eliminated by PR-G. Assert they're not passed
    to set_timing_source anywhere in the orchestrator source."""
    src = _MAIN_PATH.read_text()
    for dep in DEPRECATED_TIMING_SOURCES:
        bad_pattern = f'set_timing_source(job_id, "{dep}")'
        assert bad_pattern not in src, \
            f"Deprecated timing_source {dep!r} appears in main.py — Bug regression"


def test_orchestrator_freezes_raw_recognition_before_selected_output():
    """Training provenance must survive catalogue reconciliation privately."""
    src = _MAIN_PATH.read_text()
    assert "_recognition_hypotheses" in src
    assert "_recognition_collector.snapshot()" in src
    assert 'out["_recognition_hypotheses"]' in src
    assert 'out["_recognition_attempt_count"]' in src
    assert 'if key != "_recognition_family"' in src
    orchestrator = ast.get_source_segment(
        src, _find_orchestrator(ast.parse(src)),
    )
    assert "run_in_executor" not in orchestrator, (
        "recognizers must use asyncio.to_thread so the per-job provenance "
        "context reaches provider wrappers"
    )
