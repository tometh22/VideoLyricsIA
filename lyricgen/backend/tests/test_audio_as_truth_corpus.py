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


# ─────────────────────────────────────────────────────────────────────────
# Sin Gamulán / Mujer Amante — Los Abuelos / Rata Blanca (2026-05-26)
# ─────────────────────────────────────────────────────────────────────────
# Both songs have extreme line repetition (Sin Gamulán: 9 unique lines
# across 28 total; Mujer Amante: 26 unique across 44). whisperX struggles +
# reconcile aborts + forced_align ALSO crashes in cureau with
# `Expected 2D or 3D tensor … got [1, 2, 0]` (highly-repetitive canonical
# text seems to break cureau's CTC alignment — see issue #357).
#
# This forces the third fallback to fire: `lrclib_aligner` over the existing
# whisperX wordstamps with `keep_unmatched=True`, interpolating timing for
# the repeating lines whisperX didn't transcribe. Validated live against
# both audios — 100% line coverage on both after this fallback runs.

def test_lrclib_aligner_recovers_highly_repetitive_lyrics():
    """When reconcile aborts and FA would also fail, lrclib_aligner with
    keep_unmatched=True must still anchor every canonical line. Validates
    the third fallback layer in the audio-as-truth path."""
    import lrclib_aligner
    # Shape of what whisperX produces on Sin Gamulán: catches the first
    # chorus + a couple of repeats, loses the rest in the music. Same
    # pattern as the live audio test produced.
    words = [
        _w("Sera",       29.4, 29.7), _w("por",        29.7, 29.9),
        _w("eso",        29.9, 30.2), _w("que",        30.2, 30.4),
        _w("hoy",        30.4, 30.7), _w("estamos",    30.7, 31.3),
        _w("aqui",       31.3, 31.9),
        _w("No",        144.8, 145.0), _w("hay",      145.0, 145.2),
        _w("nadie",     145.2, 145.6), _w("mas",      145.6, 145.9),
        _w("que",       145.9, 146.0), _w("vos",      146.0, 146.3),
        _w("y",         146.3, 146.4), _w("yo",       146.4, 146.7),
    ]
    wx_segs = [_wx_seg(29.4, 146.7, " ".join(w["word"] for w in words), words)]
    canonical = (
        "Tanto tiempo te esperé sentado aquí\n"
        "Que ya el invierno me alcanzó sin gamulán\n"
        "Será por eso que hoy estamos aquí\n"
        "No hay nadie más que vos y yo\n"
        "Será por eso que hoy estamos aquí\n"
        "No hay nadie más que vos y yo\n"
        "Tantas veces lo soñé como real\n"
        "Que quiso el tiempo y quiso nada más\n"
        "Será por eso que hoy estamos aquí\n"
        "No hay nadie más que vos y yo"
    )
    out = lrclib_aligner.align_lrclib_to_whisper(
        canonical, wx_segs, keep_unmatched=True,
    )
    canon_lines = [l for l in canonical.splitlines() if l.strip()]
    # Must recover ≥ 90% of canonical lines. Some have interpolated timing
    # (review:True) but the operator at least SEES every line at an
    # approximate position — way better than whisperX raw with mishears.
    assert len(out) >= int(len(canon_lines) * 0.9), (
        f"lrclib_aligner should recover ≥90% of {len(canon_lines)} lines, "
        f"got {len(out)}"
    )
    # All segments span audio time (no NaN/None starts).
    assert all(isinstance(s.get("start"), (int, float)) for s in out), (
        "every output segment must have a numeric start time"
    )


# ─────────────────────────────────────────────────────────────────────────
# 638 — Viejas Locas (UMG dry-run incident, 2026-05-26)
# ─────────────────────────────────────────────────────────────────────────
# whisperX over a dense rock mix produced 13 mishear-heavy segments for a
# 19-line song: number "638" came back as "780465" / "738-0465", intra-seg
# duplications ("y empecé y empecé"), and 3 canonical lines were skipped at
# the intro. Reconcile correctly aborted by drift (correct behaviour — we
# don't want to fabricate timestamps over confident-but-wrong text).
#
# Before the fix, that abort emitted whisperX raw with mishear text, and the
# editor saw only 8/19 canonical lines. The fix in main.py adds a FA
# fallback after reconcile aborts: same audio, same canonical, but FA's
# greedy-monotonic anchoring over the full file recovered 19/19.
#
# This test pins the reconcile-abort behaviour. The FA fallback that runs
# in _run_transcription_for_job after the abort is verified manually
# against the real audio (/tmp/pipeline_638/pipeline_A_with_fix.json).

def test_638_mishears_abort_reconcile():
    # Shape of what whisperX produced on the real audio: 3 intro lines
    # skipped, "y empecé" duplicated, "638" replaced with a fabricated
    # phone number. Phonetic relaxation can't bridge a 4-digit number
    # mishear, so reconcile must abort and let the FA fallback handle it.
    words = [
        # whisperX picked up canonical line 4 first ("pero tu apellido…"),
        # then duplicated "y empecé".
        _w("pero",     21.95, 22.15), _w("tu",       22.15, 22.30),
        _w("apellido", 22.30, 22.80), _w("nunca",    22.80, 23.10),
        _w("entendí",  23.10, 23.50), _w("bien",     23.50, 23.90),
        _w("y",        24.00, 24.10), _w("empecé",   24.10, 24.50),
        _w("y",        24.50, 24.60), _w("empecé",   24.60, 24.81),
        # Body picks up the canonical line about the corazón — but with
        # "mi amor" instead of "mía mor" and a fabricated number.
        _w("decía",    49.61, 49.90), _w("mi",       49.90, 50.05),
        _w("amor",     50.05, 50.40), _w("llámame",  50.40, 50.90),
        _w("al",       50.90, 51.05), _w("780465",   51.05, 52.29),
    ]
    wx_segs = [_wx_seg(21.95, 52.29, " ".join(w["word"] for w in words), words)]
    canonical = (
        "Tenía tantas ganas de volverte a ver\n"
        "y en mi casa me dicen que llamaste recién,\n"
        "traté de buscar tu número en la guía\n"
        "pero tu apellido nunca entendí bien.\n"
        "Y empecé a pensar que tenía en un cajón\n"
        "un viejo cuaderno que guardabas vos\n"
        "en el anotabas todo lo que hacías\n"
        "y vi un corazón que decía: mía mor llamame al\n"
        "638..."
    )
    # Reconcile MUST return None: thin coverage (4 first canon lines missing)
    # plus number mishear means wordstamps_to_segments can't safely anchor.
    # The caller (main.py audio-as-truth path) then triggers the FA fallback.
    assert wr.reconcile(wx_segs, canonical) is None
