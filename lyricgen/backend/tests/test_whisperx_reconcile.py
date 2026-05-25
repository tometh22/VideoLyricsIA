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
