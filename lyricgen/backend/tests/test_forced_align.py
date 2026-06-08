"""Unit tests for forced_align — the pure parts (no Replicate network).

The network call (`forced_align_lyrics`) is exercised only for its
disabled/short-circuit branches; the alignment math lives in
`wordstamps_to_segments` and is fully testable offline.
"""

import forced_align as fa


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_is_enabled_requires_flag_and_token(monkeypatch):
    monkeypatch.delenv("FORCED_ALIGNER_ENABLED", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert fa.is_enabled() is False
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    assert fa.is_enabled() is False          # flag on but no token
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert fa.is_enabled() is True
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "0")
    assert fa.is_enabled() is False          # token but flag off


def test_lrc_to_plain_text_strips_timestamps():
    synced = "[00:12.34] primera linea\n[01:05.6] segunda linea\n\n[02:00] tercera"
    assert fa.lrc_to_plain_text(synced) == "primera linea\nsegunda linea\ntercera"
    assert fa.lrc_to_plain_text("") == ""
    assert fa.lrc_to_plain_text(None) == ""


def _strip_words(segs):
    """Helper: compare timing/text fields, ignore per-word stamps (which the
    refactor preserves for karaoke + confidence)."""
    return [{k: v for k, v in s.items() if k != "words"} for s in segs]


def test_wordstamps_to_segments_reconstructs_lines():
    lines = ["hola mundo", "adios amigo cruel"]
    words = [
        _w("hola", 1.0, 1.4), _w("mundo", 1.4, 2.0),
        _w("adios", 3.0, 3.4), _w("amigo", 3.4, 3.8), _w("cruel", 3.8, 4.5),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert _strip_words(segs) == [
        {"start": 1.0, "end": 2.0, "text": "hola mundo"},
        {"start": 3.0, "end": 4.5, "text": "adios amigo cruel"},
    ]
    # PR-G: words preserved per line for karaoke + confidence highlighting.
    assert [w["word"] for w in segs[0]["words"]] == ["hola", "mundo"]
    assert [w["word"] for w in segs[1]["words"]] == ["adios", "amigo", "cruel"]


def test_wordstamps_to_segments_clamps_overlap_monotonic():
    lines = ["uno dos", "tres cuatro"]
    # second line starts BEFORE first line's end → clamp first end.
    words = [
        _w("uno", 1.0, 2.5), _w("dos", 2.5, 5.0),
        _w("tres", 4.0, 4.5), _w("cuatro", 4.5, 6.0),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs[0]["end"] <= segs[1]["start"]      # no overlap
    assert segs[0]["start"] == 1.0 and segs[1]["end"] == 6.0


def test_wordstamps_to_segments_skips_blank_and_missing():
    lines = ["", "  ", "linea real"]
    words = [_w("linea", 1.0, 1.5), _w("real", 1.5, 2.0)]
    segs = fa.wordstamps_to_segments(words, lines)
    assert _strip_words(segs) == [{"start": 1.0, "end": 2.0, "text": "linea real"}]


def test_destretch_trims_ballooned_trailing_word():
    """Hermanos pattern: the model stretches the last word to fill the
    instrumental gap (line held 12 s). De-stretch caps the end back near
    where the word actually started + a normal tail."""
    # Median word ~0.3s; "vos" stretched 0.6 -> 12.0 (11.4s).
    words = [
        _w("Yo", 122.6, 122.9), _w("solo", 122.9, 123.3), _w("quiero", 123.3, 123.8),
        _w("vagar", 123.8, 124.3), _w("con", 124.3, 124.6), _w("vos", 124.6, 134.8),
        _w("Yo", 134.8, 135.1), _w("solo", 135.1, 135.5),  # next line words (median)
    ]
    segs = fa.wordstamps_to_segments(words, ["Yo solo quiero vagar con vos", "Yo solo"])
    line14 = segs[0]
    dur = line14["end"] - line14["start"]
    assert dur < 3.0, f"line should be de-stretched, got {dur:.1f}s"
    assert line14["start"] == 122.6           # sung start preserved
    assert line14["end"] <= 134.8             # never extends past the word


def test_destretch_leaves_normal_lines_untouched():
    words = [
        _w("hola", 1.0, 1.4), _w("mundo", 1.4, 2.0),
        _w("chau", 3.0, 3.4), _w("amigo", 3.4, 4.0),
    ]
    segs = fa.wordstamps_to_segments(words, ["hola mundo", "chau amigo"])
    assert _strip_words(segs) == [
        {"start": 1.0, "end": 2.0, "text": "hola mundo"},
        {"start": 3.0, "end": 4.0, "text": "chau amigo"},
    ]


def test_wordstamps_preserves_score_when_available():
    # PR-G: when the cureau model returns per-word `score` (via the
    # show_probabilities=True input we now pass), preserve it on each word so
    # the editor can render low-confidence lines tinted red.
    words = [
        {"word": "hola", "start": 1.0, "end": 1.4, "score": 0.95},
        {"word": "mundo", "start": 1.4, "end": 2.0, "score": 0.42},  # low!
    ]
    segs = fa.wordstamps_to_segments(words, ["hola mundo"])
    assert "words" in segs[0]
    scores = [w.get("score") for w in segs[0]["words"]]
    assert scores == [0.95, 0.42]


def test_compress_for_upload_falls_back_on_bad_input():
    """ffmpeg can't transcode a nonexistent file → graceful fallback to the
    original path (never raises, never leaves a temp behind)."""
    path, is_temp = fa._compress_for_upload("/nonexistent/audio.wav")
    assert path == "/nonexistent/audio.wav"
    assert is_temp is False


def test_wordstamps_reanchors_when_model_inserts_extra_word():
    # The model tokenises "rocknroll" as THREE stamps (rock/n/roll) where the
    # lyric line has it as one word. A naive positional walk would consume the
    # wrong stamps from here on (drift). Re-anchoring must keep later lines
    # aligned by matching their tokens forward in the stream.
    lines = ["amo el rocknroll", "vivo de noche", "canto sin parar"]
    words = [
        _w("amo", 1.0, 1.3), _w("el", 1.3, 1.6),
        _w("rock", 1.6, 1.9), _w("n", 1.9, 2.0), _w("roll", 2.0, 2.4),
        _w("vivo", 3.0, 3.3), _w("de", 3.3, 3.5), _w("noche", 3.5, 4.0),
        _w("canto", 5.0, 5.3), _w("sin", 5.3, 5.6), _w("parar", 5.6, 6.2),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    # Despite the extra token, the LATER lines must land on their real stamps.
    assert len(segs) == 3
    assert segs[1]["text"] == "vivo de noche"
    assert segs[1]["start"] == 3.0 and segs[1]["end"] == 4.0
    assert segs[2]["text"] == "canto sin parar"
    assert segs[2]["start"] == 5.0 and segs[2]["end"] == 6.2


def test_wordstamps_reanchors_when_model_drops_word():
    # The model dropped "muy" — fewer stamps than the lyric word count. The
    # next line must still anchor on its real stamps, not slide late.
    lines = ["hola mundo muy lindo", "adios para siempre"]
    words = [
        _w("hola", 1.0, 1.3), _w("mundo", 1.3, 1.7), _w("lindo", 1.7, 2.2),
        _w("adios", 3.0, 3.4), _w("para", 3.4, 3.7), _w("siempre", 3.7, 4.3),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert len(segs) == 2
    assert segs[1]["text"] == "adios para siempre"
    assert segs[1]["start"] == 3.0 and segs[1]["end"] == 4.3


def test_wordstamps_aborts_on_sustained_drift():
    # The word stream is unrelated noise (nothing matches the lyric lines for
    # several consecutive lines). The detector must give up (return []) so the
    # caller falls back, rather than emit a confidently-wrong drifting result.
    lines = ["primera linea cantada", "segunda linea distinta",
             "tercera estrofa nueva", "cuarta parte final",
             "quinta y ultima aqui"]
    words = [_w(f"ruido{i}", i, i + 0.4) for i in range(25)]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs == []


def test_forced_align_lyrics_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("FORCED_ALIGNER_ENABLED", raising=False)
    assert fa.forced_align_lyrics("/tmp/x.mp3", "a\nb\nc\nd") is None


def test_forced_align_lyrics_returns_none_for_short_lyrics(monkeypatch):
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    # < 4 lines → skip (not worth a call), returns None without network.
    assert fa.forced_align_lyrics("/tmp/x.mp3", "una\ndos") is None


def test_phonetic_ratio_catches_acoustic_mishears():
    """Sanity check on the phonetic helper itself — these are the cases the
    Legalícenla incident proved we need."""
    # Intro chorus mishear that anchored to the wrong region in PROD.
    assert fa._phonetic_ratio(["legalicenla"], ["lerealizanla"]) > 0.5
    # Word-split mishear ("le galisenla" instead of "Legalícenla").
    assert fa._phonetic_ratio(["legalicenla"], ["legalisenla"]) > 0.5
    # Different lyric — must stay below the anchor threshold.
    assert fa._phonetic_ratio(["legalicenla"], ["hubotiemposdeguerras"]) < 0.4
    assert fa._phonetic_ratio(["legalicenla"], ["paramiparami"]) < 0.4
    # Defensive: empty inputs return 0.0 instead of raising.
    assert fa._phonetic_ratio([], ["any"]) == 0.0
    assert fa._phonetic_ratio(["any"], []) == 0.0


def test_wordstamps_anchors_intro_chorus_mishear_legalicenla():
    """End-to-end regression for the Legalícenla intro:

    whisperX heard the intro chorus as 'Le realizan la × 3' at 0:17/0:19/0:22
    (correct timing, wrong text). Canonical lyrics start with 'Legalícenla × 3'.
    With Jaccard-only scoring (pre-2026-05-25 behaviour) the anchor missed,
    drift_streak triggered, the function returned [] and reconcile bailed —
    leaving the editor at 'first lyric @ 0:45'.

    With phonetic scoring the lines anchor to their true timestamps and the
    canonical text replaces the mishear in the emitted segments."""
    # Three mis-heard words from whisperX at the intro chorus timestamps,
    # plus a verse word at 0:53 so the function has more than just the
    # mishear region to align against.
    words = [
        _w("le",       17.0, 17.5),
        _w("realizan", 17.5, 18.2),
        _w("la",       18.2, 18.6),
        _w("le",       19.5, 20.0),
        _w("realizan", 20.0, 20.7),
        _w("la",       20.7, 21.1),
        _w("le",       22.0, 22.5),
        _w("realizan", 22.5, 23.2),
        _w("la",       23.2, 23.6),
        _w("hubo",     53.2, 53.5),
        _w("tiempos",  53.5, 54.0),
    ]
    lines = ["Legalícenla", "Legalícenla", "Legalícenla", "Hubo tiempos"]
    segs = fa.wordstamps_to_segments(words, lines)

    # All four lines must anchor (no drift abort, no empty result).
    assert len(segs) == 4, f"expected 4 segments, got {len(segs)}: {segs}"

    # Canonical text wins on the output (this is the whole point of reconcile).
    assert [s["text"] for s in segs] == [
        "Legalícenla", "Legalícenla", "Legalícenla", "Hubo tiempos",
    ]

    # Critical: first segment lands near 0:17, NOT past 0:45 (the bug shape).
    assert 16.5 < segs[0]["start"] < 18.0, (
        f"intro chorus must anchor to 0:17 area, got {segs[0]['start']}"
    )
    # Second + third chorus repeats land at their real positions.
    assert 19.0 < segs[1]["start"] < 20.5
    assert 21.5 < segs[2]["start"] < 23.0
    # Verse anchors past the intro chorus.
    assert segs[3]["start"] > 50.0


def test_is_non_retryable_padding_size():
    """The original [1,2,1] padding crash. Sigue siendo deterministic."""
    err = Exception("Padding size should be less than the corresponding input dimension")
    assert fa._is_non_retryable(err) is True


def test_is_non_retryable_tensor_120_bug_2026_05_26():
    """LIVE PROD INCIDENT 2026-05-26: cureau crashea con
    `Expected 2D or 3D (batch mode) tensor ... got: [1, 2, 0]` sobre
    Sin Gamulán y Mujer Amante. Sin este match, el caller reintentaba
    3× (~90s) antes de fallar al synced-direct fallback. El operador
    veía 'Transcribiendo 0% — tomando más tiempo de lo normal'. Con
    el match, FA bail-out en attempt 1 (<5 s) y synced-direct dispara
    inmediato."""
    err = Exception(
        "Expected 2D or 3D (batch mode) tensor with possibly 0 batch "
        "size and other non-zero dimensions for input, but got: [1, 2, 0]"
    )
    assert fa._is_non_retryable(err) is True


def test_is_non_retryable_real_lyric_text_not_affected():
    """Sanity: una excepción que mencione palabras como 'expected' en
    contexto normal no debe matchear. La regla `expected 2d or 3d` es
    suficientemente específica."""
    err1 = Exception("Connection timeout")
    err2 = Exception("expected response but got 500")  # NO debe matchear
    err3 = Exception("audio file expected MP3 format")  # NO debe matchear
    assert fa._is_non_retryable(err1) is False
    assert fa._is_non_retryable(err2) is False
    assert fa._is_non_retryable(err3) is False


def test_phonetic_does_not_rescue_genuinely_unrelated_text():
    """The phonetic path must NOT match lines that don't acoustically resemble
    the canonical. Otherwise we'd happily anchor random whisperX noise to any
    canonical line and emit garbage timestamps with confident-looking text."""
    words = [_w(f"ruido{i}", i, i + 0.4) for i in range(25)]
    lines = ["primera linea cantada", "segunda linea distinta",
             "tercera estrofa nueva", "cuarta parte final",
             "quinta y ultima aqui"]
    # Same expectation as test_wordstamps_aborts_on_sustained_drift —
    # phonetic must not lower the bar so far that drift-abort stops firing
    # on random noise.
    assert fa.wordstamps_to_segments(words, lines) == []


# ── Chunked aligner: pure helpers (no network) ──────────────────────────────

def test_plan_windows_uniform_when_no_vad():
    # No VAD signal → uniform ~target windows over the whole duration.
    w = fa._plan_windows(120.0, None, target_s=30.0, overlap_s=3.0)
    assert len(w) >= 3
    assert w[0][0] == 0.0
    assert w[-1][1] <= 120.0 + 0.01
    # consecutive windows overlap
    assert w[1][0] < w[0][1]


def test_plan_windows_groups_voiced_regions():
    # A stem-like VAD signal with real gaps → windows land in the gaps and
    # group several regions up to ~target_s.
    vr = [(0.0, 5.0), (8.0, 13.0), (40.0, 45.0), (48.0, 53.0)]
    w = fa._plan_windows(60.0, vr, target_s=30.0, overlap_s=2.0)
    assert 1 <= len(w) <= 4
    # the big gap 13..40 should produce a window break (two groups)
    assert len(w) >= 2


def test_plan_windows_full_mix_single_region_falls_back_to_uniform():
    # A full mix's VAD returns one giant region covering ~all the song; the
    # fraction gate must reject it and tile uniformly instead of making 1 huge
    # window.
    vr = [(0.0, 223.0)]
    w = fa._plan_windows(223.0, vr, target_s=30.0, overlap_s=3.0)
    assert len(w) >= 5


def test_line_expected_time_voiced_proportional():
    # Lines spread over CUMULATIVE voiced time, not wall-clock: with a long
    # late gap, the last line maps before the gap, not at the very end.
    vr = [(0.0, 20.0), (100.0, 120.0)]  # 40 s voiced over 120 s
    t_first = fa._line_expected_time(0, 4, 120.0, vr)
    t_last = fa._line_expected_time(3, 4, 120.0, vr)
    assert 0.0 <= t_first <= 20.0
    assert 100.0 <= t_last <= 120.0


def test_window_cores_partition_at_overlap_midpoints():
    windows = [(0.0, 12.0), (10.0, 22.0), (20.0, 30.0)]
    cores = fa._window_cores(windows, 30.0)
    assert cores[0][0] == 0.0
    assert cores[-1][1] == 30.0
    # cores are contiguous (one window's hi == next window's lo)
    assert abs(cores[0][1] - cores[1][0]) < 1e-6


def test_interpolate_missing_fills_linearly():
    lines = ["a", "b", "c", "d", "e"]
    final = [
        {"start": 0.0, "end": 1.0, "text": "a"},
        None,
        None,
        {"start": 9.0, "end": 10.0, "text": "d"},
        {"start": 12.0, "end": 13.0, "text": "e"},
    ]
    out = fa._interpolate_missing(final, lines, 15.0)
    assert all(s is not None for s in out)
    # b, c interpolated between 0 and 9 → 3.0 and 6.0
    assert abs(out[1]["start"] - 3.0) < 1e-6
    assert abs(out[2]["start"] - 6.0) < 1e-6
    assert out[1].get("interp") and out[2].get("interp")


def test_snap_starts_to_vocal_onsets_pins_to_onset_within_cap():
    segs = [
        {"start": 12.4, "end": 14.0, "text": "late by ~0.2s"},
        {"start": 30.0, "end": 31.0, "text": "no onset near → unchanged"},
        {"start": 5.1, "end": 6.0, "text": "snap to 5.0"},
    ]
    vr = [(5.0, 8.0), (12.2, 15.0), (50.0, 52.0)]
    out = fa.snap_starts_to_vocal_onsets(segs, vr, max_snap_s=2.0)
    by_text = {s["text"]: s["start"] for s in out}
    assert abs(by_text["late by ~0.2s"] - 12.2) < 1e-6   # snapped to onset
    assert abs(by_text["snap to 5.0"] - 5.0) < 1e-6
    assert abs(by_text["no onset near → unchanged"] - 30.0) < 1e-6  # > cap, kept


def test_snap_no_op_without_regions():
    segs = [{"start": 1.0, "end": 2.0, "text": "x"}]
    assert fa.snap_starts_to_vocal_onsets(segs, []) == segs


def test_assemble_windows_rejects_dead_window_and_interpolates():
    # Two windows; the second "failed" (None words). The first aligns its
    # lines; the dead window's lines are interpolated, NOT stolen.
    lines = ["uno dos", "tres cuatro", "cinco seis", "siete ocho"]
    windows = [(0.0, 10.0), (9.0, 20.0)]
    line_map = [[0, 1], [2, 3]]
    w0 = [
        {"word": "uno", "start": 0.5, "end": 1.0},
        {"word": "dos", "start": 1.0, "end": 1.5},
        {"word": "tres", "start": 4.0, "end": 4.5},
        {"word": "cuatro", "start": 4.5, "end": 5.0},
    ]
    final, covered = fa._assemble_windows(
        windows, line_map, [w0, None], lines, 20.0,
    )
    assert covered == 2
    assert len(final) == 4
    # lines 2,3 interpolated (window 2 dead) → flagged
    interp = [s for s in final if s.get("interp")]
    assert len(interp) == 2
