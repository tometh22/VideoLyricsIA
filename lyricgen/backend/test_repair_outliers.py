"""Unit tests for forced_align.repair_outlier_onsets — the repeated-chorus
rescue (A2). Covers: a verbatim-repeat line stranded far from its expected time
gets re-placed; normal alignment is left untouched; monotonicity is enforced;
length mismatch degrades gracefully."""
import forced_align as fa


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_repairs_repeated_chorus_outlier():
    # 8 evenly-sung lines over 0..40s. Line index 4 ("Me gustas mucho") got
    # bound to a LATER occurrence → stranded at 34s when it should be ~22s.
    lines = ["Verso uno", "Verso dos", "Me gustas mucho", "Me gustas mucho",
             "Me gustas mucho", "Me gustas mucho", "Cierre uno", "Cierre dos"]
    # expected ~ (i+0.5)/8 * 40 = 2.5, 7.5, 12.5, 17.5, 22.5, 27.5, 32.5, 37.5
    segs = [_seg(2.4, 4, "Verso uno"), _seg(7.6, 9, "Verso dos"),
            _seg(12.4, 14, "Me gustas mucho"), _seg(17.6, 19, "Me gustas mucho"),
            _seg(34.0, 36, "Me gustas mucho"),   # ← OUTLIER (+11.5s off expected)
            _seg(27.4, 29, "Me gustas mucho"), _seg(32.6, 34, "Cierre uno"),
            _seg(37.4, 39, "Cierre dos")]
    out = fa.repair_outlier_onsets(segs, lines, 40.0, None)
    # the outlier (index 4) should be pulled back near ~22.5 (between trusted 3 & 5)
    assert out[4].get("repaired") is True
    assert 20.0 <= out[4]["start"] <= 25.0, f"outlier not repaired: {out[4]['start']}"
    # onsets monotonic non-decreasing
    starts = [s["start"] for s in out]
    assert starts == sorted(starts), f"not monotonic: {starts}"


def test_leaves_good_alignment_untouched():
    lines = [f"Linea {i}" for i in range(6)]
    segs = [_seg((i + 0.5) / 6 * 30 + 0.3, 0, f"Linea {i}") for i in range(6)]  # all ~expected
    out = fa.repair_outlier_onsets(segs, lines, 30.0, None)
    assert not any(s.get("repaired") for s in out), "no line should be repaired"


def test_force_monotonic_clamps_backwards_line():
    segs = [_seg(10.0, 11, "a"), _seg(8.0, 9, "b"), _seg(12.0, 13, "c")]
    out = fa._force_monotonic(segs)
    assert out[1]["start"] >= out[0]["start"], "backwards line must be clamped forward"
    assert out[2]["start"] >= out[1]["start"]


def test_length_mismatch_degrades_to_monotonic():
    segs = [_seg(5.0, 6, "a"), _seg(3.0, 4, "b")]   # 2 segs
    out = fa.repair_outlier_onsets(segs, ["a", "b", "c"], 20.0, None)  # 3 lines
    assert out[1]["start"] >= out[0]["start"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
