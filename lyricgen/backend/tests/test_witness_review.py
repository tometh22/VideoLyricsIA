"""Testigo whisper-1 contra la pantalla en estudio (WITNESS_STUDIO_COMPARE_MODE).

Datos sintéticos con la forma de las dos demos que motivaron el cambio
(16-11-2026): Mc Kevinho (repetición omitida) y Toni Garrido (cola mal oída).
"""
from types import SimpleNamespace

import pytest

import witness_review
from lyric_review import build_review, required_for_scope
from lyric_review_sources import require_resolved


def _line(text, start, end, segment_id):
    return {"text": text, "start": start, "end": end, "segment_id": segment_id}


def _words(text, start, step=0.4):
    out, t = [], start
    for token in text.split():
        out.append({"word": token, "start": round(t, 2), "end": round(t + step - 0.05, 2)})
        t += step
    return out


def _evidence(witness, *, live=False):
    evidence = {"hypotheses_by_family": [{
        "role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
        "transformation": "live_independent_verify_raw", "events": witness,
    }]}
    if live:
        # Lo que deja el camino en vivo: el testigo ya entró al veredicto.
        evidence["hypotheses_by_family"].append({
            "role": "independent", "family": "openai/whisper-1", "kind": "word_stream",
            "events": witness,
        })
        evidence["capture"] = {"independent_present": True}
        evidence["decisions"] = {"quality": {"metrics": {
            "is_live": True, "independent_witness_words": len(witness)}}}
    else:
        evidence["capture"] = {"independent_present": False}
        evidence["decisions"] = {"quality": {"metrics": {"is_live": False}}}
    return evidence


VERSE = [
    ("olha o que ela faz", 0.0),
    ("no baile funk com as amigas", 3.0),
    ("ela é muito explosiva", 6.0),
]
LAST = ("não mexe com ela não", 15.0)


def _kevinho():
    """La estrofa se canta dos veces; la pantalla la muestra una sola."""
    screen = [_line(t, s, s + 2.5, f"k{i}") for i, (t, s) in enumerate(VERSE)]
    screen.append(_line(LAST[0], LAST[1], LAST[1] + 2.5, "k3"))
    witness = [w for t, s in VERSE for w in _words(t, s)]
    witness += _words("olha o que ela faz", 9.0) + _words("no baile funk com as amigas", 11.5)
    witness += _words(LAST[0], LAST[1])
    return screen, witness


GARRIDO = [
    ("eu não sei viver sem teu amor", 20.0),
    ("tudo que eu quero é te ver", 23.0),
    ("me diz que é verdade", 26.0),
]


def _garrido(tail_heard="nem me acalmo"):
    screen = [_line(t, s, s + 2.6, f"g{i}") for i, (t, s) in enumerate(GARRIDO)]
    screen.append(_line("Minha causa", 28.8, 30.0, "g3"))
    witness = [w for t, s in GARRIDO for w in _words(t, s)]
    witness += _words(tail_heard, 28.9)
    return screen, witness


def _witness_items(review):
    return [i for i in review["items"] if i["kind"].startswith("witness_")]


@pytest.fixture(autouse=True)
def _observe(monkeypatch):
    monkeypatch.delenv("WITNESS_STUDIO_COMPARE_MODE", raising=False)


def test_default_mode_is_observe():
    assert witness_review.mode() == "observe"


def test_studio_omitted_repetition_flags_uncovered_span():
    screen, witness = _kevinho()
    review = build_review(screen, original_segments=screen, machine_evidence=_evidence(witness))
    [item] = [i for i in _witness_items(review) if i["kind"] == "witness_uncovered"]
    occ = item["occurrences"][0]
    assert 8.9 <= occ["start"] <= 9.1 and occ["end"] >= 13.0
    assert occ["line_segment_id"] == "k2"  # la línea anterior al hueco
    assert "olha o que ela faz" in occ["before"]
    assert item["listen"] is True and item["required"] is False
    assert item["sources"] == ["witness"] and item["group"] == "text"


def test_studio_misheard_tail_flags_text_mismatch():
    screen, witness = _garrido()
    review = build_review(screen, original_segments=screen, machine_evidence=_evidence(witness))
    [item] = _witness_items(review)
    assert item["kind"] == "witness_mismatch"
    occ = item["occurrences"][0]
    assert occ["line_segment_id"] == "g3"
    assert occ["before"] == occ["after"] == "Minha causa"  # nada que aplicar
    assert "nem me acalmo" in item["why"]
    assert item["required"] is False and item["listen"] is True


def test_clean_studio_job_has_no_witness_flags():
    screen, witness = _garrido(tail_heard="Minha causa")
    review = build_review(screen, original_segments=screen, machine_evidence=_evidence(witness))
    assert _witness_items(review) == []


def test_live_job_is_unchanged(monkeypatch):
    screen, witness = _kevinho()
    evidence = _evidence(witness, live=True)
    live = build_review(screen, original_segments=screen, machine_evidence=evidence)
    assert _witness_items(live) == []
    monkeypatch.setenv("WITNESS_STUDIO_COMPARE_MODE", "off")
    assert build_review(screen, original_segments=screen, machine_evidence=evidence) == live


@pytest.mark.parametrize("value", ["off", "OFF", "enforce", "nonsense"])
def test_flag_off_or_unknown_adds_nothing(monkeypatch, value):
    monkeypatch.setenv("WITNESS_STUDIO_COMPARE_MODE", value)
    for screen, witness in (_kevinho(), _garrido()):
        review = build_review(screen, original_segments=screen, machine_evidence=_evidence(witness))
        assert _witness_items(review) == []


def test_never_blocks_approval_even_in_enforce(monkeypatch):
    monkeypatch.setenv("LYRIC_REVIEW_MODE", "enforce")
    screen, witness = _kevinho()
    review = build_review(screen, original_segments=screen, machine_evidence=_evidence(witness))
    assert _witness_items(review)
    assert not [i for i in required_for_scope(review, "full") if i["kind"].startswith("witness_")]
    # Campaña batch = enforce: con sólo puntos del testigo, aprobar no se frena.
    import lyric_review_sources
    only_witness = {**review, "items": _witness_items(review)}
    monkeypatch.setattr(lyric_review_sources, "review_for_document",
                        lambda *a, **k: only_witness)
    require_resolved(None, SimpleNamespace(), SimpleNamespace(workload_class="batch"))


def test_dismissed_witness_point_stays_dismissed():
    screen, witness = _garrido()
    evidence = _evidence(witness)
    [item] = _witness_items(build_review(screen, original_segments=screen, machine_evidence=evidence))
    screen[3] = {**screen[3], "qa_dismissed": item["keys"]}
    assert _witness_items(build_review(screen, original_segments=screen,
                                       machine_evidence=evidence)) == []


def test_hallucinated_credits_and_unreliable_witness_are_ignored():
    screen, witness = _garrido(tail_heard="Minha causa")
    credits = _words("Legendas pela comunidade Amara.org", 40.0, step=1.0)
    review = build_review(screen, original_segments=screen,
                          machine_evidence=_evidence(witness + credits))
    assert _witness_items(review) == []
    # Un testigo que no se parece a la pantalla es otra versión: no acusa.
    noise = _words("la la la la la la la la la la la la la la la la", 20.0, step=0.6)
    review = build_review(screen, original_segments=screen, machine_evidence=_evidence(noise))
    assert _witness_items(review) == []


def test_without_witness_or_with_sparse_witness_nothing_is_flagged():
    screen, _ = _kevinho()
    assert _witness_items(build_review(screen, original_segments=screen,
                                       machine_evidence=_evidence([]))) == []
    sparse = _words("olha o que", 9.0)
    assert witness_review.review_items(screen, sparse, _evidence(sparse)) == []
