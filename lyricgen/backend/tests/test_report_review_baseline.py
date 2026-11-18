"""La línea de base separa lo que hizo la máquina de lo que hizo el operador.

Las revisiones ``migration`` (canonicalización al abrir el editor) no cuentan
como ediciones humanas, y el adelanto se mide contra la primera palabra,
que ningún lead mueve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_review_baseline", BACKEND / "scripts" / "report_review_baseline.py",
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _line(text, start, end, words=None, line_id=None):
    row = {"text": text, "start": start, "end": end}
    if words is not None:
        row["words"] = words
    if line_id:
        row["_id"] = line_id
    return row


def _version(revision, reason, segments, *, approved=False, minutes=0):
    return {
        "revision": revision, "reason": reason, "is_approved": approved,
        "created_at": T0 + timedelta(minutes=minutes), "segments": segments,
    }


def test_machine_version_includes_migration_before_first_human_save():
    v0 = _version(0, "transcription", [_line("a", 1.0, 2.0)])
    mig = _version(1, "migration", [_line("a", 1.0, 2.0)], minutes=1)
    human = _version(2, "manual", [_line("b", 1.0, 2.0)], minutes=2)
    late_mig = _version(3, "migration", [_line("b", 1.0, 2.0)], minutes=3)
    approved = _version(4, "approve", [_line("b", 1.0, 2.0)], approved=True, minutes=4)

    chosen = baseline.pick_versions([approved, late_mig, human, mig, v0])

    assert chosen["v0"] is v0
    assert chosen["machine"] is mig
    assert chosen["approved"] is approved
    assert chosen["migrations_total"] == 2
    assert chosen["migrations_after_human"] == 1


def test_first_approval_wins_over_later_reapproval():
    v0 = _version(0, "transcription", [_line("a", 1.0, 2.0)])
    first = _version(1, "approve", [_line("a", 1.0, 2.0)], approved=True, minutes=1)
    second = _version(2, "approve", [_line("x", 1.0, 2.0)], approved=True, minutes=9)

    assert baseline.pick_versions([second, first, v0])["approved"] is first


def test_line_edits_counts_text_timing_insert_and_delete_separately():
    before = [
        _line("uno", 1.0, 2.0, line_id="l1"),
        _line("dos", 3.0, 4.0, line_id="l2"),
        _line("tres", 5.0, 6.0, line_id="l3"),
    ]
    after = [
        _line("uno!", 1.0, 2.0, line_id="l1"),        # texto
        _line("dos", 3.0, 3.7, line_id="l2"),         # fin
        _line("nueva", 7.0, 8.0, line_id="l4"),       # alta
    ]                                                  # l3: baja

    edits = baseline.line_edits(before, after, timing_threshold_s=0.05)

    assert edits["complete"]
    assert (edits["text"], edits["timing"], edits["end"], edits["start"]) == (1, 1, 1, 0)
    assert (edits["inserted"], edits["deleted"]) == (1, 1)
    assert edits["any"]


def test_timing_jitter_below_threshold_is_not_an_edit():
    before = [_line("uno", 1.0, 2.0, line_id="l1")]
    after = [_line("uno", 1.02, 2.03, line_id="l1")]

    edits = baseline.line_edits(before, after, timing_threshold_s=0.05)

    assert not edits["any"]


def test_word_lead_measures_line_start_against_first_word():
    single = _line("a", 9.92, 11.0, words=[{"start": 10.0, "end": 10.5}])
    double = _line("b", 19.84, 21.0, words=[{"start": 20.0, "end": 20.5}])
    no_words = _line("c", 30.0, 31.0)

    assert baseline.word_leads([single, double, no_words]) == [0.08, 0.16]


def test_shifts_only_compare_lines_whose_text_survived():
    before = [_line("uno", 1.0, 2.0, line_id="l1"), _line("dos", 3.0, 4.0, line_id="l2")]
    after = [_line("uno", 1.2, 1.7, line_id="l1"), _line("DOS", 3.5, 4.0, line_id="l2")]

    assert baseline.start_shifts(before, after) == [0.2]
    assert baseline.boundary_shifts(before, after, "end") == [-0.3]


def test_live_class_prefers_persisted_metric_and_marks_inference():
    assert baseline.live_class({"metrics": {"is_live": False}}, "Tema (En Vivo)") == (False, "metric")
    assert baseline.live_class({}, "Tema (En Vivo)") == (True, "inferred")
    assert baseline.live_class(None, "Tema", "tema_estudio.wav") == (False, "inferred")


def test_live_regex_matches_main():
    from main import _looks_live

    titles = [
        "Tema (En Vivo)", "Live In Buenos Aires 2001", "Acústica", "sesion_01.wav",
        "Unplugged", "Concierto", "Directo", "Vivir", "Tema", "MTV-Unplugged_take2",
    ]
    for title in titles:
        assert baseline.looks_live(title) == _looks_live(title), title
