"""Fase 0 (2026-09-13): métrica '¿el texto llegó exacto al editor?'."""
import importlib.util
import os


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "report_reviewer_minutes.py")
    spec = importlib.util.spec_from_file_location("report_reviewer_minutes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_text_exact_ratio_is_one_when_reviewer_only_touched_timing():
    mod = _load()
    original = [{"text": "Hola, mundo"}, {"text": "adiós amigo"}]
    current = [{"text": "hola mundo", "start": 1.0}, {"text": "Adiós amigo!"}]
    assert mod.text_exact_ratio(original, current) == 1.0


def test_text_exact_ratio_counts_rewritten_and_inserted_lines():
    mod = _load()
    original = [{"text": "uno"}, {"text": "dos"}, {"text": "tres"}, {"text": "cuatro"}]
    current = [{"text": "uno"}, {"text": "dos reescrita"}, {"text": "tres"}, {"text": "cuatro"}, {"text": "cinco nueva"}]
    # 3 iguales sobre max(4, 5) líneas.
    assert mod.text_exact_ratio(original, current) == 0.6


def test_text_exact_ratio_none_without_lines():
    mod = _load()
    assert mod.text_exact_ratio([], []) is None
