"""Audio-as-truth pipeline regression corpus.

Each test reproduces the SHAPE of a real-world song failure with synthetic
wordstamps + canonical text. No Replicate, no audio files, no network — pure
data-in / data-out against `whisperx_reconcile.reconcile()` and
`forced_align.wordstamps_to_segments`.

Add a song here every time a real bug ships. The corpus grows monotonically;
each new entry locks the fix in place against future regressions.

Canon for each entry:
  * incident_id: link to the bug report / PR
  * shape: what whisperX returned (mishear text + correct timing)
  * canonical: what lrclib/Genius/Gemini brought as ground truth text
  * expectations: timestamps + texts that MUST be in the reconciled output
"""

import forced_align as fa
import whisperx_reconcile as wr


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def _wx_seg(start, end, text, words):
    return {"start": start, "end": end, "text": text, "words": words}


# ─────────────────────────────────────────────────────────────────────────
# Legalícenla — Viejas Locas (UMG dry-run incident, 2026-05-25)
# ─────────────────────────────────────────────────────────────────────────
# whisperX mis-heard the intro chorus "Legalícenla × 3" as "Le realizan la × 3"
# at 0:17/0:19/0:22 (correct timing, wrong text). With Jaccard-only scoring,
# reconcile aborted by drift and the editor showed "first lyric @ 0:45" —
# the body chorus position. With phonetic anchor + audio-as-truth, the intro
# chorus must land at 0:17 with canonical text.

def test_legalicenla_intro_chorus_lands_at_17s():
    words = [
        _w("Le",       17.0, 17.5),
        _w("realizan", 17.5, 18.2),
        _w("la",       18.2, 18.6),
        _w("Le",       19.5, 20.0),
        _w("realizan", 20.0, 20.7),
        _w("la",       20.7, 21.1),
        _w("Le",       22.0, 22.5),
        _w("realizan", 22.5, 23.2),
        _w("la",       23.2, 23.6),
        _w("Hubo",     53.2, 53.5),
        _w("tiempos",  53.5, 54.0),
        _w("de",       54.0, 54.2),
        _w("guerras",  54.2, 54.8),
    ]
    wx_segs = [_wx_seg(17.0, 54.8, " ".join(w["word"] for w in words), words)]
    canonical = (
        "Legalícenla\nLegalícenla\nLegalícenla\nHubo tiempos de guerras"
    )
    out = wr.reconcile(wx_segs, canonical, min_coverage=0.5)
    assert out is not None
    # Intro chorus anchors at its real acoustic position, not at the body chorus.
    assert 16.5 < out[0]["start"] < 18.5
    # Canonical text wins over mishear.
    assert out[0]["text"] == "Legalícenla"


# ─────────────────────────────────────────────────────────────────────────
# Cosas Mías — Andrés Calamaro (incident 2026-05-22, agus.cafisi)
# ─────────────────────────────────────────────────────────────────────────
# lrclib_synced was 35s off vs the user's audio — the canonical "Después de
# tanto vagar" was at 53s in the user's master but lrclib placed it at 88s.
# The verify gate at 0.54 confidence was borderline-accepted by the old
# threshold. PR-G eliminated lrclib_synced as a timing source; this test
# locks in that whisperX timing wins over any synced offset.

def test_cosas_mias_whisperx_timing_wins_over_synced_offset():
    # whisperX heard the line at the REAL audio position (53.0s).
    words = [
        _w("Despues", 53.0, 53.3),
        _w("de",      53.3, 53.4),
        _w("tanto",   53.4, 53.7),
        _w("vagar",   53.7, 54.2),
        _w("por",     54.2, 54.5),
        _w("las",     54.5, 54.7),
        _w("calles",  54.7, 56.4),
        _w("la",      58.0, 58.5),
        _w("ciudad",  58.5, 58.9),
        _w("te",      58.9, 59.0),
        _w("parece",  59.0, 59.5),
        _w("tan",     59.5, 59.8),
        _w("gris",    59.8, 60.5),
    ]
    wx_segs = [_wx_seg(53.0, 60.5, " ".join(w["word"] for w in words), words)]
    canonical = (
        "Después de tanto vagar por las calles\n"
        "La ciudad te parece tan gris\n"
        "Mejor hacerse un viaje al campo\n"
        "Y sentirse libre para poder sentir"
    )
    out = wr.reconcile(wx_segs, canonical, min_coverage=0.25)
    assert out is not None
    # Timing comes from audio, NOT from any stale synced source.
    assert 52.5 < out[0]["start"] < 53.5
    assert 57.5 < out[1]["start"] < 58.5


# ─────────────────────────────────────────────────────────────────────────
# Hermanos de Sangre — Almafuerte (stretched trailing word incident)
# ─────────────────────────────────────────────────────────────────────────
# cureau (and whisperX) sometimes STRETCH the last word of a line across the
# instrumental gap to the next sung line. A 3-s line ended up held 12 s,
# leaving a frozen subtitle during the instrumental. wordstamps_to_segments
# trims this — verify it still does under the new phonetic-aware path.

def test_hermanos_de_sangre_trims_stretched_trailing_word():
    words = [
        _w("Hermanos", 10.0, 10.4),
        _w("de",       10.4, 10.6),
        _w("sangre",   10.6, 11.0),
        # `eterno` was held 12 s across the instrumental into the next line.
        _w("eterno",   11.0, 23.0),
        _w("Por",      23.5, 23.8),
        _w("siempre",  23.8, 24.3),
        _w("juntos",   24.3, 24.9),
    ]
    lines = ["Hermanos de sangre eterno", "Por siempre juntos"]
    segs = fa.wordstamps_to_segments(words, lines)
    assert len(segs) == 2
    # Line 1 must be capped — end shouldn't hang past the median tail.
    # 12-s stretch detected as ballooned and replaced with normal_tail.
    assert segs[0]["end"] < 15.0, (
        f"trailing 'eterno' should not hold 12 s, got end={segs[0]['end']}"
    )
    # Line 2 anchors at its real position.
    assert 23.0 < segs[1]["start"] < 24.0


# ─────────────────────────────────────────────────────────────────────────
# Mondegreens-heavy verse (mishear + chorus repeat)
# ─────────────────────────────────────────────────────────────────────────
# Composite case: whisperX returns multiple acoustic mishears in a single
# verse ("Despes" / "bagar" / "kayes" / "pareze") AND the song has chorus
# repeats elsewhere. The phonetic anchor must rescue all the mishears
# without confusing the chorus matching.

def test_mondegreen_verse_with_chorus_repeats():
    words = [
        _w("Despes", 53.0, 53.3),
        _w("de",     53.3, 53.4),
        _w("tanto",  53.4, 53.7),
        _w("bagar",  53.7, 54.2),  # bagar → vagar
        _w("por",    54.2, 54.5),
        _w("las",    54.5, 54.7),
        _w("kayes",  54.7, 56.4),  # kayes → calles
        _w("La",     58.0, 58.5),
        _w("ciudad", 58.5, 58.9),
        _w("te",     58.9, 59.0),
        _w("pareze", 59.0, 59.5),  # pareze → parece
        _w("tan",    59.5, 59.8),
        _w("gris",   59.8, 60.5),
    ]
    wx_segs = [_wx_seg(53.0, 60.5, " ".join(w["word"] for w in words), words)]
    canonical = (
        "Después de tanto vagar por las calles\n"
        "La ciudad te parece tan gris\n"
        "Mejor hacerse un viaje al campo\n"
        "Y sentirse libre para poder sentir"
    )
    out = wr.reconcile(wx_segs, canonical, min_coverage=0.25)
    assert out is not None
    assert out[0]["text"] == "Después de tanto vagar por las calles"
    assert out[1]["text"] == "La ciudad te parece tan gris"


# ─────────────────────────────────────────────────────────────────────────
# Negative control — drift abort still fires on TRULY unrelated noise
# ─────────────────────────────────────────────────────────────────────────
# The phonetic relaxation must not soften the bar so much that random noise
# anchors to canonical lines. This guards against the "false rescue" failure
# mode where we'd happily emit fabricated timestamps with confident text.

def test_drift_abort_still_fires_on_random_noise():
    words = [_w(f"ruido{i}", i, i + 0.4) for i in range(25)]
    wx_segs = [_wx_seg(0, 25, " ".join(w["word"] for w in words), words)]
    canonical = (
        "primera linea cantada\nsegunda linea distinta\n"
        "tercera estrofa nueva\ncuarta parte final\n"
        "quinta y ultima aqui"
    )
    # Either drift abort (None) or thin coverage (None). Anything else is a
    # regression — the bar must stay high enough to reject random words.
    assert wr.reconcile(wx_segs, canonical) is None
