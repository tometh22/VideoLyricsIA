"""Persistencia de señales de revisión (review_signal_records).

La revisión rápida y las propuestas de repetition_reconcile se calculaban y
se perdían. Estas pruebas fijan que se guardan sólo con el flag prendido, sin
duplicados, y que una falla al guardar nunca llega al editor ni al pipeline.
"""
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import repetition_reconcile as rr
import review_signals as rs
from database import Base, Job, ReviewSignalRecord
from lyric_review import build_review
from tests.test_repetition_reconcile import CORO, _fixture_real, _seg


def _line(text, start, end, segment_id):
    return {"text": text, "start": start, "end": end, "segment_id": segment_id}


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'signals.db'}")
    Base.metadata.create_all(engine, tables=[Job.__table__, ReviewSignalRecord.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _rows(factory):
    db = factory()
    try:
        return db.query(ReviewSignalRecord).order_by(ReviewSignalRecord.id).all()
    finally:
        db.close()


def _review_records(revision=3):
    segs = [_line("¿Cuando vuelvas?", 10, 12, "a"), _line("Cuando vuelvas", 20, 22, "b")]
    review = build_review(segs, title="")
    assert review["items"], "la revisión de prueba debe tener puntos"
    return rs.lyric_review_records(
        job_id="job123", tenant_id="umg", revision=revision,
        current_segments=segs, review=review,
    ), review


# ---------------------------------------------------------------------------
# Migración
# ---------------------------------------------------------------------------

def test_alembic_upgrade_creates_review_signal_records(tmp_path):
    db_path = tmp_path / "alembic_signals.db"
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "JWT_SECRET": "test",
        "ADMIN_PASSWORD": "test123ab",
        "ENVIRONMENT": "development",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(review_signal_records)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(review_signal_records)")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert {
        "id", "job_id", "tenant_id", "kind", "item_type", "decision", "reason",
        "editor_revision", "segments_hash", "pipeline_release", "line_index",
        "start_s", "end_s", "payload", "dedupe_key", "created_at",
    } <= columns
    assert "ix_review_signal_records_job_id" in indexes
    assert "ix_review_signal_records_kind_created" in indexes
    assert version == "c1d3e5f7a9b2"


def test_single_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["c1d3e5f7a9b2"]


# ---------------------------------------------------------------------------
# Escritor
# ---------------------------------------------------------------------------

def test_lyric_review_records_identity_and_bounds():
    records, review = _review_records()
    assert len(records) == len(review["items"])
    first = records[0]
    assert first["kind"] == "lyric_review_item"
    assert first["decision"] == "proposed"
    assert first["editor_revision"] == 3
    assert first["item_type"] == review["items"][0]["kind"]
    assert first["line_index"] == review["items"][0]["occurrences"][0]["line_index"]
    assert len(first["segments_hash"]) == 64 and len(first["dedupe_key"]) == 64
    assert first["payload"]["item_id"] == review["items"][0]["id"]

    long_review = {"items": [{
        "id": "x", "kind": "missing", "sources": ["witness", "gemini"],
        "occurrences": [{"line_index": i, "start": i, "end": i + 1,
                         "before": "a" * 5000, "after": "b" * 5000,
                         "fix": {"type": "insert", "text": "c" * 5000}}
                        for i in range(40)],
    }]}
    [rec] = rs.lyric_review_records(job_id="j", tenant_id="t", revision=1,
                                    current_segments=[], review=long_review)
    assert len(rec["payload"]["occurrences"]) <= rs._MAX_OCCURRENCES
    assert all(len(o["before"]) <= rs._MAX_TEXT for o in rec["payload"]["occurrences"])


def test_writer_dedupes_same_item_same_revision(session_factory):
    records, _ = _review_records(revision=3)
    assert rs.write_records(records, session_factory=session_factory) == len(records)
    # Recalcular lo mismo (otro proceso, otro request) no duplica.
    assert rs.write_records(records, session_factory=session_factory) == 0
    # Duplicados dentro del mismo lote tampoco.
    rs.write_records(records + records, session_factory=session_factory)
    assert len(_rows(session_factory)) == len(records)
    # Otra revisión del documento sí es otra fila.
    newer, _ = _review_records(revision=4)
    assert rs.write_records(newer, session_factory=session_factory) == len(newer)
    rows = _rows(session_factory)
    assert len(rows) == 2 * len(records)
    assert {r.editor_revision for r in rows} == {3, 4}
    assert rows[0].tenant_id == "umg" and rows[0].payload["item_id"]


def test_writer_fills_tenant_from_job(session_factory):
    db = session_factory()
    db.add(Job(job_id="jobrep", user_id=1, artist="A", song_title="T", filename="a.mp3", status="processing", tenant_id="universal_music"))
    db.commit()
    db.close()
    segs, asr = _fixture_real()
    _out, stats = rr.reconcile(segs, asr)
    records = rs.repetition_records(job_id="jobrep", segments=segs, proposals=stats["proposals"])
    assert rs.write_records(records, session_factory=session_factory) == len(records)
    assert {r.tenant_id for r in _rows(session_factory)} == {"universal_music"}


def test_writer_failure_never_propagates():
    def broken():
        raise RuntimeError("db down")

    records, _ = _review_records()
    assert rs.write_records(records, session_factory=broken) == 0


def test_flag_off_writes_nothing(monkeypatch):
    monkeypatch.delenv("REVIEW_SIGNALS_PERSIST_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(rs, "submit", lambda records: calls.append(records))
    records, review = _review_records()
    document = SimpleNamespace(job_id="job123", tenant_id="umg", revision=3,
                               current_segments=[])
    rs.record_lyric_review(document, None, review)
    segs, asr = _fixture_real()
    _out, stats = rr.reconcile(segs, asr)
    rs.record_repetition("job123", segs, stats)
    assert calls == []

    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    rs.record_lyric_review(document, None, review)
    rs.record_repetition("job123", segs, stats)
    assert len(calls) == 2 and all(calls)


def test_capture_failure_does_not_break_review(monkeypatch):
    """Si guardar explota, la revisión rápida igual llega al editor."""
    import lyric_review_sources as lrs

    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    monkeypatch.setenv("LYRIC_REVIEW_MODE", "observe")

    def boom(records):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(rs, "submit", boom)
    lrs._review_cache.clear()
    segs = [_line("¿Cuando vuelvas?", 10, 12, "zz-unique")]
    document = SimpleNamespace(job_id="j", tenant_id="t", current_segments=segs,
                               original_segments=segs, machine_evidence=None, revision=1)
    job = SimpleNamespace(job_id="j", artist="", song_title="", campaign_item_id=None,
                          tenant_id="t", workload_class="batch")

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    db = SimpleNamespace(query=lambda *a, **k: _Query())
    review = lrs.review_for_document(db, document, job)
    assert review.get("error") is None
    assert review["items"]


def test_review_for_document_submits_once_per_content(monkeypatch):
    import lyric_review_sources as lrs

    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    monkeypatch.setenv("LYRIC_REVIEW_MODE", "observe")
    calls = []
    monkeypatch.setattr(rs, "submit", lambda records: calls.append(records))
    lrs._review_cache.clear()
    segs = [_line("¿Cuando vuelvas otra vez?", 10, 12, "once")]
    document = SimpleNamespace(job_id="j2", tenant_id="t", current_segments=segs,
                               original_segments=segs, machine_evidence=None, revision=7)
    job = SimpleNamespace(job_id="j2", artist="", song_title="", campaign_item_id=None,
                          tenant_id="t", workload_class="batch")

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    db = SimpleNamespace(query=lambda *a, **k: _Query())
    lrs.review_for_document(db, document, job)
    lrs.review_for_document(db, document, job)  # caché: no se recalcula
    assert len(calls) == 1
    assert calls[0][0]["editor_revision"] == 7 and calls[0][0]["job_id"] == "j2"


def test_submit_runs_writer_in_background(monkeypatch):
    done = []
    monkeypatch.setattr(rs, "write_records", lambda records: done.append(len(records)))
    assert rs.submit([{"dedupe_key": "k"}]) is True
    rs._get_executor().submit(lambda: None).result(timeout=5)
    assert done == [1]
    assert rs.submit([]) is False


# ---------------------------------------------------------------------------
# repetition_reconcile
# ---------------------------------------------------------------------------

def test_repetition_applied_proposal_is_recorded():
    segs, asr = _fixture_real()
    _out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 1
    applied = [p for p in stats["proposals"] if p["decision"] == "applied"]
    assert len(applied) == 1
    assert applied[0]["item_type"] == "repetition_insert"
    assert applied[0]["group_text"] == CORO and applied[0]["ratio"] >= 0.7
    [rec] = [r for r in rs.repetition_records(job_id="j", segments=segs,
                                               proposals=stats["proposals"])
             if r["decision"] == "applied"]
    assert rec["kind"] == "repetition_proposal" and rec["reason"] is None
    assert 209.0 <= rec["start_s"] <= 210.5
    assert segs[rec["line_index"]]["text"] == CORO


def test_repetition_declined_proposals_keep_gate_reason():
    segs, asr = _fixture_real()
    casi = CORO + " ya"
    for ini in (120.0, 130.0, 140.0):
        segs.append(_seg(casi, ini, ini + 4.2))
    segs.sort(key=lambda s: s["start"])
    for s in segs[-3:]:
        asr.extend(s["words"])
    asr.sort(key=lambda w: w["start"])
    _out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 0
    records = rs.repetition_records(job_id="j", segments=segs, proposals=stats["proposals"])
    declined = [r for r in records if r["reason"] == "run_ambigua_entre_grupos"]
    assert declined and all(r["decision"] == "declined" for r in declined)
    assert declined[0]["payload"]["best_other_ratio"] is not None
    assert declined[0]["start_s"] is not None


def test_repetition_trimmed_orphans_are_recorded_as_declined():
    miembros = [_seg(CORO, ini, ini + 3.4, ctc_lr=-0.05) for ini in (100.0, 120.0, 140.0, 160.0)]
    asr = [w for s in miembros for w in s["words"]]
    from tests.test_repetition_reconcile import _words_de
    for ini in (108.0, 128.0, 148.0, 168.0, 176.0, 184.0):
        asr.extend(_words_de(CORO, ini))
    asr.sort(key=lambda w: w["start"])
    _out, stats = rr.reconcile(miembros, asr)
    trimmed = [p for p in stats["proposals"] if p["reason"] == "huerfanas_extra_recortadas"]
    assert len(trimmed) == 2
    assert sum(1 for p in stats["proposals"] if p["decision"] == "applied") == stats["inserted"]


def test_repetition_gate_reason_turns_applied_into_declined():
    segs, asr = _fixture_real()
    _out, stats = rr.reconcile(segs, asr)
    records = rs.repetition_records(job_id="j", segments=segs, proposals=stats["proposals"],
                                    gate_reason="mutation_not_authorized")
    assert all(r["decision"] == "declined" for r in records)
    shadow = [r for r in records if r["reason"] == "mutation_not_authorized"]
    assert len(shadow) == 1 and shadow[0]["payload"]["would_decision"] == "applied"


def test_main_wrapper_records_shadow_when_mutation_gate_closed(monkeypatch):
    import main

    monkeypatch.setenv("REPETITION_RECONCILE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda job_id: False)
    monkeypatch.setattr(rs, "job_tenant", lambda job_id: "universal_music")
    calls = []
    monkeypatch.setattr(rs, "submit", lambda records: calls.append(records))
    segs, asr = _fixture_real()
    result = {"segments": segs, "_asr_words": asr}
    out = main._maybe_repetition_reconcile(result, "jobshadow")
    assert out is result and out["segments"] is segs  # el job no cambia
    assert "postpass_stats" not in out
    assert len(calls) == 2
    assert any(r["reason"] == "mutation_not_authorized" for r in calls[0])
    # Costo de la corrida en sombra, en su propia fila.
    (run,) = calls[1]
    assert run["kind"] == rs.KIND_REPETITION_SHADOW_RUN and run["tenant_id"] == "universal_music"
    assert run["payload"]["duration_ms"] >= 0 and run["payload"]["words"] == len(asr)
    assert run["payload"]["proposals"] == len([r for r in calls[0]])

    calls.clear()
    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "0")
    main._maybe_repetition_reconcile({"segments": segs, "_asr_words": asr}, "jobshadow")
    assert calls == []


def test_main_wrapper_records_and_keeps_postpass_stats_clean(monkeypatch):
    import main

    monkeypatch.setenv("REPETITION_RECONCILE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda job_id: True)
    calls = []
    monkeypatch.setattr(rs, "submit", lambda records: calls.append(records))
    segs, asr = _fixture_real()
    out = main._maybe_repetition_reconcile({"segments": segs, "_asr_words": asr}, "jobapplied")
    assert any(s.get("repetition_recovered") for s in out["segments"])
    assert "proposals" not in out["postpass_stats"]["rep_reconcile"]
    assert any(r["decision"] == "applied" for r in calls[0])


def test_shadow_only_runs_for_umg_jobs(monkeypatch):
    import main

    monkeypatch.setenv("REPETITION_RECONCILE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_SIGNALS_PERSIST_ENABLED", "1")
    monkeypatch.setattr(main, "_quality_mutation_authorized", lambda job_id: False)
    calls = []
    monkeypatch.setattr(rs, "submit", lambda records: calls.append(records))
    segs, asr = _fixture_real()
    for tenant in ("tomas@epical.digital", "preflight_staging_x", None):
        monkeypatch.setattr(rs, "job_tenant", lambda job_id, t=tenant: t)
        main._maybe_repetition_reconcile({"segments": segs, "_asr_words": asr}, "jobother")
    assert calls == []
