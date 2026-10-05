"""Aire mínimo antes de la línea siguiente y su prueba prospectiva A/B."""
from __future__ import annotations

import uuid

import pytest

import lead_in
import line_gap_experiment


def _seg(start, end, last_word=None, **extra):
    seg = {"start": start, "end": end, "text": "x", **extra}
    if last_word is not None:
        seg["words"] = [{"start": start, "end": last_word}]
    return seg


# --------------------------------------------------------------- lead_in


def test_default_gap_keeps_current_behaviour(monkeypatch):
    monkeypatch.delenv("LYRIC_MIN_GAP_MS", raising=False)
    segs = [_seg(0.0, 2.0, 2.0), _seg(2.2, 4.0, 4.0)]
    out = lead_in.apply_hold(segs, hold_s=0.5)
    assert out[0]["end"] == pytest.approx(2.19)        # siguiente − 10 ms
    assert lead_in.apply_hold([_seg(0.0, 2.3, 2.3), _seg(2.2, 4.0)], hold_s=0.5)[0]["end"] == 2.3


def test_gap_caps_hold_and_trims_into_the_air(monkeypatch):
    monkeypatch.setenv("LYRIC_MIN_GAP_MS", "300")
    out = lead_in.apply_hold([_seg(0.0, 2.0, 2.0), _seg(3.0, 4.0, 4.0)], hold_s=0.5)
    assert out[0]["end"] == pytest.approx(2.5)          # hay aire: hold completo
    out = lead_in.apply_hold([_seg(0.0, 2.0, 1.85), _seg(2.2, 4.0, 4.0)], hold_s=0.5)
    assert out[0]["end"] == pytest.approx(1.9)          # recorta a siguiente − 300 ms


def test_trim_never_cuts_more_than_100ms_of_the_last_word(monkeypatch):
    monkeypatch.setenv("LYRIC_MIN_GAP_MS", "300")
    out = lead_in.apply_hold([_seg(0.0, 2.1, 2.1), _seg(2.2, 4.0, 4.0)], hold_s=0.5)
    assert out[0]["end"] == pytest.approx(2.0)          # última palabra − 0,10 s


def test_trim_respects_locked_lines_short_lines_and_overlaps(monkeypatch):
    monkeypatch.setenv("LYRIC_MIN_GAP_MS", "300")
    locked = lead_in.apply_hold([_seg(0.0, 2.1, 2.1, locked=True), _seg(2.2, 4.0)], hold_s=0.5)
    assert locked[0]["end"] == 2.1
    short = lead_in.apply_hold([_seg(0.0, 0.5), _seg(0.6, 2.0)], hold_s=0.5)
    assert short[0]["end"] == pytest.approx(0.3)        # inicio + 0,3 s
    overlap = lead_in.apply_hold([_seg(0.0, 2.0), _seg(0.2, 2.5)], hold_s=0.5)
    assert overlap[0]["end"] == 2.0


def test_override_and_stats_follow_the_context(monkeypatch):
    monkeypatch.delenv("LYRIC_MIN_GAP_MS", raising=False)
    stats: dict = {}
    with lead_in.min_gap_override(300, stats):
        out = lead_in.polish([_seg(0.0, 2.0, 1.85), _seg(2.2, 4.0, 4.0)])
    assert out[0]["end"] < 2.0
    assert stats["lines_trimmed"] == 1 and stats["polish_calls"] == 1
    assert lead_in.min_gap_seconds() == 0.0


# --------------------------------------------------------------- asignación


@pytest.fixture
def clean_audit(db):
    from database import AuditLog

    def wipe():
        db.query(AuditLog).filter(AuditLog.action.in_([
            line_gap_experiment.ASSIGNED, line_gap_experiment.EXPOSURE,
        ])).delete(synchronize_session=False)
        db.commit()

    wipe()
    yield
    wipe()


def _job(db, *, tenant="universal_music", **overrides):
    from database import Job

    fields = dict(
        job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id=tenant, artist="A",
        filename="a.wav", status="processing", workload_class="batch",
        campaign_id="camp", campaign_item_id=uuid.uuid4().hex[:12],
        segments_revision=0,
    )
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def test_assignment_alternates_and_is_sticky(db, clean_audit, monkeypatch):
    monkeypatch.setenv("LYRIC_MIN_GAP_AB_ENABLED", "1")
    first, second, third = (_job(db) for _ in range(3))

    arms = [line_gap_experiment.assign(db, job)["arm"] for job in (first, second, third)]

    assert arms == ["A", "B", "A"]
    again = line_gap_experiment.assign(db, second)
    assert again["arm"] == "B" and again["gap_ms"] == 300
    monkeypatch.setenv("LYRIC_MIN_GAP_AB_ENABLED", "0")
    assert line_gap_experiment.assign(db, first)["arm"] == "A"   # reintento conserva el brazo


def test_only_new_umg_campaign_jobs_participate(db, clean_audit, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("LYRIC_MIN_GAP_AB_ENABLED", "1")
    assert line_gap_experiment.assign(db, _job(db, tenant="preflight_staging_x")) is None
    assert line_gap_experiment.assign(db, _job(db, workload_class="interactive")) is None
    assert line_gap_experiment.assign(db, _job(db, approved_at=datetime.now(timezone.utc))) is None
    assert line_gap_experiment.assign(db, _job(db, segments_revision=3)) is None
    monkeypatch.setenv("LYRIC_MIN_GAP_AB_ENABLED", "0")
    assert line_gap_experiment.assign(db, _job(db)) is None


def test_cap_stops_new_assignments(db, clean_audit, monkeypatch):
    monkeypatch.setenv("LYRIC_MIN_GAP_AB_ENABLED", "1")
    monkeypatch.setenv("LYRIC_MIN_GAP_AB_MAX_JOBS", "2")
    assert line_gap_experiment.assign(db, _job(db)) is not None
    assert line_gap_experiment.assign(db, _job(db)) is not None
    assert line_gap_experiment.assign(db, _job(db)) is None
