"""Fin de línea = fin de la voz (voice_end_timing), con energía sintética."""
from __future__ import annotations

import copy

import numpy as np
import pytest

import voice_end_timing as ve

HOP = 0.01


def envelope(voice, duration=20.0, offset=0.0, floor=1e-3, verified=True):
    """``voice``: lista de (desde, hasta, nivel) en tiempo del ORIGINAL.

    La energía se escribe en el reloj del stem (``t + offset``), como llega
    del R2: un stem que llega tarde tiene la voz corrida hacia después.
    """
    frames = int(round((duration + max(offset, 0.0)) / HOP)) + 1
    energy = np.full(frames, floor, dtype=np.float32)
    for start, end, level in voice:
        a = int(round((start + offset) / HOP))
        b = int(round((end + offset) / HOP))
        energy[a:b] = level
    return ve.VoiceEnvelope(
        energy=energy, hop_s=HOP, offset_s=offset, duration_s=duration,
        clock={"verified": verified, "offset_s": offset if verified else None},
    )


def seg(start, word_start, word_end, end=None, **extra):
    return {
        "start": start, "end": word_end + 0.5 if end is None else end,
        "text": "la nota",
        "words": [{"word": "la", "start": start, "end": word_start},
                  {"word": "nota", "start": word_start, "end": word_end}],
        **extra,
    }


# --- Regla ------------------------------------------------------------------

def test_sustained_note_extends_to_release_plus_pad():
    # Voz sostenida hasta 5,0 s; la última palabra "termina" en 3,0.
    env = envelope([(1.0, 5.0, 1.0)])
    end, evidence = ve.line_voice_end(2.5, 3.0, 9.0, env)
    assert evidence["status"] == "released"
    assert end == pytest.approx(5.0 - HOP + ve.END_PAD_S, abs=0.011)


def test_voice_cut_right_after_word_trims_the_fixed_hold():
    env = envelope([(1.0, 3.0, 1.0)])
    end, evidence = ve.line_voice_end(2.5, 3.0, 9.0, env)
    assert evidence["status"] == "released"
    # Fin = 3,0 + 0,2 (menos que el hold fijo de 0,5).
    assert end == pytest.approx(3.0 + ve.END_PAD_S, abs=0.011)


def test_short_dip_under_gap_is_not_a_release():
    # Bajón de 80 ms (< 0,10 s) a mitad de la nota: la voz sigue.
    env = envelope([(1.0, 4.0, 1.0), (4.08, 6.0, 1.0)])
    end, evidence = ve.line_voice_end(2.5, 3.0, 9.0, env)
    assert end == pytest.approx(6.0 - HOP + ve.END_PAD_S, abs=0.011)


def test_threshold_is_relative_to_the_anchor():
    # La cola baja al 40 % del ancla (sigue siendo voz) y después al 20 %.
    env = envelope([(1.0, 3.0, 1.0), (3.0, 4.0, 0.40), (4.0, 7.0, 0.20)])
    end, _ = ve.line_voice_end(2.5, 3.0, 12.0, env)
    assert end == pytest.approx(4.0 - HOP + ve.END_PAD_S, abs=0.011)


def test_capped_by_next_line_minus_gap():
    env = envelope([(1.0, 8.0, 1.0)])
    end, _ = ve.line_voice_end(2.5, 3.0, 4.0, env)
    assert end == pytest.approx(4.0 - ve.NEXT_LINE_GAP_S)


def test_search_stops_at_four_seconds_after_last_word():
    env = envelope([(1.0, 15.0, 1.0)])
    end, evidence = ve.line_voice_end(2.5, 3.0, None, env)
    assert evidence["status"] == "censored"
    assert end <= 3.0 + ve.MAX_EXTENSION_S + ve.END_PAD_S + 1e-6
    assert end >= 3.0 + ve.MAX_EXTENSION_S - 0.05


def test_floor_is_the_last_word_when_next_line_overlaps():
    env = envelope([(1.0, 5.0, 1.0)])
    end, _ = ve.line_voice_end(2.5, 3.0, 2.9, env)
    assert end == 3.0


def test_last_line_is_capped_by_audio_duration():
    env = envelope([(17.0, 21.0, 1.0)], duration=19.5)
    end, _ = ve.line_voice_end(18.5, 19.0, None, env)
    assert end == pytest.approx(19.5)


def test_inaudible_anchor_falls_back():
    # La canción tiene voz fuerte, pero la última palabra cae en silencio.
    env = envelope([(1.0, 5.0, 1.0)], duration=8.0)
    end, evidence = ve.line_voice_end(5.5, 6.0, 7.5, env)
    assert end is None
    assert evidence["status"] == "fallback_inaudible_anchor"


def test_zero_energy_anchor_on_sparse_stem_falls_back():
    """Revisión #1645: con el stem casi mudo el piso inaudible puede ser 0 y un
    ancla en silencio pasaba; el umbral 0 trataba todo como voz y alargaba la línea."""
    env = envelope([(0.0, 0.5, 1.0)], duration=20.0, floor=0.0)
    end, evidence = ve.line_voice_end(2.0, 3.0, 12.0, env)
    assert end is None
    assert evidence["status"] == "fallback_inaudible_anchor"


def test_too_short_anchor_window_falls_back():
    env = envelope([(1.0, 5.0, 1.0)])
    end, evidence = ve.line_voice_end(2.99, 3.01, 9.0, env)
    assert end is None
    assert evidence["status"] == "fallback_no_anchor_window"


def test_late_stem_clock_is_corrected():
    # El stem llega 25 ms tarde: sin corregir, el fin saldría 25 ms tarde.
    late = envelope([(1.0, 4.0, 1.0)], offset=0.025)
    aligned = envelope([(1.0, 4.0, 1.0)])
    end_late, _ = ve.line_voice_end(2.5, 3.0, 9.0, late)
    end_aligned, _ = ve.line_voice_end(2.5, 3.0, 9.0, aligned)
    assert end_late == pytest.approx(end_aligned, abs=0.011)
    unverified = ve.VoiceEnvelope(
        energy=late.energy, hop_s=HOP, offset_s=0.0, duration_s=20.0,
        clock={"verified": False},
    )
    end_raw, _ = ve.line_voice_end(2.5, 3.0, 9.0, unverified)
    assert end_raw == pytest.approx(end_aligned + 0.025, abs=0.011)


# --- plan / run ---------------------------------------------------------------

def song():
    return [
        seg(1.0, 2.5, 3.0),                       # nota sostenida hasta 5,0
        seg(6.0, 7.0, 7.5),                       # voz cortada en 7,5
        seg(9.0, 10.0, 10.5, locked=True),        # bloqueada
        seg(12.0, 13.5, 14.0),                    # última palabra en silencio
    ]


def song_env():
    return envelope([(1.0, 5.0, 1.0), (6.0, 7.5, 1.0), (9.0, 12.0, 1.0)],
                    duration=16.0)


def test_off_does_nothing(monkeypatch):
    monkeypatch.delenv("VOICE_END_TIMING_MODE", raising=False)
    segments = song()
    out, report = ve.run(segments, song_env())
    assert out is segments and report is None


@pytest.mark.parametrize("value", ["", "bogus", "APPLY ", "Observe"])
def test_mode_is_normalized(monkeypatch, value):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", value)
    expected = value.strip().lower() if value.strip().lower() in ve.MODES else "off"
    assert ve.mode() == expected


def test_observe_records_evidence_without_touching_segments(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "observe")
    segments = song()
    frozen = copy.deepcopy(segments)
    out, report = ve.run(segments, song_env())
    assert out == frozen and segments == frozen
    assert report["status"] == "observed" and report["applied_count"] == 0
    assert report["would_extend"] == 1 and report["would_trim"] == 1
    assert report["locked"] == 1
    assert report["fallback_inaudible_anchor"] == 1
    lines = {e["index"]: e for e in report["line_evidence"]}
    assert lines[0]["proposed_end"] == pytest.approx(5.19, abs=0.011)
    assert lines[1]["proposed_end"] == pytest.approx(7.69, abs=0.011)
    assert 2 not in lines


def test_apply_moves_only_end_and_never_locked_lines(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    segments = song()
    frozen = copy.deepcopy(segments)
    out, report = ve.run(segments, song_env())
    assert segments == frozen
    assert report["status"] == "applied" and report["applied_count"] == 2
    assert out[0]["end"] == pytest.approx(5.19, abs=0.011)
    assert out[1]["end"] == pytest.approx(7.69, abs=0.011)
    assert out[2] == frozen[2]                      # bloqueada
    assert out[3] == frozen[3]                      # sin evidencia: hold actual
    for before, after in zip(frozen, out):
        for key in ("start", "text", "words"):
            assert after[key] == before[key]
    adjustment = out[0]["voice_end_adjustment"]
    assert adjustment["original_end"] == 3.5
    assert adjustment["policy_version"] == ve.POLICY_VERSION
    # Idempotente: volver a correr no mueve nada.
    again, second = ve.run(out, song_env())
    assert second["applied_count"] == 0
    assert [s["end"] for s in again] == [s["end"] for s in out]


def test_eligible_restricts_lines(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    out, report = ve.run(song(), song_env(), eligible=[1])
    assert report["eligible"] == 1 and report["applied_count"] == 1
    assert out[0]["end"] == 3.5 and out[1]["end"] != 8.0


def test_stale_word_clock_is_not_touched(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    stale = seg(1.0, 2.5, 3.0, end=2.0)        # palabras después del fin
    out, report = ve.run([stale], song_env())
    assert report["invalid_word_clock"] == 1 and out == [stale]


def test_missing_envelope_is_reported(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    segments = song()
    out, report = ve.run(segments, None)
    assert out is segments
    assert report["status"] == "skipped" and report["reason"] == "cached_stem_unavailable"


def test_summary_drops_line_evidence(monkeypatch):
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "observe")
    _, report = ve.run(song(), song_env())
    assert "line_evidence" not in ve.summary(report)
    assert ve.summary(None) is None


# --- Envolvente real sobre audio sintético ------------------------------------

def test_load_voice_envelope_measures_late_stem_clock(tmp_path):
    import soundfile as sf

    rate = ve.SAMPLE_RATE
    rng = np.random.default_rng(7)
    seconds = 12
    t = np.arange(seconds * rate) / rate
    # "Voz": ráfagas de ruido (autocorrelación sin ambigüedad, a diferencia
    # de un tono puro) y una "banda" independiente en la mezcla.
    voice = np.zeros_like(t)
    for start in np.arange(0.5, seconds - 1, 1.5):
        mask = (t >= start) & (t < start + 1.0)
        voice[mask] = 0.4 * rng.standard_normal(int(mask.sum()))
    mix = voice + 0.06 * rng.standard_normal(len(t))
    delay = int(0.025 * rate)
    stem = np.concatenate([np.zeros(delay), voice])[: len(voice)]
    sf.write(tmp_path / "mix.wav", mix.astype(np.float32), rate)
    sf.write(tmp_path / "stem.wav", stem.astype(np.float32), rate)
    env = ve.load_voice_envelope(str(tmp_path / "stem.wav"), str(tmp_path / "mix.wav"))
    assert env.clock["verified"] is True
    assert env.offset_s == pytest.approx(0.025, abs=0.002)
    assert env.hop_s == pytest.approx(0.01)
    assert env.duration_s == pytest.approx(seconds, abs=0.01)
    no_mix = ve.load_voice_envelope(str(tmp_path / "stem.wav"))
    assert no_mix.offset_s == 0.0 and no_mix.clock["verified"] is False


# --- Pipeline: precedencia ----------------------------------------------------

def _result():
    return {"segments": song(), "postpass_stats": {}}


def test_finalizer_apply_changes_fresh_transcription(monkeypatch, tmp_path):
    import transcription_worker as tw

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setattr(ve, "envelope_for_audio", lambda path: (song_env(), None))
    source = _result()
    out = tw._finalize_voice_end_timing(source, "audio.wav", fresh_transcription=True)
    report = out["postpass_stats"]["voice_end_timing"]
    assert report["applied_count"] == 2 and report["mutated_segments"] is True
    assert out["segments"][0]["end"] == pytest.approx(5.19, abs=0.011)
    assert source["segments"][0]["end"] == 3.5


def test_finalizer_only_observes_existing_document(monkeypatch):
    import transcription_worker as tw

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setattr(ve, "envelope_for_audio", lambda path: (song_env(), None))
    source = _result()
    out = tw._finalize_voice_end_timing(source, "audio.wav", fresh_transcription=False)
    report = out["postpass_stats"]["voice_end_timing"]
    assert out["segments"] == source["segments"]
    assert report["applied_count"] == 0 and report["reason"] == "fresh_transcription_required"
    assert report["would_change"] == 2


def test_finalizer_skips_reused_approved_document(monkeypatch):
    import transcription_worker as tw

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setattr(ve, "envelope_for_audio",
                        lambda path: pytest.fail("must not decode"))
    source = {**_result(), "approved_text_reuse": {"used": True}}
    out = tw._finalize_voice_end_timing(source, "audio.wav", fresh_transcription=True)
    assert out["postpass_stats"]["voice_end_timing"]["reason"] == "approved_document_reused"
    assert out["segments"] == source["segments"]


def test_finalizer_off_is_a_no_op(monkeypatch):
    import transcription_worker as tw

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "off")
    monkeypatch.setattr(ve, "envelope_for_audio",
                        lambda path: pytest.fail("must not decode"))
    source = _result()
    assert tw._finalize_voice_end_timing(source, "a.wav", fresh_transcription=True) is source


def test_apply_supersedes_vocal_continuity(monkeypatch):
    import transcription_worker as tw
    import vocal_continuity as vc

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "apply")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setattr(vc, "process_vocal_continuity",
                        lambda *a: pytest.fail("continuity must not run"))
    out = tw._finalize_vocal_continuity(_result(), "a.wav", fresh_transcription=True)
    report = out["postpass_stats"]["vocal_continuity"]
    assert report["reason"] == "superseded_by_voice_end_timing"
    assert report["applied_count"] == 0


def test_observe_keeps_vocal_continuity_running(monkeypatch):
    import transcription_worker as tw
    import vocal_continuity as vc

    monkeypatch.setenv("VOICE_END_TIMING_MODE", "observe")
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "shadow")
    seen = []
    monkeypatch.setattr(vc, "process_vocal_continuity",
                        lambda result, *a: seen.append(1) or result)
    tw._finalize_vocal_continuity(_result(), "", fresh_transcription=True)
    assert seen == [1]


def test_apply_supersedes_stable_pitch_tail(monkeypatch, tmp_path):
    import transcription_worker as tw
    import timing_review_suggestions
    import pipeline
    import vocal_sep

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"x")
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "off")
    monkeypatch.setenv("STABLE_PITCH_TAIL_ENABLED", "1")
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *a: 16.0)
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *a, **kw: str(stem))
    monkeypatch.setattr(timing_review_suggestions, "load_acoustic_track",
                        lambda *a: pytest.fail("pitch tail must not run"))
    result = {**_result(), "_asr_words": [{"word": "la", "start": 1.0, "end": 2.5}]}
    out = tw._medir_cobertura_final(result, "job", None, str(audio), strip_internal=False)
    tail = out["postpass_stats"]["stable_pitch_tail"]
    assert tail["reason"] == "superseded_by_voice_end_timing"


# --- Paridad de la flota ------------------------------------------------------

def test_mode_is_part_of_fleet_timing_parity(monkeypatch):
    from observability import runtime_timing_config, runtime_timing_config_effective

    monkeypatch.delenv("VOICE_END_TIMING_MODE", raising=False)
    assert runtime_timing_config()["voice_end_timing_mode"] == "off"
    assert runtime_timing_config_effective()["voice_end_timing_mode"]["value"] == ve.mode()
    monkeypatch.setenv("VOICE_END_TIMING_MODE", " Observe ")
    assert runtime_timing_config()["voice_end_timing_mode"] == "observe" == ve.mode()
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "bogus")
    assert runtime_timing_config()["voice_end_timing_mode"] == "off" == ve.mode()
    effective = runtime_timing_config_effective()["voice_end_timing_mode"]
    assert effective["origin"] == "invalid" and effective["value"] == "off"


def test_mode_is_a_shared_railway_timing_key():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "railway_share_pipeline_config.py"
    spec = importlib.util.spec_from_file_location("railway_share", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "VOICE_END_TIMING_MODE" in module.TIMING_KEYS


# --- Re-anclado ----------------------------------------------------------------

def test_reanchor_retimed_indices_skip_locked_and_unchanged():
    import reanchor_hold

    previous = [{"start": 1.0, "end": 2.0}, {"start": 3.0, "end": 4.0},
                {"start": 5.0, "end": 6.0}]
    merged = [{"start": 1.1, "end": 2.0}, {"start": 3.0, "end": 4.0},
              {"start": 5.2, "end": 6.1, "locked": True}]
    assert reanchor_hold.retimed_indices(merged, previous) == [0]


@pytest.mark.parametrize("value", ["observe", "apply"])
def test_reanchor_endpoint_applies_voice_end_only_in_apply(client, monkeypatch, value):
    from tests.conftest import auth
    from tests.test_reanchor import SEGS, _db_segments, _make_user, _mock_align_ok, _seed_job

    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.setenv("VOICE_END_TIMING_MODE", value)
    monkeypatch.delenv("REANCHOR_HOLD_MODE", raising=False)
    # Voz de "primera" hasta 1,6 s; la línea re-anclada termina en 2,1.
    monkeypatch.setattr(ve, "envelope_for_audio",
                        lambda path: (envelope([(0.5, 1.6, 1.0)], duration=10.0), None))
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])
    _mock_align_ok(monkeypatch)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    stats = res.json()["voice_end"]
    assert "line_evidence" not in stats
    assert stats["mode"] == value and stats["would_change"] == 1
    by_text = {s["text"]: s for s in _db_segments(job_id)}
    first = by_text["primera linea corregida"]
    locked = by_text["segunda linea corregida"]
    assert locked["start"] == 2.0 and locked["end"] == 4.0   # bloqueada intacta
    assert first["start"] == 0.5
    if value == "apply":
        assert stats["applied_count"] == 1
        assert first["end"] == pytest.approx(1.79, abs=0.011)
    else:
        assert stats["applied_count"] == 0 and first["end"] == 2.1


@pytest.mark.parametrize("has_stem", [True, False])
def test_reanchor_voice_end_wins_over_hold_where_it_has_evidence(client, monkeypatch, has_stem):
    import main as main_mod
    from tests.conftest import auth
    from tests.test_reanchor import SEGS, _db_segments, _make_user, _seed_job

    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    monkeypatch.setenv("LYRIC_HOLD_S", "0.5")
    monkeypatch.delenv("LYRIC_MIN_GAP_MS", raising=False)
    found = (envelope([(0.5, 1.7, 1.0)], duration=10.0), None)
    monkeypatch.setattr(ve, "envelope_for_audio",
                        lambda path: found if has_stem else (None, "cached_stem_unavailable"))

    async def _align(result, audio_path, job_id, anchor_lyrics):
        # Cada línea re-anclada termina en su última palabra (como el CTC).
        rows = []
        for k, base in enumerate((0.5, 2.6, 4.7, 6.9)):
            rows.append({"start": base, "end": base + 1.0, "text": SEGS[k]["text"],
                         "words": [{"word": "w", "start": base, "end": base + 1.0,
                                    "score": 0.9}]})
        return {**result, "segments": rows, "timing_source": "anchor_ctc"}

    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _align)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    first = {s["text"]: s for s in _db_segments(job_id)}["primera linea corregida"]
    if has_stem:
        # La voz sigue hasta 1,7: fin 1,89 (el hold habría dado 1,99).
        assert first["end"] == pytest.approx(1.89, abs=0.011)
        assert body["voice_end"]["applied_count"] >= 1
    else:
        # Sin stem la regla se abstiene y queda el hold de REANCHOR_HOLD_MODE.
        assert body["voice_end"]["reason"] == "cached_stem_unavailable"
        assert first["end"] == pytest.approx(1.99)


# --- prefetch del stem en transcripciones nuevas ---------------------------

def test_prefetch_calls_separation_once_and_cleans_temp(monkeypatch, tmp_path):
    import transcription_worker as tw
    import vocal_sep
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"s")
    calls = []
    monkeypatch.setattr(vocal_sep, "separate_vocals",
                        lambda p, *a, **k: calls.append(k) or str(stem))
    assert tw._prefetch_voice_end_stem(str(audio)) is True
    assert len(calls) == 1 and not calls[0].get("cache_only") and not stem.exists()


def test_prefetch_failure_is_false_and_never_raises(monkeypatch, tmp_path):
    import transcription_worker as tw
    import vocal_sep
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(vocal_sep, "separate_vocals", boom)
    assert tw._prefetch_voice_end_stem(str(audio)) is False


def test_finalizer_reports_stem_unavailable_after_attempt(monkeypatch):
    import transcription_worker as tw
    import vocal_sep
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setattr(ve, "envelope_for_audio", lambda p: (None, "cached_stem_unavailable"))
    monkeypatch.setattr(vocal_sep, "separate_vocals",
                        lambda *a, **k: pytest.fail("el finalizador no separa"))
    source = _result()
    out = tw._finalize_voice_end_timing(source, "a.wav", fresh_transcription=True,
                                        stem_attempted=True)
    rep = out["postpass_stats"]["voice_end_timing"]
    assert rep["status"] == "skipped" and rep["reason"] == "stem_unavailable_after_attempt"
    assert out["segments"] == source["segments"]


def test_finalizer_without_attempt_keeps_cached_reason(monkeypatch):
    import transcription_worker as tw
    monkeypatch.setenv("VOICE_END_TIMING_MODE", "apply")
    monkeypatch.setattr(ve, "envelope_for_audio", lambda p: (None, "cached_stem_unavailable"))
    out = tw._finalize_voice_end_timing(_result(), "a.wav", fresh_transcription=True)
    assert out["postpass_stats"]["voice_end_timing"]["reason"] == "cached_stem_unavailable"
