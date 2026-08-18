import repetition_consensus as rc


def _row(start, text):
    return {"start": start, "end": start + 4.0, "text": text}


def test_repeated_chorus_majority_proposes_review_only_content_fix():
    segments = [
        _row(40, "Nos rodea el viento y somos libres para navegar"),
        _row(103, "Nos rompe el viento y estamos libres para navegar"),
        _row(166, "Nos rodea el viento y somos libres para navegar"),
    ]
    suggestions = rc.propose_recurrence_corrections(segments)
    assert suggestions == [{
        "segment_index": 1,
        "suggested_text": "Nos rodea el viento y somos libres para navegar",
        "occurrences": 3, "exact_support": 2, "similarity": 0.7382,
        "confidence_kind": "uncalibrated", "review": True,
        "automatic_apply_allowed": False,
    }]


def test_three_distinct_ambiguous_variants_abstain():
    segments = [
        _row(135, "Es de strobe a ver lo que se"),
        _row(151, "Estratega a ver lo que sé"),
        _row(167, "Este trope a ver lo que es"),
    ]
    assert rc.propose_recurrence_corrections(segments) == []


def test_adjacent_duplicate_rows_do_not_manufacture_recurrence_support():
    segments = [
        _row(10, "Una frase repetida en el editor"),
        _row(12, "Una frase repetida en el editor"),
        _row(40, "Otra frase distinta en el editor"),
    ]
    assert rc.propose_recurrence_corrections(segments) == []
