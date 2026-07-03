"""Fallback stem↔mezcla en _maybe_ctc_retime (CTC_ALIGN_MIX_FALLBACK).

Evidencia (gold set 03/07): la declinación de CTC varía según la fuente —
Grignani/PROVENZA alinearon sobre la MEZCLA (74.5%/76% ≤0.3s) habiendo
declinado sobre el stem; Rata Blanca al revés. La corrida de 40 canciones
100% sobre mezcla dio 50.7% ≤0.3s → la mezcla está validada como fuente.

Contratos: (1) stem declina por score → retry sobre mezcla; (2) sin stem
cacheado → alinear directo sobre mezcla (antes: skip total); (3) declive
ESTRUCTURAL no reintenta (eso es del camino perf-text); (4) flag en 0
restaura el comportamiento anterior; (5) el global last_decline_reason
rancio de un job anterior no contamina la decisión.
"""
import asyncio

import ctc_align
import vocal_sep
from main import _maybe_ctc_retime


def _fake_result():
    return {"job_id": "j", "segments": [{"text": f"l{i}", "start": float(i), "end": i + 0.5}
                                        for i in range(5)]}


def _arm(monkeypatch, tmp_path, *, stem_exists, per_call):
    """per_call: lista de returns de retime_segments, en orden de llamada."""
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    calls = []
    stem_file = tmp_path / "stem.wav"
    if stem_exists:
        stem_file.write_bytes(b"x")
    monkeypatch.setattr(
        vocal_sep, "separate_vocals",
        lambda *a, **kw: str(stem_file) if stem_exists else None,
    )
    def fake_retime(audio, segs, job_id="", mix_path=None, max_skip_frac=None):
        calls.append(audio)
        return per_call[len(calls) - 1]
    monkeypatch.setattr(ctc_align, "retime_segments", fake_retime)
    return calls, str(stem_file)


def test_stem_decline_retries_on_mix(monkeypatch, tmp_path):
    retimed = [{"text": "l0", "start": 9.9, "end": 10.0}] * 5
    calls, stem = _arm(monkeypatch, tmp_path, stem_exists=True,
                       per_call=[None, retimed])
    ctc_align.last_decline_reason = ""          # decline por score, no estructural
    out = asyncio.run(_maybe_ctc_retime(_fake_result(), "/mix/audio.wav", "j"))
    assert calls == [stem, "/mix/audio.wav"]    # stem primero, mezcla después
    assert out["segments"] == retimed


def test_no_stem_aligns_directly_on_mix(monkeypatch, tmp_path):
    retimed = [{"text": "l0", "start": 9.9, "end": 10.0}] * 5
    calls, _ = _arm(monkeypatch, tmp_path, stem_exists=False, per_call=[retimed])
    out = asyncio.run(_maybe_ctc_retime(_fake_result(), "/mix/audio.wav", "j"))
    assert calls == ["/mix/audio.wav"]
    assert out["segments"] == retimed


def test_no_stem_with_flag_off_keeps_old_skip(monkeypatch, tmp_path):
    calls, _ = _arm(monkeypatch, tmp_path, stem_exists=False, per_call=[])
    monkeypatch.setenv("CTC_ALIGN_MIX_FALLBACK", "0")
    result = _fake_result()
    out = asyncio.run(_maybe_ctc_retime(result, "/mix/audio.wav", "j"))
    assert out is result and calls == []        # comportamiento pre-cambio


def test_structural_decline_does_not_retry_mix(monkeypatch, tmp_path):
    def fake_retime(audio, segs, job_id="", mix_path=None, max_skip_frac=None):
        fake_retime.calls.append(audio)
        ctc_align.last_decline_reason = "structural"
        return None
    fake_retime.calls = []
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.delenv("CTC_ALIGN_PERF_TEXT", raising=False)
    stem_file = tmp_path / "stem.wav"; stem_file.write_bytes(b"x")
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *a, **kw: str(stem_file))
    monkeypatch.setattr(ctc_align, "retime_segments", fake_retime)
    result = _fake_result()
    out = asyncio.run(_maybe_ctc_retime(result, "/mix/audio.wav", "j"))
    assert fake_retime.calls == [str(stem_file)]  # una sola llamada, sin retry
    assert out is result


def test_stale_structural_reason_does_not_block_mix(monkeypatch, tmp_path):
    """Sin stem, el global quedó en 'structural' de un job ANTERIOR:
    la decisión usa estado local del call, así que la mezcla corre igual."""
    retimed = [{"text": "l0", "start": 9.9, "end": 10.0}] * 5
    calls, _ = _arm(monkeypatch, tmp_path, stem_exists=False, per_call=[retimed])
    ctc_align.last_decline_reason = "structural"   # rancio
    out = asyncio.run(_maybe_ctc_retime(_fake_result(), "/mix/audio.wav", "j"))
    assert calls == ["/mix/audio.wav"]
    assert out["segments"] == retimed


def test_retimed_segments_get_lead_in(monkeypatch, tmp_path):
    """Bug de composición #801×CTC: la cascada aplica lead_in en
    _emit_segments, pero el retime de CTC pisa esos segmentos con onsets
    frescos — el wrapper debe re-aplicar el lead para que ambas mejoras
    convivan."""
    retimed = [{"text": f"l{i}", "start": 10.0 + i * 3, "end": 11.0 + i * 3}
               for i in range(5)]
    calls, stem = _arm(monkeypatch, tmp_path, stem_exists=True,
                       per_call=[[dict(s) for s in retimed]])
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.4")
    out = asyncio.run(_maybe_ctc_retime(_fake_result(), "/mix/audio.wav", "j"))
    # primer start: 10.0 − 0.4 = 9.6 (aire de sobra)
    assert out["segments"][0]["start"] == 9.6
    # todos adelantados respecto del onset crudo de CTC
    assert all(o["start"] < r["start"]
               for o, r in zip(out["segments"], retimed))


def test_decline_path_does_not_double_apply_lead(monkeypatch, tmp_path):
    """Si CTC declina, los segmentos de la cascada (que YA traen lead)
    pasan intactos — sin segunda aplicación."""
    calls, _ = _arm(monkeypatch, tmp_path, stem_exists=True, per_call=[None, None])
    ctc_align.last_decline_reason = ""
    monkeypatch.setenv("LYRIC_LEAD_IN_S", "0.4")
    result = _fake_result()
    starts_before = [s["start"] for s in result["segments"]]
    out = asyncio.run(_maybe_ctc_retime(result, "/mix/audio.wav", "j"))
    assert [s["start"] for s in out["segments"]] == starts_before
