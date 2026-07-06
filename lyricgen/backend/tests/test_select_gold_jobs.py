"""Rewind de lyrics.segments_diff → output original de la máquina, y
scoring machine-vs-gold (scripts/select_gold_jobs.py).

El invariante importante: aplicar los audits en REVERSA (nuevo→viejo)
seteando prev_* recupera el estado pre-humano incluso cuando la misma
línea se corrigió varias veces en saves distintos (gana el prev más
viejo). Es la base del baseline contra el que se mide cualquier mejora
de alignment (p.ej. CTC_ALIGN_ENABLED).
"""
import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "select_gold_jobs.py"
_spec = importlib.util.spec_from_file_location("select_gold_jobs", _SCRIPT)
sgj = importlib.util.module_from_spec(_spec)
sys.modules["select_gold_jobs"] = sgj
_spec.loader.exec_module(sgj)


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_rewind_single_change():
    gold = [_seg(10.0, 12.0, "hola"), _seg(14.0, 16.0, "mundo")]
    audits = [{
        "changed": [{"id": "idx_1", "prev_start": 12.6, "new_start": 14.0,
                     "prev_end": 16.0, "new_end": 16.0,
                     "prev_text": "mundo", "new_text": "mundo"}],
        "reorder": [], "truncated": False,
    }]
    machine, info = sgj.rewind_segments(gold, audits)
    assert machine[1]["start"] == 12.6           # rebobinado
    assert machine[0]["start"] == 10.0           # intacto
    assert gold[1]["start"] == 14.0              # el gold no se muta
    assert info == {"truncated_saves": 0, "reorders": 0, "out_of_range": 0}


def test_rewind_multiple_saves_oldest_prev_wins():
    """La línea se corrigió dos veces (save1: 10→11, save2: 11→12).
    El rewind debe volver al 10 original, no al 11 intermedio."""
    gold = [_seg(12.0, 13.0, "x")]
    audits = [
        {"changed": [{"id": "idx_0", "prev_start": 10.0, "new_start": 11.0,
                      "prev_end": 13.0, "new_end": 13.0,
                      "prev_text": "x", "new_text": "x"}]},
        {"changed": [{"id": "idx_0", "prev_start": 11.0, "new_start": 12.0,
                      "prev_end": 13.0, "new_end": 13.0,
                      "prev_text": "x", "new_text": "x"}]},
    ]
    machine, _ = sgj.rewind_segments(gold, audits)
    assert machine[0]["start"] == 10.0


def test_rewind_flags_quality_issues():
    gold = [_seg(1.0, 2.0, "a")]
    audits = [{
        "changed": [
            {"id": "idx_9", "prev_start": 0.5},   # fuera de rango
            {"id": "weird", "prev_start": 0.5},   # id no posicional
        ],
        "reorder": [{"id": "idx_0", "from_idx": 0, "to_idx": 1}],
        "truncated": True,
    }]
    machine, info = sgj.rewind_segments(gold, audits)
    assert machine[0]["start"] == 1.0             # nada aplicado
    assert info["truncated_saves"] == 1
    assert info["reorders"] == 1
    assert info["out_of_range"] == 2


def test_score_machine_vs_gold():
    gold = [_seg(10.0, 12.0, "hola"), _seg(14.0, 16.0, "mundo"),
            _seg(20.0, 22.0, "fin")]
    machine = [_seg(10.0, 12.0, "hola"),          # intacta
               _seg(12.6, 16.0, "mundo"),          # start corrido 1.4s
               _seg(20.1, 22.0, "final")]          # 0.1s + texto cambiado
    m = sgj.score_machine_vs_gold(machine, gold)
    assert m["lines"] == 3
    assert m["lines_touched"] == 2
    assert m["start_p50"] == 0.1
    assert m["start_max"] == 1.4
    assert m["pct_start_within_tight"] == round(100 * 2 / 3, 1)  # ≤0.3s: líneas 0 y 2
    assert m["pct_start_within_loose"] == round(100 * 2 / 3, 1)
    assert m["pct_text_changed"] == round(100 * 1 / 3, 1)


def test_score_empty():
    m = sgj.score_machine_vs_gold([], [])
    assert m["lines"] == 0 and m["start_p50"] == 0.0
