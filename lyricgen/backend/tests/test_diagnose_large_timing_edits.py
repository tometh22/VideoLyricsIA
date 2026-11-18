"""Clasificación de ediciones grandes y atribución de cambios entre versiones."""
from __future__ import annotations

import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "diagnose_large_timing_edits", BACKEND / "scripts" / "diagnose_large_timing_edits.py",
)
large = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(large)


def test_classify_flags_each_kind_of_edit():
    row = {"approved_start": 1.0, "machine_start": 1.0, "approved_end": 3.0,
           "machine_end": 2.4, "text_changed": True}
    flags = large.classify(row)
    assert flags["end_gt_500"] and flags["timing_gt_500"] and flags["text_edit"]
    assert not flags["start_edit"]


def test_repeated_texts_normalizes_case_and_punctuation():
    counts = large.repeated_texts([{"text": "Oh, amor"}, {"text": "oh amor!"}, {"text": "otra"}])
    assert counts["oh amor"] == 2 and counts["otra"] == 1


def test_changed_rows_detects_text_timing_and_insertions():
    before = [{"_id": "a", "text": "uno", "start": 0, "end": 1},
              {"_id": "b", "text": "dos", "start": 2, "end": 3}]
    after = [{"_id": "a", "text": "uno", "start": 0, "end": 1},
             {"_id": "b", "text": "dos", "start": 2, "end": 3.6},
             {"_id": "c", "text": "tres", "start": 4, "end": 5}]
    assert sorted(large._changed_rows(before, after)) == [1, 2]
