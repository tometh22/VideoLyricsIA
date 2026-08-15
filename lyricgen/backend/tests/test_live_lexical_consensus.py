import live_lexical_consensus as llc


REFERENCE = """Hoy temprano estuve pensando en vos
Pasó el tiempo y ahora me siento mejor
Oh no, ya no te extraño
Si estás lejos de mí
Oh no, no te hice daño
Y te alejaste de mí
Real, uoo uou
"""


def _seg(start, text):
    return {"start": start, "end": start + 3, "text": text}


def test_pericos_safe_word_corrections_preserve_live_structure_and_timing():
    source = [
        _seg(13.35, "Muy temprano estuve pensando en vos."),
        _seg(38.93, "No te hice daño."),
        _seg(43.67, "Si estás lejos de mí."),
        _seg(47.77, "Oh, no, no, no, no, no, no, oh, no, no te hice daño."),
        _seg(55.93, "Y te alejas de mí."),
        _seg(62.20, "Real, real."),
    ]
    out, stats = llc.correct_segments(source, REFERENCE)

    assert len(out) == len(source)
    assert [(s["start"], s["end"]) for s in out] == [
        (s["start"], s["end"]) for s in source
    ]
    assert out[0]["text"] == "Hoy temprano estuve pensando en vos"
    assert out[1]["text"] == "No te hice daño."  # never inserts “Oh no”
    assert out[3]["text"] == source[3]["text"]   # never deletes chant tokens
    assert out[4]["text"] == "Y te alejaste de mí"
    assert out[5]["text"] == "Real, real."       # short live ad-lib stays audio-first
    assert stats["lexical_substitutions"] == 2


def test_different_live_line_is_never_replaced_by_studio_catalogue():
    source = [_seg(10, "Improvisamos otra canción esta noche")]
    out, stats = llc.correct_segments(source, REFERENCE)
    assert out == source
    assert stats["lines_corrected"] == 0


def test_near_tied_different_reference_lines_decline():
    reference = "Hoy puedo verte bien\nHoy puedo verte mal"
    source = [_seg(10, "Hoy puedo verte ya")]
    out, stats = llc.correct_segments(source, reference)
    assert out == source
    assert stats["declined_ambiguous"] == 1


def _words(text, start=10.0, step=0.45):
    return [
        {"word": token, "start": start + i * step,
         "end": start + i * step + 0.3}
        for i, token in enumerate(text.split())
    ]


def test_independent_witness_must_support_each_catalogue_substitution():
    source = [_seg(10, "Muy temprano estuve pensando en vos")]
    corrected, _ = llc.correct_segments(source, REFERENCE)
    accepted = llc.verify_corrections(
        corrected, _words("Hoy temprano estuve pensando en vos"),
    )
    rejected = llc.verify_corrections(
        corrected, _words("Muy temprano estuve pensando en vos"),
    )
    assert accepted["verified"] == 1
    assert accepted["unverified"] == 0
    assert rejected["verified"] == 0
    assert rejected["unverified"] == 1


def test_accent_only_catalogue_cleanup_needs_no_acoustic_vote():
    source = [_seg(10, "Paso el tiempo y ahora me siento mejor")]
    corrected, _ = llc.correct_segments(source, REFERENCE)
    result = llc.verify_corrections(corrected, [])
    assert result == {"total": 0, "verified": 0, "unverified": 0, "details": []}


def test_proposal_does_not_mutate_until_independent_witness_agrees():
    source = [{
        **_seg(10, "Muy temprano estuve pensando en vos"),
        "words": _words("Muy temprano estuve pensando en vos"),
    }]
    proposed, stats = llc.propose_segments(source, REFERENCE)
    assert proposed[0]["text"].startswith("Muy temprano")
    assert proposed[0]["live_lexical_suggestion"].startswith("Hoy temprano")
    assert stats["lines_proposed"] == 1

    accepted, accepted_stats = llc.apply_verified_proposals(
        proposed, _words("Hoy temprano estuve pensando en vos"),
    )
    rejected, rejected_stats = llc.apply_verified_proposals(
        proposed, _words("Muy temprano estuve pensando en vos"),
    )
    assert accepted[0]["text"].startswith("Hoy temprano")
    assert accepted[0]["words"][0]["word"] == "Hoy"
    assert accepted_stats["applied"] == 1
    assert rejected[0]["text"].startswith("Muy temprano")
    assert rejected_stats["declined"] == 1


def test_neighbor_line_outside_adaptive_pad_cannot_verify_opposite_word():
    candidate = [{
        "start": 43.78, "end": 46.18,
        "text": "Si estás lejos de mí",
        "live_lexical_original": "Si estás cerca de mí",
        "live_lexical_corrected": True,
    }]
    # The corrected word appears only in the following row at 47.65s.
    witness = _words("Si estás cerca de mí", start=43.8)
    witness += _words("Si estás lejos de mí", start=47.65)
    result = llc.verify_corrections(candidate, witness)
    assert result["unverified"] == 1
