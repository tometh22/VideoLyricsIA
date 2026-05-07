"""Tests for _attach_close_chain (pipeline.py).

Pure-Python — no moviepy needed. Covers the contract that closing a
derived clip cascades to the source clips whose ownership we propagated
into it. Pre-fix the loop-fallback in _get_background_clip_from_path
leaked one VideoFileClip per loop iteration because moviepy's
concatenate_videoclips/.close() does NOT cascade to its inputs.
"""

from pipeline import _attach_close_chain


class _FakeClip:
    """Minimal moviepy.VideoFileClip stand-in for close-chain testing."""

    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def test_close_chain_closes_owned_clips():
    src1 = _FakeClip("src1")
    src2 = _FakeClip("src2")
    derived = _FakeClip("derived")

    out = _attach_close_chain(derived, [src1, src2])
    assert out is derived

    assert src1.closed is False
    assert src2.closed is False
    assert derived.closed is False

    out.close()

    assert derived.closed is True
    assert src1.closed is True
    assert src2.closed is True


def test_close_chain_swallows_owner_close_errors():
    # If an owned clip's .close() raises, we must still close every
    # other owner — moviepy can leak FDs if a single bad clip aborts
    # the chain.
    bad = _FakeClip("bad")
    def _broken_close():
        bad.closed = True
        raise RuntimeError("simulated close failure")
    bad.close = _broken_close

    good = _FakeClip("good")
    derived = _FakeClip("derived")

    _attach_close_chain(derived, [bad, good])
    derived.close()

    assert derived.closed is True
    assert bad.closed is True   # we set this before raising
    assert good.closed is True  # must run despite the failure above


def test_close_chain_runs_owner_close_after_target_close():
    # Order matters for moviepy: the derived clip's reader may still be
    # holding a ref to the source's frame data while close() runs.
    # Closing the source first could yank the buffer out from under it.
    order = []

    src = _FakeClip("src")
    src_close = src.close
    def _src_close():
        order.append("src")
        src_close()
    src.close = _src_close

    derived = _FakeClip("derived")
    derived_close = derived.close
    def _derived_close():
        order.append("derived")
        derived_close()
    derived.close = _derived_close

    _attach_close_chain(derived, [src])
    derived.close()

    assert order == ["derived", "src"]
