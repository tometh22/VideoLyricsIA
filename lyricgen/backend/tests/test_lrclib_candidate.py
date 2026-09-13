"""Fase 2 (2026-09-13): lrclib como candidato de referencia GATEADO cuando la
planilla de campaña no trae letra. Contrato:
  - flag CAMPAIGN_LRCLIB_CANDIDATE_ENABLED (default off) → sin flag, nada cambia.
  - Si la planilla trae letra (candidate), lrclib NO se consulta.
  - Candidato con el mismo binding que la planilla (text_sha256 +
    source_audio_sha256) → pasa el chequeo de binding de
    _maybe_apply_catalog_reference y se somete a la MISMA atestación.
  - Vivos / <4 líneas / fetch roto → None (best-effort, nunca levanta).
"""
import asyncio
import hashlib

import pytest

import transcription_worker as tw


TEXT = "primera frase de prueba\nsegunda estrofa del ejemplo\nultima linea del canto\ncuarta linea final"
AUDIO_SHA = "a" * 64


def _asr_result():
    return {"segments": [
        {"start": 0.0, "end": 2.0, "text": "primera frase de prueba", "words": []},
        {"start": 2.0, "end": 4.0, "text": "segunda estrofa del ejemplo", "words": []},
        {"start": 4.0, "end": 6.0, "text": "ultima linea del canto", "words": []},
        {"start": 6.0, "end": 7.0, "text": "cuarta linea final", "words": []},
    ], "reference_lyrics": ""}


@pytest.fixture
def lrclib(monkeypatch):
    calls = {"n": 0}

    def _fake_fetch(artist, song, db=None, audio_duration=None):
        calls["n"] += 1
        return {"plain": TEXT, "synced": None, "duration": 7}
    monkeypatch.setattr("pipeline._fetch_lrclib", _fake_fetch)
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    return calls


def _cand(**kw):
    args = dict(artist="Artist", title="Track", source_audio_sha256=AUDIO_SHA,
                audio_path="/tmp/x.wav", live=False)
    args.update(kw)
    return asyncio.run(tw._maybe_lrclib_candidate(
        args.pop("artist"), args.pop("title"), args.pop("source_audio_sha256"), **args))


def test_flag_off_never_fetches(monkeypatch, lrclib):
    monkeypatch.delenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", raising=False)
    assert _cand() is None
    assert lrclib["n"] == 0


def test_candidate_has_sheet_contract_and_audio_binding(monkeypatch, lrclib):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    cand = _cand()
    assert cand["status"] == "candidate"
    assert cand["source_kind"] == "lrclib"
    assert cand["source_audio_sha256"] == AUDIO_SHA
    assert cand["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert cand["text"] == TEXT
    assert lrclib["n"] == 1


@pytest.mark.parametrize("kw", [dict(live=True), dict(artist=""), dict(title="")])
def test_live_or_missing_identity_skips(monkeypatch, lrclib, kw):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    assert _cand(**kw) is None
    assert lrclib["n"] == 0


def test_short_or_broken_fetch_is_none(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    monkeypatch.setattr("pipeline._fetch_lrclib", lambda *a, **k: {"plain": "una\ndos", "synced": None})
    assert _cand() is None
    def _boom(*a, **k):
        raise RuntimeError("lrclib down")
    monkeypatch.setattr("pipeline._fetch_lrclib", _boom)
    assert _cand() is None


def test_synced_lrc_is_flattened_to_plain(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    synced = "\n".join(f"[00:0{i}.00] linea numero {i}" for i in range(5))
    monkeypatch.setattr("pipeline._fetch_lrclib", lambda *a, **k: {"plain": "", "synced": synced})
    cand = _cand()
    assert cand["text"].splitlines() == [f"linea numero {i}" for i in range(5)]


def _effective(reference, monkeypatch, *, required=True):
    return asyncio.run(tw._effective_catalog_reference(
        reference, artist="Artist", title="Track", source_audio_sha256=AUDIO_SHA,
        audio_path="/tmp/x.wav", live=False, reference_required=required))


def test_sheet_candidate_wins_and_lrclib_is_not_consulted(monkeypatch, lrclib):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    sheet = {"status": "candidate", "source_kind": "google_sheet", "text": TEXT}
    ref, status = _effective(sheet, monkeypatch)
    assert ref is sheet and status == "candidate"
    assert lrclib["n"] == 0


@pytest.mark.parametrize("sheet", [None, {"status": "absent", "source_kind": "none"}])
def test_missing_sheet_falls_back_to_lrclib_when_reference_required(monkeypatch, lrclib, sheet):
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    ref, status = _effective(sheet, monkeypatch)
    assert ref["source_kind"] == "lrclib"
    assert status == ("absent" if sheet else "none")
    # Sin reference_required (subida interactiva) no se toca nada.
    ref2, _ = _effective(sheet, monkeypatch, required=False)
    assert ref2 is sheet


def test_lrclib_candidate_passes_binding_and_reaches_attestation(monkeypatch, lrclib):
    """El candidato entra por la MISMA puerta que la planilla: binding OK y
    luego atestación acústica. Con texto idéntico al ASR el gate lo acepta y
    el aligner se invoca con content_source=catalog_reference."""
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    cand = _cand()
    seen = {}

    async def aligner(result, audio_path, job_id, text, *, content_source, enabled):
        seen["content_source"] = content_source
        out = dict(result)
        out["anchor_alignment"] = {"status": "applied"}
        out["timing_source"] = "anchor_ctc"
        return out

    out = asyncio.run(tw._maybe_apply_catalog_reference(
        _asr_result(), "/tmp/x.wav", "job1", cand, AUDIO_SHA, live=False, aligner=aligner))
    summary = out["catalog_reference"]
    assert summary["source_kind"] == "lrclib"
    assert summary["status"] not in ("invalidated", "rejected"), summary
    assert summary["used"] is True
    assert seen["content_source"] == "catalog_reference"


def test_lrclib_other_version_is_rejected_by_attestation(monkeypatch):
    """Letra de otra versión (estrofas que el audio no tiene) → rechazada,
    igual que una planilla equivocada. lrclib nunca es autor sin gate."""
    monkeypatch.setenv("CAMPAIGN_LRCLIB_CANDIDATE_ENABLED", "1")
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    other = "\n".join(f"estrofa completamente distinta numero {i} con palabras nuevas" for i in range(8))
    monkeypatch.setattr("pipeline._fetch_lrclib", lambda *a, **k: {"plain": other, "synced": None})
    cand = _cand()

    async def never(*args, **kwargs):
        raise AssertionError("aligner must not run for a rejected reference")

    out = asyncio.run(tw._maybe_apply_catalog_reference(
        _asr_result(), "/tmp/x.wav", "job1", cand, AUDIO_SHA, live=False, aligner=never))
    assert out["catalog_reference"]["status"] == "rejected"
    assert out["catalog_reference"]["used"] is False
    assert [s["text"] for s in out["segments"]] == [s["text"] for s in _asr_result()["segments"]]
