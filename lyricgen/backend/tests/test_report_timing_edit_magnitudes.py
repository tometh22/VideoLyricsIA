"""La magnitud de cada edición de timing se mide por borde y por línea."""
from __future__ import annotations

import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_timing_edit_magnitudes", BACKEND / "scripts" / "report_timing_edit_magnitudes.py",
)
magnitudes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(magnitudes)


def _line(job, machine, approved, *, text_changed=False, deleted=False):
    return {
        "job_id": job, "alignment_complete": True, "deleted": deleted,
        "matched": not deleted, "text_changed": text_changed,
        "machine_start": machine[0], "machine_end": machine[1],
        "approved_start": approved[0] if approved else None,
        "approved_end": approved[1] if approved else None,
    }


def test_shares_and_small_only_edits():
    lines = [
        _line("a", (1.0, 2.0), (1.0, 2.0)),                     # sin edición
        _line("a", (3.0, 4.0), (3.12, 4.0)),                    # solo inicio, 120 ms
        _line("a", (5.0, 6.0), (5.0, 5.6)),                     # solo fin, 400 ms
        _line("a", (7.0, 8.0), (7.1, 8.0), text_changed=True),  # texto + 100 ms
        _line("b", (1.0, 2.0), None, deleted=True),             # baja
        _line("b", (3.0, 4.0), (3.0, 4.03)),                    # ruido < 50 ms
    ]

    report = magnitudes.summarize(lines)

    assert report["start"]["edits"] == 2
    assert report["start"]["below_150ms"] == 1.0
    assert report["end"]["below_150ms"] == 0.0 and report["end"]["below_500ms"] == 1.0
    assert report["edited_lines_excluding_inserted"] == 4
    assert report["only_edit_was_timing_below_150ms"]["lines"] == 1
    assert report["jobs_whose_timing_edits_are_all_below"]["500ms"] == 1.0
    assert report["jobs_whose_timing_edits_are_all_below"]["150ms"] == 0.5
    assert report["jobs_without_timing_edits"] == 0.5
