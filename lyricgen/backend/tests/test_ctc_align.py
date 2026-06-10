"""Unit tests for ctc_align.py — the Genly CTC timing engine.

Covers the PURE pieces (no torch, no model download, no audio): text
normalization, target building with the synthetic star, span→line
grouping, the collapsed-alignment guard, and the decline-fast paths of
`retime_segments` (flag off / too few lines / missing file), which all
exit BEFORE any heavy import. The acoustic quality itself is covered by
the offline benchmark vs Rotor ground truth (scripts/exp_ctc_align.py),
not by unit tests.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ctc_align  # noqa: E402

# a-z + accented vowels + ñ as a fake vocab; ids arbitrary but unique
VOCAB = {c: i + 5 for i, c in enumerate("abcdefghijklmnopqrstuvwxyzáéíóúñü'")}
STAR = 999


def test_norm_word_keeps_spanish_chars():
    assert ctc_align.norm_word("Canción") == "canción"
    assert ctc_align.norm_word("¡Hola!") == "hola"
    assert ctc_align.norm_word("pingüino") == "pingüino"
    assert ctc_align.norm_word("(woo)") == "woo"
    assert ctc_align.norm_word("123") == ""


def test_build_targets_stars_at_edges_and_between_lines():
    targets, words = ctc_align.build_targets(["la la", "si"], VOCAB, STAR)
    # leading star + line0 + star + line1 + trailing star: spoken intros /
    # outros must have somewhere to go that isn't the first/last line
    assert [w[0] for w in words] == [-1, 0, 0, -1, 1, -1]
    assert targets.count(STAR) == 3
    assert len(targets) == 1 + 2 + 2 + 1 + 2 + 1


def test_build_targets_word_separator_within_lines():
    sep = 777
    targets, words = ctc_align.build_targets(["la la", "si"], VOCAB, STAR,
                                             word_sep_id=sep)
    # separator BETWEEN words of a line (appended to the preceding word's
    # count), never at line edges
    assert targets.count(sep) == 1
    first_la = next(w for w in words if w[0] == 0)
    assert first_la[2] == 3  # 'l','a',sep
    # bookkeeping stays 1:1 with targets
    assert sum(n for _, _, n in words) == len(targets)


def test_build_targets_skips_unalignable_words():
    targets, words = ctc_align.build_targets(["123 ok"], VOCAB, STAR)
    assert [w[1] for w in words if w[0] >= 0] == ["ok"]


def test_spans_to_lines_groups_words_and_lines():
    # leading star + "ab" + star + "cd" + trailing star → 7 tokens
    targets, words = ctc_align.build_targets(["ab", "cd"], VOCAB, STAR)
    spans = [(0, 5, 0.5),                      # leading star
             (10, 20, 0.9), (20, 30, 0.8),     # ab
             (30, 80, 0.5),                    # star
             (80, 85, 0.7), (85, 90, 0.6),     # cd
             (90, 95, 0.5)]                    # trailing star
    lines = ctc_align.spans_to_lines(spans, words, 2, frame_to_s=0.02)
    (s0, e0, w0), (s1, e1, w1) = lines
    assert (s0, e0) == (10 * 0.02, 30 * 0.02)
    assert (s1, e1) == (80 * 0.02, 90 * 0.02)
    assert w0[0][0] == "ab" and len(w0) == 1 and len(w1) == 1
    assert s1 >= e0  # monotonic across the star gap


def test_spans_to_lines_none_for_unalignable_line():
    targets, words = ctc_align.build_targets(["ab", "123"], VOCAB, STAR)
    spans = [(0, 5, 0.5), (10, 20, 0.9), (20, 30, 0.8),
             (30, 40, 0.5), (40, 45, 0.5)]
    lines = ctc_align.spans_to_lines(spans, words, 2, frame_to_s=0.02)
    assert lines[0] is not None and lines[1] is None


def test_repair_bridge_intro_case():
    """Costumbres line 0 shape: first words bind to spoken-intro voice,
    one word bridges 27s of instrumental, the rest sits at the real
    verse. The bigger cluster (real verse side) must win."""
    ws = [("Muerdo", 3.54, 5.66, 0.75), ("el", 5.80, 6.98, 0.98),
          ("anzuelo", 8.30, 35.12, 0.88),  # 27s bridge word
          ("y", 35.90, 36.00, 0.66), ("vuelvo", 36.04, 36.56, 0.98)]
    regions = [(3.0, 10.2), (32.9, 41.8)]
    (s, e, kept), = ctc_align.repair_bridge_words([(3.54, 36.56, ws)], regions)
    assert e == 36.56
    assert s > 25  # snapped to the real verse, not the spoken intro
    assert [w[0] for w in kept] == ["anzuelo", "y", "vuelvo"]


def test_repair_bridge_outro_case():
    """Costumbres line 19 shape: last words dragged over the outro where
    the stem has no voice. The line must end near the real singing."""
    ws = [("Costumbres", 152.76, 154.12, 0.98), ("argentinas", 154.22, 155.68, 0.91),
          ("de,", 155.72, 156.80, 0.93),
          ("decir,", 157.18, 180.16, 0.61),  # 23s bridge word
          ("no", 183.40, 187.18, 0.20)]
    regions = [(147.2, 159.2)]
    (s, e, kept), = ctc_align.repair_bridge_words([(152.76, 187.18, ws)], regions)
    assert s == 152.76
    assert e < 161  # trimmed to plausible duration, near voice end
    assert kept[-1][0] == "decir,"


def test_repair_bridge_no_op_on_healthy_lines():
    ws = [("hola", 10.0, 10.5, 0.9), ("que", 10.6, 10.9, 0.9),
          ("tal", 11.0, 11.4, 0.9)]
    out = ctc_align.repair_bridge_words([(10.0, 11.4, ws)], [(9.0, 12.0)])
    assert out == [(10.0, 11.4, ws)]


def test_group_consecutive():
    assert ctc_align.group_consecutive([7, 8, 20, 31, 32]) == [[7, 8], [20], [31, 32]]
    assert ctc_align.group_consecutive([]) == []


def test_recovery_window_between_anchors():
    lt = [(10.0, 12.0, []), None, None, (40.0, 42.0, [])]
    assert ctc_align.recovery_window(lt, [1, 2], 200.0) == (11.0, 41.0)
    # degenerate (too small, pad=0) and oversized windows are rejected
    lt2 = [(10.0, 12.0, []), None, (12.5, 13.0, [])]
    assert ctc_align.recovery_window(lt2, [1], 200.0, pad=0.0) is None
    lt3 = [(10.0, 12.0, []), None]  # open-ended → until total_dur (189s)
    assert ctc_align.recovery_window(lt3, [1], 200.0) is None


def test_guess_text_lang():
    es = ["No quiero que me perdones", "Y no me pidas perdón",
          "Yo quería que nos pasara", "Para bien o para mal"]
    en = ["I want it I got it", "You like my hair gee thanks just bought it",
          "Ain't got enough money to pay me respect", "Look at my neck"]
    assert ctc_align.guess_text_lang(es) == "es"
    assert ctc_align.guess_text_lang(en) == "en"
    assert ctc_align.guess_text_lang(["la"]) == "unknown"


def test_retime_declines_on_english_text(monkeypatch, tmp_path):
    """The English guard fires BEFORE any audio/model work."""
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    f = tmp_path / "a.wav"
    f.write_bytes(b"RIFF")
    segs = [{"text": t, "start": 0, "end": 0} for t in
            ["I want it I got it", "You like my hair gee thanks",
             "Just bought it yeah", "And I want it I got it"]]
    assert ctc_align.retime_segments(str(f), segs) is None


def test_looks_collapsed_detects_crammed_alignment():
    ok = [(0.0, 2.0, []), (3.0, 5.0, []), (6.0, 8.0, [])]
    assert not ctc_align.looks_collapsed(ok)
    crammed = [(0.0, 0.05, []), (0.1, 0.14, []), (6.0, 8.0, [])]
    assert ctc_align.looks_collapsed(crammed)
    assert ctc_align.looks_collapsed([None, None])


def test_retime_declines_fast_without_torch(monkeypatch, tmp_path):
    segs = [{"text": "hola", "start": 0, "end": 1}] * 5
    # flag off → None before any heavy import
    monkeypatch.delenv("CTC_ALIGN_ENABLED", raising=False)
    assert ctc_align.retime_segments("/nope.wav", segs) is None
    # flag on but too few lines
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    assert ctc_align.retime_segments("/nope.wav", segs[:2]) is None
    # flag on, enough lines, missing file
    assert ctc_align.retime_segments(str(tmp_path / "missing.wav"), segs) is None


def test_ctc_align_is_valid_timing_source():
    from timing_sources import CTC_ALIGN, VALID_TIMING_SOURCES
    assert CTC_ALIGN in VALID_TIMING_SOURCES
    assert len(CTC_ALIGN) <= 20  # VARCHAR(20) constraint


def test_wrapper_flag_off_is_identity(monkeypatch):
    """THE contract of the PR: with CTC_ALIGN_ENABLED off,
    _maybe_ctc_retime returns the very same object — prod behaviour is
    byte-identical to before the wiring."""
    import asyncio
    from main import _maybe_ctc_retime

    monkeypatch.delenv("CTC_ALIGN_ENABLED", raising=False)
    result = {"job_id": "j1", "segments": [{"text": "hola", "start": 1.0}] * 5}
    out = asyncio.run(_maybe_ctc_retime(result, "/no/such/audio.mp3", "j1"))
    assert out is result  # same object, not a copy


def test_wrapper_never_raises_even_with_flag_on(monkeypatch):
    """Flag ON + missing audio/stem must decline to identity, not 500."""
    import asyncio
    from main import _maybe_ctc_retime

    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.delenv("VOCAL_SEP_ENABLED", raising=False)  # no stem possible
    result = {"job_id": "j2", "segments": [{"text": "hola", "start": 1.0}] * 5}
    out = asyncio.run(_maybe_ctc_retime(result, "/no/such/audio.mp3", "j2"))
    assert out is result
