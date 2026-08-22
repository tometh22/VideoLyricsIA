"""Cost and margin by product tier: lyric_veo / lyric_static / art_track.

Universal's contract prices these three tiers differently (400 lyric
videos/mes a $6 — la mitad obligada a fondo estático — + 250 Art Tracks/mes
a $4) and margin has only ever been visible aggregated by tenant/song. These
tests lock the two things that are easy to get backwards:

* Classification ORDER — `art_track` must win over `background_ai_generated`
  (an Art Track's cover is a static image, but it is still a different
  product, not a "lyric_static" video). And a job whose render_params never
  recorded the bit (pre-instrumentation) must land in `unknown`, never be
  guessed into a billable category.
* Art Track's labor sample is expected to be EMPTY — that flow never opens
  the lyrics editor, so it never emits the `editor_approved` event
  `active_edit_ms` comes from. `None`, not `0`.
"""
from datetime import datetime, timezone
import uuid

import pytest

from tests.conftest import auth

import cost_by_video_type as cbvt

PERIOD = "2031-02"
_START = datetime(2031, 2, 10, tzinfo=timezone.utc)
_TEST_TENANT = "cbvt-test-tenant"


def _uid(prefix: str) -> str:
    """Short, unique job id — Job.job_id is varchar(12)."""
    return f"{prefix}{uuid.uuid4().hex}"[:12]


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    from database import AIProvenance, Job, ProductEvent
    job_ids = [
        r[0] for r in
        db.query(Job.job_id).filter(Job.tenant_id == _TEST_TENANT).all()
    ]
    if job_ids:
        (db.query(AIProvenance).filter(AIProvenance.job_id.in_(job_ids))
           .delete(synchronize_session=False))
        (db.query(ProductEvent).filter(ProductEvent.job_id.in_(job_ids))
           .delete(synchronize_session=False))
        (db.query(Job).filter(Job.job_id.in_(job_ids))
           .delete(synchronize_session=False))
        db.commit()


def _job(db, job_id, render_params, status="done",
        created_at=_START, completed_at=_START):
    from database import Job
    db.add(Job(job_id=job_id, user_id=1, tenant_id=_TEST_TENANT, artist="A",
               filename="a.mp3", status=status, render_params=render_params,
               created_at=created_at, completed_at=completed_at))
    db.flush()


def _veo(db, job_id, n=1, summary=None, created_at=_START):
    from database import AIProvenance
    for _ in range(n):
        db.add(AIProvenance(job_id=job_id, step="video_bg",
                            tool_name="veo-3.1-fast-generate-001",
                            tool_provider="google_vertex", prompt_sent="p",
                            response_summary=summary, created_at=created_at))


def _approval(db, job_id, active_edit_ms, revision=1):
    from database import ProductEvent
    db.add(ProductEvent(
        tenant_id=_TEST_TENANT, user_id=1, job_id=job_id,
        name="editor_approved",
        properties={"revision": revision, "active_edit_ms": active_edit_ms},
        created_at=_START,
    ))


# ---------------------------------------------------------------------------
# classify_video_type — order and unknown handling
# ---------------------------------------------------------------------------

def test_art_track_wins_over_background_ai_generated():
    """An Art Track's cover is a static image (background_ai_generated is
    False), but the PRODUCT distinction is art_track — checked first."""
    assert cbvt.classify_video_type(
        {"art_track": True, "background_ai_generated": False},
    ) == cbvt.CAT_ART_TRACK


def test_veo_background_is_lyric_veo():
    assert cbvt.classify_video_type(
        {"background_ai_generated": True}) == cbvt.CAT_LYRIC_VEO


def test_static_background_is_lyric_static():
    assert cbvt.classify_video_type(
        {"background_ai_generated": False}) == cbvt.CAT_LYRIC_STATIC


@pytest.mark.parametrize("render_params", [
    None, {}, {"background_ai_generated": "yes"}, {"art_track": False},
])
def test_missing_or_malformed_field_is_unknown_not_guessed(render_params):
    """Pre-instrumentation jobs must never be forced into a billable
    category — see the module docstring on why."""
    assert cbvt.classify_video_type(render_params) == cbvt.CAT_UNKNOWN


# ---------------------------------------------------------------------------
# collect_delivered_job_ids_by_type — only DELIVERED jobs, in-period
# ---------------------------------------------------------------------------

def test_collect_groups_delivered_jobs_and_excludes_undelivered(db):
    from cost_attribution import period_bounds
    start, end = period_bounds(PERIOD)

    veo_job = _uid("cbvtv")
    static_job = _uid("cbvts")
    art_job = _uid("cbvta")
    unknown_job = _uid("cbvtu")
    rejected_job = _uid("cbvtr")

    _job(db, veo_job, {"background_ai_generated": True})
    _job(db, static_job, {"background_ai_generated": False})
    _job(db, art_job, {"art_track": True})
    _job(db, unknown_job, {})
    # Touched but never delivered — must not appear in ANY bucket.
    _job(db, rejected_job, {"background_ai_generated": True},
        status="rejected", completed_at=None)
    db.commit()

    by_type = cbvt.collect_delivered_job_ids_by_type(db, start, end)
    assert veo_job in by_type[cbvt.CAT_LYRIC_VEO]
    assert static_job in by_type[cbvt.CAT_LYRIC_STATIC]
    assert art_job in by_type[cbvt.CAT_ART_TRACK]
    assert unknown_job in by_type[cbvt.CAT_UNKNOWN]
    all_grouped = {j for jobs in by_type.values() for j in jobs}
    assert rejected_job not in all_grouped


def test_collect_respects_period_bounds(db):
    from cost_attribution import period_bounds
    start, end = period_bounds(PERIOD)

    outside = _uid("cbvto")
    _job(db, outside, {"background_ai_generated": True},
        created_at=datetime(2031, 3, 1, tzinfo=timezone.utc),
        completed_at=datetime(2031, 3, 1, tzinfo=timezone.utc))
    db.commit()

    by_type = cbvt.collect_delivered_job_ids_by_type(db, start, end)
    assert outside not in by_type[cbvt.CAT_LYRIC_VEO]


# ---------------------------------------------------------------------------
# infra_cost_by_job — billable_filter + period bound
# ---------------------------------------------------------------------------

def test_infra_cost_excludes_cache_hits_and_out_of_period_spend(db):
    from cost_attribution import period_bounds
    start, end = period_bounds(PERIOD)

    job_id = _uid("cbvtc")
    _job(db, job_id, {"background_ai_generated": True})
    _veo(db, job_id, n=1)                                  # billable: $0.80
    _veo(db, job_id, n=2, summary="cache_hit: abc")         # free
    _veo(db, job_id, n=5,
        created_at=datetime(2031, 3, 1, tzinfo=timezone.utc))  # next month
    db.commit()

    cost = cbvt.infra_cost_by_job(db, [job_id], start, end)
    assert cost[job_id] == pytest.approx(0.80)


def test_infra_cost_empty_job_list_short_circuits(db):
    assert cbvt.infra_cost_by_job(db, [], _START, _START) == {}


# ---------------------------------------------------------------------------
# labor_ms_by_job — sums real revisions, dedupes retried POSTs
# ---------------------------------------------------------------------------

def test_labor_ms_sums_revisions_and_dedupes_retried_events(db):
    job_id = _uid("cbvtl")
    _job(db, job_id, {"background_ai_generated": True})
    _approval(db, job_id, active_edit_ms=60_000, revision=1)
    _approval(db, job_id, active_edit_ms=60_000, revision=1)  # retried POST
    _approval(db, job_id, active_edit_ms=30_000, revision=2)  # reopened later
    db.commit()

    ms = cbvt.labor_ms_by_job(db, [job_id])
    assert ms[job_id] == pytest.approx(90_000)


def test_labor_ms_omits_jobs_without_editor_approved_event(db):
    job_id = _uid("cbvtn")
    _job(db, job_id, {"art_track": True})
    db.commit()

    ms = cbvt.labor_ms_by_job(db, [job_id])
    assert job_id not in ms


# ---------------------------------------------------------------------------
# build_cost_by_video_type — full report
# ---------------------------------------------------------------------------

def test_report_averages_infra_and_labor_per_category(db):
    v1, v2 = _uid("cbvV1"), _uid("cbvV2")
    s1 = _uid("cbvS1")
    a1 = _uid("cbvA1")

    _job(db, v1, {"background_ai_generated": True})
    _job(db, v2, {"background_ai_generated": True})
    _job(db, s1, {"background_ai_generated": False})
    _job(db, a1, {"art_track": True})

    _veo(db, v1, n=1)   # $0.80
    _veo(db, v2, n=2)   # $1.60
    # s1 (static) and a1 (art track) never call Veo — no provenance rows.

    _approval(db, v1, active_edit_ms=5 * 60_000)    # 5 min
    _approval(db, v2, active_edit_ms=15 * 60_000)   # 15 min
    # s1, a1: never open the lyrics editor — no editor_approved at all.
    db.commit()

    report = cbvt.build_cost_by_video_type(
        db, PERIOD, labor_rate_usd_per_hour=12.0,
        prices_usd={cbvt.CAT_LYRIC_VEO: 6.0, cbvt.CAT_ART_TRACK: 4.0},
    )
    assert report["period"] == PERIOD
    assert report["labor_rate_usd_per_hour"] == 12.0

    veo_cat = report["categories"][cbvt.CAT_LYRIC_VEO]
    assert veo_cat["delivered_count"] == 2
    assert veo_cat["infra_cost_avg_usd"] == pytest.approx(1.20)   # (.8+1.6)/2
    assert veo_cat["labor_minutes_avg"] == pytest.approx(10.0)     # (5+15)/2
    assert veo_cat["labor_cost_avg_usd"] == pytest.approx(
        (10.0 / 60.0) * 12.0)
    assert veo_cat["total_cost_avg_usd"] == pytest.approx(
        veo_cat["infra_cost_avg_usd"] + veo_cat["labor_cost_avg_usd"])
    assert veo_cat["margin_usd"] == pytest.approx(
        6.0 - veo_cat["total_cost_avg_usd"])

    static_cat = report["categories"][cbvt.CAT_LYRIC_STATIC]
    assert static_cat["delivered_count"] == 1
    assert static_cat["infra_cost_avg_usd"] == 0.0
    assert static_cat["labor_minutes_avg"] is None
    assert static_cat["labor_cost_avg_usd"] is None
    # No price passed for this tier → no margin fabricated.
    assert "margin_usd" not in static_cat

    art_cat = report["categories"][cbvt.CAT_ART_TRACK]
    assert art_cat["delivered_count"] == 1
    # The headline behavior: Art Track structurally has no editor telemetry.
    assert art_cat["labor_minutes_avg"] is None
    assert art_cat["labor_cost_avg_usd"] is None
    assert art_cat["total_cost_avg_usd"] == art_cat["infra_cost_avg_usd"]
    assert "margin_usd" in art_cat


def test_report_counts_unknown_separately(db):
    unknown_job = _uid("cbvtU1")
    _job(db, unknown_job, None)
    db.commit()

    report = cbvt.build_cost_by_video_type(db, PERIOD)
    assert report["unknown"]["delivered_count"] >= 1
    for cat in cbvt.VIDEO_TYPE_CATEGORIES:
        assert unknown_job not in report["categories"][cat]


def test_report_without_prices_never_computes_margin(db):
    job_id = _uid("cbvtP1")
    _job(db, job_id, {"background_ai_generated": True})
    db.commit()

    report = cbvt.build_cost_by_video_type(db, PERIOD)
    for cat in cbvt.VIDEO_TYPE_CATEGORIES:
        assert "margin_usd" not in report["categories"][cat]
        assert "margin_pct" not in report["categories"][cat]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_endpoint_requires_admin(client, user_token):
    res = client.get(f"/admin/cost/by-video-type?period={PERIOD}",
                     headers=auth(user_token))
    assert res.status_code == 403


def test_endpoint_requires_period(client, admin_token):
    res = client.get("/admin/cost/by-video-type", headers=auth(admin_token))
    assert res.status_code == 422


def test_endpoint_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/by-video-type?period=2026-13",
                     headers=auth(admin_token))
    assert res.status_code == 400


def test_endpoint_returns_all_three_categories_plus_unknown(
    client, admin_token, db,
):
    job_id = _uid("cbvtE1")
    _job(db, job_id, {"background_ai_generated": True})
    _veo(db, job_id, n=1)
    db.commit()

    res = client.get(f"/admin/cost/by-video-type?period={PERIOD}",
                     headers=auth(admin_token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body["categories"]) == {
        cbvt.CAT_LYRIC_VEO, cbvt.CAT_LYRIC_STATIC, cbvt.CAT_ART_TRACK,
    }
    assert body["categories"][cbvt.CAT_LYRIC_VEO]["delivered_count"] >= 1
    assert "delivered_count" in body["unknown"]
    assert body["labor_rate_usd_per_hour"] == 10.0   # default


def test_endpoint_margin_only_appears_for_priced_categories(
    client, admin_token, db,
):
    job_id = _uid("cbvtE2")
    _job(db, job_id, {"art_track": True})
    db.commit()

    res = client.get(
        f"/admin/cost/by-video-type?period={PERIOD}&price_art_track_usd=4.0",
        headers=auth(admin_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "margin_usd" in body["categories"][cbvt.CAT_ART_TRACK]
    assert "margin_usd" not in body["categories"][cbvt.CAT_LYRIC_VEO]


def test_endpoint_this_route_is_not_swallowed_by_the_tenant_catch_all(
    client, admin_token,
):
    """`/cost/{tenant_id}` is registered later precisely so a literal path
    like this one is never captured by it — see the NOTE above that route
    in admin.py. A regression would return a tenant cost-summary shape
    instead (no `categories` key)."""
    res = client.get(f"/admin/cost/by-video-type?period={PERIOD}",
                     headers=auth(admin_token))
    assert res.status_code == 200
    assert "categories" in res.json()
