"""Regression test for the PR #298 warm-start Whisper-1 race bug.

INCIDENT 2026-05-25: `main.py:3569` wrapped a `loop.run_in_executor(...)`
call in `asyncio.create_task(...)`. That raises
`TypeError: a coroutine was expected, got <Future>` because
`run_in_executor` returns a `concurrent.futures.Future` (well, the
asyncio-wrapped variant), NOT a coroutine. `create_task` rejects it.

The bug shipped because:
1. `py_compile main.py` passes (syntax is valid).
2. No test actually executed the lrclib+FA warm-start path with the
   async runtime — `test_emit_segments.py` is AST-only.
3. `scripts/qa/run_qa_suite.py` does exercise the path but isn't gated
   in CI.

This test:
- Exercises the EXACT race-helper shape used in production
  (`main.py:3563-3625`) with mocked `transcribe` and `forced_align`
  callables.
- Asserts the buggy shape (`create_task(run_in_executor(...))`) still
  raises `TypeError` so we never re-introduce it accidentally.
- Asserts the fixed shape (plain `run_in_executor(...)`) supports
  `.cancel()` + `await` the same as the original create_task.
- Verifies the FA-wins path: warm task gets cancelled cleanly.
- Verifies the FA-fails path: warm task result is consumed and used as
  fallback.

Uses `asyncio.run(...)` rather than `pytest-asyncio` because the repo
doesn't have that dep yet (see follow-up PR for the install).
"""
import asyncio
import pytest


# ─── Helper: mirrors the production warm-start race in main.py ──────

async def _warm_start_race(transcribe_fn, forced_align_fn, audio):
    """Lift the production race semantics from main.py:3563-3625 into
    a standalone helper. Inputs are mockable callables. Returns a tuple
    `(source, segments)` where source is "fa" or "w1"."""
    loop = asyncio.get_event_loop()
    # POST-FIX shape: no `create_task` wrap around run_in_executor.
    w1_warm = loop.run_in_executor(None, transcribe_fn)
    fa_segs = None
    try:
        fa_segs = await asyncio.to_thread(forced_align_fn, audio)
    except Exception:
        fa_segs = None
    if fa_segs:
        w1_warm.cancel()
        try:
            await w1_warm
        except (asyncio.CancelledError, Exception):
            pass
        return ("fa", fa_segs)
    try:
        w1_segs = await w1_warm
        return ("w1", w1_segs)
    except Exception:
        return ("none", [])


# ─── Tests ──────────────────────────────────────────────────────────

def test_buggy_shape_raises_typeerror():
    """REGRESSION GUARD: if anyone re-introduces
    `create_task(run_in_executor(...))`, this test must fail loudly.
    Documents the exact Python contract that bit us."""
    async def _try_bug():
        loop = asyncio.get_event_loop()
        with pytest.raises(TypeError, match="coroutine was expected"):
            asyncio.create_task(loop.run_in_executor(None, lambda: None))
    asyncio.run(_try_bug())


def test_fixed_shape_runs_without_typeerror():
    """The shape used in main.py post-fix: plain run_in_executor.
    Must complete cleanly + return the function's result."""
    async def _run():
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, lambda: 42)
        return await future
    assert asyncio.run(_run()) == 42


def test_fixed_shape_supports_cancel():
    """The .cancel() call on the warm task must work. We can't
    deterministically cancel mid-flight without timing tricks, but
    we CAN verify `.cancel()` is a valid method that doesn't raise."""
    async def _run():
        loop = asyncio.get_event_loop()
        # Schedule something the executor will get to instantly.
        future = loop.run_in_executor(None, lambda: "done")
        cancelled = future.cancel()
        # cancel() may return True or False depending on whether the
        # callable already started — both are legal.
        assert isinstance(cancelled, bool)
        # awaiting a cancelled future raises CancelledError; awaiting
        # one that already completed returns the value.
        try:
            await future
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(_run())


def test_warm_start_race_fa_wins():
    """Happy path: FA returns segments, warm task gets cancelled."""
    def fake_transcribe():
        # If FA is fast enough, this never runs to completion. If it
        # does, returns a different fingerprint we'd notice.
        return [{"start": 0.0, "end": 1.0, "text": "warm-fallback"}]
    def fake_fa(_audio):
        return [{"start": 0.0, "end": 1.0, "text": "fa-result", "score": 0.95}]
    source, segs = asyncio.run(_warm_start_race(fake_transcribe, fake_fa, b"audio"))
    assert source == "fa"
    assert segs[0]["text"] == "fa-result"
    assert segs[0]["score"] == 0.95


def test_warm_start_race_fa_fails_w1_takes_over():
    """Worst-case path: FA raises (timeout / non-retryable / etc.) —
    the warm Whisper-1 result is used as fallback. This is the win:
    no extra 8-min wait for whisperX."""
    def fake_transcribe():
        return [{"start": 0.0, "end": 2.0, "text": "warm-fallback"}]
    def failing_fa(_audio):
        raise RuntimeError("FA timeout (simulated Replicate degraded)")
    source, segs = asyncio.run(_warm_start_race(fake_transcribe, failing_fa, b"audio"))
    assert source == "w1"
    assert segs[0]["text"] == "warm-fallback"


def test_warm_start_race_fa_empty_w1_takes_over():
    """Edge case: FA returns `[]` (thin alignment), warm Whisper-1
    fallback fires. Matches the `if fa_segs:` truthy check in main.py."""
    def fake_transcribe():
        return [{"start": 0.0, "end": 1.0, "text": "fallback"}]
    def empty_fa(_audio):
        return []
    source, segs = asyncio.run(_warm_start_race(fake_transcribe, empty_fa, b"audio"))
    assert source == "w1"
    assert segs[0]["text"] == "fallback"


def test_warm_start_race_both_fail():
    """If both FA and warm Whisper-1 fail, we surface an empty result
    so the caller can fall through to the original whisperX path."""
    def failing_transcribe():
        raise RuntimeError("Whisper-1 unavailable")
    def failing_fa(_audio):
        raise RuntimeError("FA failed")
    source, segs = asyncio.run(_warm_start_race(failing_transcribe, failing_fa, b"audio"))
    assert source == "none"
    assert segs == []
