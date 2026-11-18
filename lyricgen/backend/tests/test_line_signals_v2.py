"""Fase 5.2: señales por línea que hoy se tiran, persistidas sin tocar nada."""
from __future__ import annotations

import line_signals_v2
from transcription_quality import segments_hash


def _seg(text, start, end, words):
    return {"text": text, "start": start, "end": end,
            "words": [{"word": w, "start": s, "end": e, "score": sc} for w, s, e, sc in words]}


def _asr(*pairs):
    return [{"word": w, "start": s, "end": s + 0.2} for w, s in pairs]


def _song():
    return [
        _seg("hola mundo", 1.0, 2.0, [("hola", 1.0, 1.4, 0.9), ("mundo", 1.5, 2.0, 0.7)]),
        _seg("adiós amor", 20.0, 21.0, [("adiós", 20.0, 20.5, 0.8), ("amor", 20.6, 21.0, 0.6)]),
        _seg("x", 22.0, 22.05, [("x", 22.0, 22.05, 0.5)]),
    ]


def test_attach_adds_signals_without_changing_what_the_hash_measures():
    segments = _song()
    result = {"segments": segments}
    metrics = {"voiced_gap_s": 12.0}
    before = segments_hash(segments)

    wrote = line_signals_v2.attach(
        result, metrics,
        asr_words=_asr(("hola", 1.0), ("mundo", 1.5), ("ventilador", 20.0), ("bicicleta", 20.5)),
        independent_words=[],
        voiced_gaps=[{"start": 2.5, "end": 19.5, "voiced_s": 11.0}],
        voiced_gap_warn_s=10.0,
    )

    assert wrote
    assert segments_hash(result["segments"]) == before
    first, second, third = (s["line_signals_v2"] for s in result["segments"])
    assert first["schema"] == line_signals_v2.SCHEMA
    assert first["asr_text_ratio"]["evaluated"] and first["asr_text_ratio"]["ratio"] > 0.9
    assert second["asr_text_ratio"]["ratio"] < 0.4
    assert first["voiced_gap_after_s"] == 11.0 and second["voiced_gap_before_s"] == 11.0
    assert first["word_score"] == {"median": 0.8, "min": 0.7, "n": 2}
    assert first["timing_validation"]["checked"] and not first["timing_validation"]["flagged"]
    assert third["truncated"] and not first["truncated"]
    assert first["independent_text_ratio"] is None
    song = metrics["line_signals_v2"]
    assert song["voiced_gap_breaker"]["fired"] is True
    assert song["lines_truncated"] == 1 and song["lines_text_ratio_below_0_4"] == 1


def test_attach_never_writes_lyric_text_into_metrics():
    result = {"segments": _song()}
    metrics: dict = {}
    line_signals_v2.attach(result, metrics, asr_words=[], independent_words=[],
                           voiced_gaps=[], voiced_gap_warn_s=10.0)
    assert "hola" not in repr(metrics)


def test_short_windows_are_marked_unevaluated_not_flagged():
    segments = [_seg("hola mundo", 1.0, 2.0, [("hola", 1.0, 1.4, 0.9)])]
    ratio = line_signals_v2.text_ratio(segments[0], _asr(("hola", 1.0)))
    assert ratio == {"evaluated": False, "window_words": 1}


def test_consensus_row_has_numbers_and_stream_names_only():
    row = line_signals_v2.consensus_row(
        3, "qw_1", {"agreement": 0.91, "sources": ["stem", "primary"],
                    "texts": {"stem": "letra secreta"}},
        {"agreement": 0.5}, True,
    )
    assert row == {"segment_index": 3, "window_id": "qw_1", "agreement": 0.91,
                   "agreement_without_lora": 0.5, "passed": True,
                   "sources": ["stem", "primary"]}


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("EVIDENCE_PERSIST_V2", raising=False)
    assert not line_signals_v2.enabled()
    monkeypatch.setenv("EVIDENCE_PERSIST_V2", "1")
    assert line_signals_v2.enabled()


def test_quality_worker_persists_consensus_rows_in_audit_log(db):
    import uuid

    from database import AuditLog
    from quality_jobs import _persist_line_consensus_v2

    job_id = uuid.uuid4().hex[:12]
    _persist_line_consensus_v2(job_id, _song(), [{"segment_index": 0, "agreement": 0.9}])
    row = db.query(AuditLog).filter(AuditLog.action == "evidence.line_consensus_v2").order_by(
        AuditLog.id.desc()).first()
    assert row.detail["job_id"] == job_id
    assert row.detail["segments_hash"] == segments_hash(_song())
    db.delete(row)
    db.commit()
