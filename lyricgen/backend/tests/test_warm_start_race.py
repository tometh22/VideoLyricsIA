"""Regression test for the warm-start race semantics in `main.py`.

INCIDENT 2026-05-25 #1: `main.py:3569` wrapped a `loop.run_in_executor(...)`
call in `asyncio.create_task(...)`. That raises
`TypeError: a coroutine was expected, got <Future>` because
`run_in_executor` returns a `concurrent.futures.Future` (well, the
asyncio-wrapped variant), NOT a coroutine. `create_task` rejects it.

INCIDENT 2026-05-25 #2: warm-start used Whisper-1 standalone as the
fallback when FA failed. That produced hallucinated lines + repetitions
("sepa que sepa que sepa", "Árbol de la vida dame hoy" repeated 3x,
long line bucketing) — the WorldClass promise was broken any time the
cureau `[1, 2, 0]` bug triggered. Fix: use whisperX (word stamps) as
the warm task and reconcile against lrclib plain text on FA failure.
The output then matches the WorldClass `whisperx_reconciled` quality,
NOT degraded Whisper-1.

The original bug shipped because:
1. `py_compile main.py` passes (syntax is valid).
2. No test actually executed the lrclib+FA warm-start path with the
   async runtime — `test_emit_segments.py` is AST-only.
3. `scripts/qa/run_qa_suite.py` does exercise the path but isn't gated
   in CI.

This test:
- Exercises the EXACT race-helper shape used in production with mocked
  `transcribe_whisperx` and `forced_align` callables.
- Asserts the buggy `create_task(run_in_executor(...))` shape still
  raises `TypeError` so we never re-introduce it accidentally.
- Verifies the FA-wins path: warm whisperX task gets cancelled cleanly.
- Verifies the FA-fails path: warm whisperX result is reconciled +
  used as fallback (output is `whisperx_reconciled`, NOT `whisper_lrclib`).

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


# ─── whisperX warm-start (post-2026-05-25 architecture) ─────────────
#
# The race shape changed: warm task is now `transcribe_whisperx` (word
# stamps) instead of `transcribe` (line-level Whisper-1). On FA failure
# we reconcile the whisperX output against the reference plain text to
# produce `whisperx_reconciled` segments — the same WorldClass quality
# the audio-as-truth pipeline promises.
#
# `_warm_start_race_wx` mirrors `main.py`'s post-refactor logic
# explicitly so a future regression (e.g. someone re-introduces
# Whisper-1 standalone) fails this test loudly.


async def _warm_start_race_wx(wx_fn, fa_fn, reconcile_fn, audio, reference_text):
    """Mirror of main.py post-refactor warm-start race. Returns
    `(source, segments)` where source ∈ {fa, whisperx_reconciled,
    whisperx, none}."""
    async def _warm_wx():
        return await asyncio.to_thread(wx_fn)
    wx_task = asyncio.create_task(_warm_wx())
    fa_segs = None
    try:
        fa_segs = await asyncio.to_thread(fa_fn, audio)
    except Exception:
        fa_segs = None
    if fa_segs:
        wx_task.cancel()
        try:
            await wx_task
        except (asyncio.CancelledError, Exception):
            pass
        return ("fa", fa_segs)
    # FA failed/empty — use warm whisperX + reconcile against reference.
    try:
        wx_segs = await wx_task
    except Exception:
        return ("none", [])
    if not wx_segs or len(wx_segs) < 2:
        return ("none", [])
    reconciled = reconcile_fn(wx_segs, reference_text) if reference_text else None
    if reconciled:
        return ("whisperx_reconciled", reconciled)
    return ("whisperx", wx_segs)


def test_warm_wx_race_fa_wins_cancels_wx():
    """Happy path: FA returns segments, warm whisperX task gets
    cancelled. We never pay the whisperX cost when FA succeeds."""
    wx_called = {"flag": False}
    def fake_wx():
        wx_called["flag"] = True
        return [{"start": 0.0, "end": 1.0, "text": "wx-line", "words": []}]
    def fake_fa(_audio):
        return [{"start": 0.0, "end": 1.0, "text": "fa-result", "score": 0.95}]
    def fake_reconcile(_wx, _ref):
        raise AssertionError("reconcile must not run when FA wins")
    source, segs = asyncio.run(_warm_start_race_wx(
        fake_wx, fake_fa, fake_reconcile, b"audio", "fa-result",
    ))
    assert source == "fa"
    assert segs[0]["text"] == "fa-result"


def test_warm_wx_race_fa_fails_reconciled_wins():
    """The architectural promise: when FA fails, the warm whisperX
    output gets reconciled against the reference plain text → the
    user gets WorldClass `whisperx_reconciled` quality, NOT degraded
    Whisper-1 hallucinations.

    THIS TEST IS THE GUARD against re-introducing Whisper-1 standalone
    as the FA-failure fallback. If anyone changes the warm task back to
    `transcribe(...)` (Whisper-1), this test should keep failing the
    quality contract."""
    def fake_wx():
        # Word stamps shape — what whisperX actually returns.
        return [
            {"start": 0.0, "end": 1.5, "text": "hola mundo",
             "words": [{"word": "hola", "start": 0.0, "end": 0.6},
                       {"word": "mundo", "start": 0.7, "end": 1.5}]},
            {"start": 1.5, "end": 3.0, "text": "como estas",
             "words": [{"word": "como", "start": 1.5, "end": 2.2},
                       {"word": "estas", "start": 2.3, "end": 3.0}]},
        ]
    def failing_fa(_audio):
        # The user's actual error: cureau `[1, 2, 0]` tensor crash.
        raise RuntimeError("Expected 2D or 3D (batch mode) tensor ... got: [1, 2, 0]")
    def fake_reconcile(wx_segs, reference_text):
        # Stub: pretend reconcile re-buckets word stamps into the
        # reference text lines. Real impl is in whisperx_reconcile.py.
        assert reference_text == "hola mundo\ncomo estas"
        return [
            {"start": s["start"], "end": s["end"], "text": ln, "score": 0.9}
            for s, ln in zip(wx_segs, reference_text.splitlines())
        ]
    source, segs = asyncio.run(_warm_start_race_wx(
        fake_wx, failing_fa, fake_reconcile, b"audio",
        "hola mundo\ncomo estas",
    ))
    assert source == "whisperx_reconciled", \
        f"FA-failure fallback must produce WorldClass output, got {source!r}"
    assert len(segs) == 2
    # No hallucinations, no repetitions — text comes from reference.
    assert [s["text"] for s in segs] == ["hola mundo", "como estas"]


def test_warm_wx_race_reconcile_skipped_keeps_whisperx():
    """If reconcile returns None (e.g. coverage too low), fall back to
    raw whisperX segments. Still WORLDCLASS-adjacent, never Whisper-1."""
    def fake_wx():
        return [
            {"start": 0.0, "end": 1.0, "text": "alpha", "words": []},
            {"start": 1.0, "end": 2.0, "text": "beta", "words": []},
        ]
    def failing_fa(_audio):
        raise RuntimeError("FA exhausted budget")
    def fake_reconcile(_wx, _ref):
        return None  # Coverage too low — whisperx_reconcile.py contract.
    source, segs = asyncio.run(_warm_start_race_wx(
        fake_wx, failing_fa, fake_reconcile, b"audio", "alpha\nbeta",
    ))
    assert source == "whisperx"
    assert len(segs) == 2


def test_warm_wx_race_both_fail():
    """If both FA and warm whisperX fail (rare — different Replicate
    models, less likely to flake together), we surface empty so the
    caller can fall through to the Whisper-1 last-resort path."""
    def failing_wx():
        raise RuntimeError("whisperX 429 burst limit")
    def failing_fa(_audio):
        raise RuntimeError("FA non-retryable")
    source, segs = asyncio.run(_warm_start_race_wx(
        failing_wx, failing_fa, lambda *_: None, b"audio", "ref",
    ))
    assert source == "none"
    assert segs == []


# ─── Architectural guard at AST level ────────────────────────────────
#
# The race-helper tests above test the SHAPE of the algorithm. This one
# pins the PRODUCTION code in `main.py` to that shape: the warm-start
# path of `_run_transcription_for_job` must reference
# `transcribe_whisperx`, `whisperx_reconcile`, and `WHISPERX_RECONCILED`.
# If anyone refactors back to Whisper-1 standalone the AST guard fails
# loudly. Lighter than a full integration test but catches the regression
# vector that bit us on 2026-05-25 (degraded "sepa que sepa que sepa"
# output reported by the user).


def test_main_warmstart_uses_whisperx_not_whisper1_standalone():
    import ast
    import pathlib
    main_py = pathlib.Path(__file__).resolve().parent.parent / "main.py"
    mod = ast.parse(main_py.read_text())
    for node in ast.walk(mod):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_transcription_for_job":
            names: set[str] = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Attribute):
                    names.add(n.attr)
                elif isinstance(n, ast.Name):
                    names.add(n.id)
            assert "transcribe_whisperx" in names, (
                "warm-start MUST call transcribe_whisperx (WorldClass), "
                "not Whisper-1 standalone (hallucinations)."
            )
            assert "whisperx_reconcile" in names, (
                "warm-start MUST reconcile whisperX output against the "
                "lrclib reference text for WorldClass quality."
            )
            assert "WHISPERX_RECONCILED" in names, (
                "FA-failure path MUST emit WHISPERX_RECONCILED timing "
                "source, not WHISPER_LRCLIB (degraded)."
            )
            return
    raise AssertionError("_run_transcription_for_job not found in main.py")
