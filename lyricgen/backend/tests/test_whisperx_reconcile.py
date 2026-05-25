"""Unit tests for whisperx_reconcile."""

import whisperx_reconcile as wr


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def _wx_seg(start, end, text, words):
    return {"start": start, "end": end, "text": text, "words": words}


def test_reconcile_replaces_wx_text_with_reference():
    # WhisperX heard "Despes de tanto bagar" (mondegreens) but lrclib's
    # reference text is the clean "Después de tanto vagar". After reconcile,
    # the segment carries the REFERENCE text with WHISPERX timing.
    words = [
        _w("Despes", 53.0, 53.3), _w("de", 53.3, 53.4),
        _w("tanto", 53.4, 53.7), _w("bagar", 53.7, 54.2),
        _w("por", 54.2, 54.5), _w("las", 54.5, 54.7),
        _w("kayes", 54.7, 56.4),
        _w("La", 58.0, 58.5), _w("ciudad", 58.5, 58.9),
        _w("te", 58.9, 59.0), _w("pareze", 59.0, 59.5),
        _w("tan", 59.5, 59.8), _w("gris", 59.8, 60.5),
    ]
    wx_segs = [_wx_seg(53.0, 60.5, "Despes de tanto bagar por las kayes La ciudad te pareze tan gris", words)]
    reference = (
        "Después de tanto vagar por las calles\n"
        "La ciudad te parece tan gris\n"
        "Mejor hacerse un viaje al campo\n"
        "Y sentirse libre para poder sentir"
    )
    out = wr.reconcile(wx_segs, reference, min_coverage=0.25)
    # Coverage check: at least the first 2 lines should be reconciled.
    assert out is not None
    assert len(out) >= 2
    # Text comes from REFERENCE, not whisperX.
    assert out[0]["text"] == "Después de tanto vagar por las calles"
    assert out[1]["text"] == "La ciudad te parece tan gris"
    # Timing comes from WHISPERX.
    assert out[0]["start"] == 53.0
    assert out[1]["start"] == 58.0


def test_reconcile_returns_none_when_too_few_words():
    # Below 8 words → don't even try.
    wx_segs = [_wx_seg(0, 1, "hola mundo", [_w("hola", 0, 0.5), _w("mundo", 0.5, 1.0)])]
    assert wr.reconcile(wx_segs, "linea 1\nlinea 2\nlinea 3\nlinea 4") is None


def test_reconcile_returns_none_when_reference_too_short():
    words = [_w(f"w{i}", i, i + 0.4) for i in range(20)]
    wx_segs = [_wx_seg(0, 20, "x" * 20, words)]
    # < 4 lines reference → skip.
    assert wr.reconcile(wx_segs, "linea 1\nlinea 2") is None


def test_reconcile_returns_none_on_total_drift():
    # WhisperX words are unrelated noise → wordstamps_to_segments aborts.
    words = [_w(f"ruido{i}", i, i + 0.4) for i in range(25)]
    wx_segs = [_wx_seg(0, 25, " ".join(w["word"] for w in words), words)]
    reference = (
        "Después de tanto vagar\n"
        "La ciudad te parece tan gris\n"
        "Mejor hacerse un viaje\n"
        "Y sentirse libre\n"
        "Para poder sentir"
    )
    # Either returns None (drift abort) or thin coverage → None.
    assert wr.reconcile(wx_segs, reference) is None


def test_reconcile_recovers_legalicenla_intro_mishear():
    """End-to-end regression for the Legalícenla incident (2026-05-25).

    whisperX heard the intro chorus 'Legalícenla × 3' as 'Le realizan la × 3'
    with timestamps at 0:17 / 0:19 / 0:22. Before the phonetic-aware anchor
    (forced_align.wordstamps_to_segments) this scored Jaccard=0 and reconcile
    aborted, so the editor showed first lyric @ 0:45. After the fix, reconcile
    must replace the mishear text with canonical 'Legalícenla' AND preserve
    the 0:17 timestamps."""
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
        _w("tiempos",  54.9, 55.4),
        _w("de",       55.4, 55.6),
        _w("paz",      55.6, 56.2),
    ]
    wx_segs = [_wx_seg(
        17.0, 56.2,
        "Le realizan la Le realizan la Le realizan la Hubo tiempos de guerras tiempos de paz",
        words,
    )]
    canonical = (
        "Legalícenla\n"
        "Legalícenla\n"
        "Legalícenla\n"
        "Hubo tiempos de guerras, tiempos de paz"
    )
    out = wr.reconcile(wx_segs, canonical, min_coverage=0.5)
    assert out is not None, "reconcile must succeed — phonetic anchor catches the mishear"
    assert len(out) == 4

    # Text comes from canonical, NOT the mishear.
    assert [s["text"] for s in out] == [
        "Legalícenla", "Legalícenla", "Legalícenla",
        "Hubo tiempos de guerras, tiempos de paz",
    ]

    # Critical: intro chorus anchors near 0:17 — NOT past 0:45 (the bug shape).
    assert 16.5 < out[0]["start"] < 18.5, (
        f"intro chorus must land at 0:17 area, got {out[0]['start']}"
    )
    assert 19.0 < out[1]["start"] < 20.5
    assert 21.5 < out[2]["start"] < 23.0
    # Verse aligns past the chorus.
    assert out[3]["start"] > 50.0


def test_reconcile_passes_words_through_for_karaoke():
    # When reconciliation succeeds, per-word stamps are re-attached so the
    # frontend can still do word-level karaoke on the reconciled lines.
    words = [
        _w("hola", 0.0, 0.5), _w("mundo", 0.5, 1.0),
        _w("chau", 2.0, 2.5), _w("amigo", 2.5, 3.0),
        _w("uno", 4.0, 4.3), _w("dos", 4.3, 4.6), _w("tres", 4.6, 5.0),
        _w("cuatro", 6.0, 6.3), _w("cinco", 6.3, 6.7), _w("seis", 6.7, 7.0),
    ]
    wx_segs = [_wx_seg(0.0, 7.0, "hola mundo chau amigo uno dos tres cuatro cinco seis", words)]
    reference = (
        "hola mundo\n"
        "chau amigo\n"
        "uno dos tres\n"
        "cuatro cinco seis"
    )
    out = wr.reconcile(wx_segs, reference, min_coverage=0.5)
    assert out is not None
    assert len(out) == 4
    # First reconciled line has its 2 words re-attached.
    assert out[0].get("words") and len(out[0]["words"]) == 2
    assert out[0]["words"][0]["word"] == "hola"
    # Third line ('uno dos tres') has its 3 words.
    assert out[2].get("words") and len(out[2]["words"]) == 3
