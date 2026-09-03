"""La cobertura no puede reportar 1,000 mientras falta letra cantada.

Caso que originó el cambio (2026-09-02, holdout del sprint): "Sisters (Live)"
de Divididos emitió 14 líneas contra 23 aprobadas, perdió 9, tuvo 62,6 s de
hueco cantado según el VAD del stem... y reportó ``audio_coverage = 1,000``,
porque esa métrica mide contra las palabras que el ASR entregó y el ASR
tampoco oyó las secciones perdidas. La omisión se autocertificaba.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_coverage import audio_coverage, voiced_coverage  # noqa: E402


def _seg(start: float, end: float) -> dict:
    return {"start": start, "end": end, "text": "linea"}


def _word(start: float, end: float) -> dict:
    return {"word": "x", "start": start, "end": end}


def test_voiced_coverage_needs_evidence_and_never_assumes_one():
    # Sin stem ni regiones no se puede afirmar ni negar pérdida.
    assert voiced_coverage([_seg(0, 10)]) is None
    assert voiced_coverage([_seg(0, 10)], regions=[]) is None


def test_full_coverage_when_every_voiced_second_is_claimed():
    regions = [(0.0, 10.0), (20.0, 30.0)]
    segments = [_seg(0, 10), _seg(20, 30)]
    assert voiced_coverage(segments, regions=regions) == 1.0


def test_missing_section_lowers_voiced_coverage():
    # Se canta en 0-10 y en 20-30, pero sólo hay cartel para el primer tramo.
    regions = [(0.0, 10.0), (20.0, 30.0)]
    value = voiced_coverage([_seg(0, 10)], regions=regions)
    assert value is not None and abs(value - 0.5) < 0.02


def test_no_lines_at_all_is_zero_not_none():
    assert voiced_coverage([], regions=[(0.0, 10.0)]) == 0.0


def test_the_sisters_case_asr_blind_spot():
    """El ASR sólo oyó lo que sí se transcribió: por palabras da 1,0."""
    regions = [(0.0, 30.0), (40.0, 100.0)]  # 90 s cantados
    segments = [_seg(0, 30)]                # sólo 30 s con cartel
    words = [_word(1, 2), _word(5, 6), _word(29, 29.5)]  # todas bajo el cartel

    assert audio_coverage(segments, words) == 1.0          # la vista vieja
    voiced = voiced_coverage(segments, regions=regions)
    assert voiced is not None and voiced < 0.4             # la vista nueva
    # La combinada (la que leen los gates) toma la peor.
    assert min(audio_coverage(segments, words), voiced) < 0.4


def test_voiced_coverage_is_consistent_with_voiced_gaps():
    """Ambas salen de las mismas regiones: si sube el hueco, baja la cobertura."""
    regions = [(0.0, 100.0)]
    poca_letra = voiced_coverage([_seg(0, 20)], regions=regions)
    mucha_letra = voiced_coverage([_seg(0, 20), _seg(30, 90)], regions=regions)
    assert poca_letra is not None and mucha_letra is not None
    assert mucha_letra > poca_letra


def test_overlapping_lines_do_not_double_count():
    regions = [(0.0, 10.0)]
    # Dos carteles solapados cubren 0-10 una sola vez, no 200%.
    assert voiced_coverage([_seg(0, 8), _seg(4, 10)], regions=regions) == 1.0
