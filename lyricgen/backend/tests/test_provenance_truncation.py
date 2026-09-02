"""Provenance VARCHAR truncation guard (fix 2026-06-01).

Sentry "Failed to insert provenance start row: value too long for type
character varying(N)": a caller passed an over-long value for one of the
ai_provenance VARCHAR columns (step/tool_name/tool_provider/tool_version).
Because the start-row INSERT is wrapped in a best-effort try/except, the
overflow dropped the ENTIRE audit row silently — a UMG-compliance gap.

The fix truncates each field to its column limit and logs which one
overflowed, so the audit row survives AND the offending caller is
identifiable.
"""

import logging
import uuid

import pytest

from provenance import _fit_varchar, record_ai_call, _PROV_MAXLEN
from database import AIProvenance, Job, SessionLocal


# ---------------------------------------------------------------------------
# _fit_varchar (pure)
# ---------------------------------------------------------------------------

def test_fit_varchar_truncates_overlong_and_warns(caplog):
    long_step = "x" * 80  # step limit is 50
    with caplog.at_level(logging.WARNING, logger="genly.provenance"):
        out = _fit_varchar(long_step, "step", "job123")
    assert out == "x" * 50
    assert any("exceeds varchar(50)" in r.getMessage() for r in caplog.records)


def test_fit_varchar_passes_through_short_values():
    assert _fit_varchar("video_bg", "step", "j") == "video_bg"
    assert _fit_varchar("veo-3.1-fast-generate-001", "tool_name", "j") == "veo-3.1-fast-generate-001"


def test_fit_varchar_none_passes_through():
    assert _fit_varchar(None, "tool_version", "j") is None


@pytest.mark.parametrize("field,limit", list(_PROV_MAXLEN.items()))
def test_fit_varchar_respects_every_column_limit(field, limit):
    out = _fit_varchar("z" * (limit + 25), field, "j")
    assert len(out) == limit


# ---------------------------------------------------------------------------
# Integration: an over-long field must NOT drop the provenance row
# ---------------------------------------------------------------------------

def _seed_job(db, job_id):
    db.add(Job(
        job_id=job_id, user_id=1, tenant_id="prov_trunc_test",
        artist="A", filename="x.mp3", style="oscuro",
        status="processing", delivery_profile="youtube", progress=10,
    ))
    db.commit()


def test_overlong_field_still_records_the_row():
    """Before the fix this raised StringDataRightTruncation inside the
    swallowed try/except and the row was lost. Now the recorder inserts a
    truncated row and exposes a real _row_id."""
    job_id = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        _seed_job(db, job_id)
    finally:
        db.close()

    # step way over its 50-char limit (simulates the offending caller).
    recorder = record_ai_call(
        job_id=job_id,
        step="lyrics_analysis_" + "x" * 80,   # > 50
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        prompt="some prompt",
    )

    assert recorder._row_id is not None, (
        "over-long field must NOT drop the provenance row anymore"
    )

    db = SessionLocal()
    try:
        row = db.query(AIProvenance).filter(AIProvenance.id == recorder._row_id).first()
        assert row is not None
        assert len(row.step) <= 50
        assert row.step.startswith("lyrics_analysis_")
        # Bulk-delete the child before its parent. PostgreSQL correctly
        # enforces the provenance audit FK whereas SQLite does not.
        db.query(AIProvenance).filter(AIProvenance.id == row.id).delete()
        db.flush()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()
    finally:
        db.close()


def test_none_prompt_does_not_crash_hash():
    """Defensive: a None prompt must not blow up the sha256 hashing on the
    start-row insert."""
    job_id = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        _seed_job(db, job_id)
    finally:
        db.close()

    recorder = record_ai_call(
        job_id=job_id, step="video_bg", tool_name="veo",
        tool_provider="google_vertex", prompt=None,
    )
    assert recorder._row_id is not None

    db = SessionLocal()
    try:
        db.query(AIProvenance).filter(AIProvenance.id == recorder._row_id).delete()
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()
    finally:
        db.close()
