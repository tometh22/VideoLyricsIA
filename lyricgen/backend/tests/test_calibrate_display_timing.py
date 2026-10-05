"""La simulación de timing reproduce las cuatro mutaciones del emisor.

El inicio crudo es la primera palabra y el fin crudo la última: ningún
adelanto, snap ni hold mueve las palabras, así que desde ahí se puede
re-simular cualquier combinación de parámetros.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BACKEND / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate = _load("calibrate_display_timing")
signals = _load("evaluate_line_signals")


def _seg(text, start, end, w0, w1, score=0.9, **extra):
    return {"text": text, "start": start, "end": end, "_id": text,
            "words": [{"word": text, "start": w0, "end": w1, "score": score}], **extra}


def _job(machine, approved, timing_source="ctc_align", job_id="job1"):
    return {"dataset": "staging", "job_id": job_id, "timing_source": timing_source,
            "live": False, "machine_quality": "transcription",
            "machine": machine, "approved": approved}


def _sim(job, beats=None):
    lines = calibrate.build_lines(job, "unregistered")
    return lines, calibrate.Simulator(lines, beats)


def test_whisperx_lines_get_both_leads_and_ctc_lines_only_polish():
    wx = _job([_seg("a", 9.84, 11.0, 10.0, 11.0)], [_seg("a", 9.84, 11.0, 10.0, 11.0)],
              timing_source="whisperx")
    ctc = _job([_seg("a", 9.92, 11.0, 10.0, 11.0)], [_seg("a", 9.92, 11.0, 10.0, 11.0)])

    _, wx_sim = _sim(wx)
    _, ctc_sim = _sim(ctc)
    current = dict(calibrate.CANDIDATES["current"])

    assert wx_sim.run(**current)[0][0] == pytest.approx(9.84)
    assert ctc_sim.run(**current)[0][0] == pytest.approx(9.92)
    single = dict(calibrate.CANDIDATES["single_lead_80"])
    assert wx_sim.run(**single)[0][0] == pytest.approx(9.92)


def test_lead_never_crosses_previous_line_end():
    machine = [_seg("a", 0.0, 10.0, 0.0, 9.98), _seg("b", 10.0, 12.0, 10.0, 12.0)]
    lines, sim = _sim(_job(machine, machine))

    start, _ = sim.run(**calibrate.CANDIDATES["current"])

    assert start[1] == pytest.approx(9.99)  # fin anterior + 10 ms


def test_hold_extends_to_next_line_minus_air_and_never_shortens():
    machine = [_seg("a", 0.92, 1.5, 1.0, 1.5), _seg("b", 1.72, 3.0, 1.8, 3.0)]
    lines, sim = _sim(_job(machine, machine))

    _, end = sim.run(**calibrate.CANDIDATES["current"])
    assert end[0] == pytest.approx(1.71)  # inicio siguiente (1,72) − 10 ms

    _, end_air = sim.run(**{**calibrate.CANDIDATES["current"], "hold_gap_ms": 300})
    assert end_air[0] == pytest.approx(1.5)  # el aire no recorta la última palabra


def test_observed_clamp_is_kept_as_a_floor():
    # El job adelanta 80 ms, pero la segunda línea salió sin adelanto: el
    # segmento anterior terminaba más tarde que su última palabra.
    machine = [
        _seg("a", 0.92, 1.0, 1.0, 1.0),
        _seg("b", 4.92, 5.5, 5.0, 5.5),
        _seg("c", 8.0, 9.0, 8.0, 9.0),
        _seg("d", 11.92, 12.5, 12.0, 12.5),
    ]
    lines, sim = _sim(_job(machine, machine))

    start, _ = sim.run(**calibrate.CANDIDATES["current"])

    assert start[2] == pytest.approx(8.0)


def test_beat_snap_only_moves_lines_without_reliable_words():
    machine = [
        _seg("a", 9.84, 11.0, 10.0, 11.0, score=0.2),
        _seg("b", 19.84, 21.0, 20.0, 21.0, score=0.9),
    ]
    lines, sim = _sim(_job(machine, machine, timing_source="whisperx"), beats={"job1": [9.88, 19.88]})

    with_snap, _ = sim.run(**calibrate.CANDIDATES["current"])
    without, _ = sim.run(**{**calibrate.CANDIDATES["current"], "snap_ms": 0})

    assert with_snap[0] != pytest.approx(without[0])
    assert with_snap[1] == pytest.approx(without[1])


def test_edit_simulation_counts_removed_and_created_per_boundary():
    machine = np.array([1.0, 2.0, 3.0])
    approved = np.array([1.2, 2.0, 3.0])
    simulated = np.array([1.2, 2.3, 3.0])
    mask = np.ones(3, dtype=bool)

    edits = calibrate.timing_edits(simulated, machine, machine, machine, approved, machine, mask)

    assert edits["start"] == {"edited_now": 1, "edited_sim": 1, "removed": 1, "created": 1,
                              "net_reduction_share": 0.0}
    tolerant = calibrate.timing_edits(simulated, machine, machine, machine, approved, machine,
                                      mask, tolerance_start=0.5, tolerance_end=0.5)
    assert tolerant["start"]["created"] == 0


def test_signal_table_separates_text_and_timing_errors():
    # Fines = última palabra + hold de 0,5 s, como los deja el pipeline.
    machine = [
        _seg("uno", 0.92, 2.5, 1.0, 2.0, ctc_min_score=0.2),
        _seg("dos", 2.92, 4.5, 3.0, 4.0, ctc_min_score=0.9),
        _seg("tres", 4.92, 6.0, 5.0, 6.0, ctc_min_score=0.9),
    ]
    approved = [
        _seg("uno!", 0.92, 2.5, 1.0, 2.0, _id="uno"),
        _seg("dos", 3.5, 4.5, 3.0, 4.0),
        _seg("tres", 4.92, 6.0, 5.0, 6.0),
    ]
    lines = calibrate.build_lines(_job(machine, approved), "unregistered")
    rows = signals.build_table(lines, {}, params=calibrate.CANDIDATES["current"],
                               timing_error_s=0.15)["rows"]

    # "uno!" normaliza igual que "uno": no es error de texto.
    assert [(r["text_error"], r["timing_error"]) for r in rows] == [
        (False, False), (False, True), (False, False)]
    table = signals.Table(rows)
    metrics = table.metrics(table.flag("ctc_min_score", 0.5))
    assert metrics["timing"]["recall"] == 0.0
    assert metrics["any"]["overflag"] == pytest.approx(0.5)


def test_frozen_windows_flag_by_index_or_overlap():
    segment = {"text": "x"}
    by_index = signals.line_signals(segment, start=10, end=12, index=3,
                                    windows=[{"start": 50, "end": 60, "segment_indices": [3]}])
    by_time = signals.line_signals(segment, start=10, end=12, index=0,
                                   windows=[{"start": 11, "end": 20, "segment_indices": []}])
    outside = signals.line_signals(segment, start=10, end=12, index=0,
                                   windows=[{"start": 13, "end": 20, "segment_indices": []}])
    unknown = signals.line_signals(segment, start=10, end=12, index=0, windows=None)

    assert by_index["unsafe_window"] and by_time["unsafe_window"]
    assert outside["unsafe_window"] is False
    assert unknown["unsafe_window"] is None
