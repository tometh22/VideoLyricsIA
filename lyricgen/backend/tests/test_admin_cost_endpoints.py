"""Admin endpoints for real invoiced cost and model-vs-invoice reconciliation.

Contract these lock down:

* Cost endpoints are admin-only (they expose margin).
* A month with no snapshot must say so rather than reporting $0 — the
  whole point of this feature is that "we have no data" and "it was free"
  stopped being the same answer.
* `cost_per_delivered` divides by videos DELIVERED. The old internal doc
  divided $199.53 of jun-2026 Google Cloud by 173 created jobs and got
  $1.15/video; the honest number over the 65 that shipped is $3.07.
"""

from tests.conftest import auth


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_cost_endpoints_require_admin(client, user_token):
    for path in ("/admin/cost/real", "/admin/cost/unit-economics",
                 "/admin/cost/reconcile"):
        res = client.get(path, headers=auth(user_token))
        assert res.status_code == 403, path


def test_cost_refresh_requires_admin(client, user_token):
    res = client.post("/admin/cost/refresh", headers=auth(user_token))
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# /admin/cost/real
# ---------------------------------------------------------------------------

def test_cost_real_without_snapshot_reports_incomplete(client, admin_token):
    """No snapshot must not read as $0 with a confident total."""
    res = client.get("/admin/cost/real?period=2019-01",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert body["complete"] is False
    assert body["total_usd"] == 0.0
    assert body["not_configured"]          # every source listed as missing
    assert "refresh" in (body.get("detail") or "")


def test_cost_real_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/real?period=2026-13",
                     headers=auth(admin_token))
    assert res.status_code == 400


def test_cost_real_roundtrips_a_snapshot(client, admin_token, db):
    """A persisted snapshot is summed and reported as the source of truth."""
    from database import CostSnapshot

    db.query(CostSnapshot).filter(CostSnapshot.period == "2020-05").delete()
    db.add(CostSnapshot(period="2020-05", source="gcp", amount_usd=199.53,
                        status="ok", is_estimate=False))
    db.add(CostSnapshot(period="2020-05", source="railway", amount_usd=124.54,
                        status="ok", is_estimate=True))
    # A source that was never configured must NOT contribute 0 silently.
    db.add(CostSnapshot(period="2020-05", source="r2", amount_usd=None,
                        status="not_configured"))
    db.commit()

    try:
        res = client.get("/admin/cost/real?period=2020-05",
                         headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()
        assert body["total_usd"] == 324.07
        assert body["source_of_truth"] == "snapshot"
        assert "r2" in body["not_configured"]
        assert body["complete"] is False        # r2 missing
        # Sorted by spend, biggest first.
        assert body["sources"][0]["source"] == "gcp"
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == "2020-05").delete()
        db.commit()


# ---------------------------------------------------------------------------
# /admin/cost/unit-economics — the denominator contract
# ---------------------------------------------------------------------------

def test_unit_economics_divides_by_delivered_not_created(client, admin_token, db):
    """The core defect the 2026-08 audit found, locked down.

    3 jobs created, 1 delivered, $30 invoiced. Cost per delivered video is
    $30 — not $10. The discarded previews cost real money.
    """
    from database import CostSnapshot, Job
    from datetime import datetime, timezone

    period = "2020-06"
    when = datetime(2020, 6, 15, tzinfo=timezone.utc)
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.query(Job).filter(Job.tenant_id == "unit-econ-test").delete()

    db.add(CostSnapshot(period=period, source="gcp", amount_usd=30.0,
                        status="ok"))
    for jid, status in (("ue1", "done"), ("ue2", "bg_preview_done"),
                        ("ue3", "rejected")):
        db.add(Job(job_id=jid, user_id=1, tenant_id="unit-econ-test",
                   artist="A", filename="a.mp3", status=status,
                   created_at=when))
    db.commit()

    try:
        res = client.get(
            f"/admin/cost/unit-economics?period={period}&price_per_video_usd=13.5",
            headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()

        assert body["videos_created"] == 3
        assert body["videos_delivered"] == 1
        assert body["cost_per_delivered"] == 30.0
        # Kept only to show the operator how flattering the wrong one is.
        assert body["cost_per_created_MISLEADING"] == 10.0
        # $13.50 revenue against $30 cost is deeply negative — the point.
        assert body["margin_per_video"] == -16.5
        assert body["cost_complete"] is False   # only gcp snapshotted
        assert "railway" in body["missing_sources"]
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.query(Job).filter(Job.tenant_id == "unit-econ-test").delete()
        db.commit()


def test_unit_economics_handles_month_with_no_videos(client, admin_token):
    res = client.get("/admin/cost/unit-economics?period=2019-02",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert body["videos_delivered"] == 0
    assert body["cost_per_delivered"] is None
    assert body["margin_per_video"] is None


# ---------------------------------------------------------------------------
# /admin/cost/reconcile
# ---------------------------------------------------------------------------

def test_reconcile_reports_calibration_factor(client, admin_token):
    res = client.get("/admin/cost/reconcile?period=2019-03",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    # No invoices snapshotted for that month → everything AI is missing.
    assert set(body["invoiced_sources_missing"]) == {"gcp", "openai", "replicate"}
    assert body["invoiced_usd"] == 0.0
    assert "row_quality" in body
    assert "cache_hits_excluded" in body["row_quality"]


def test_reconcile_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/reconcile?period=nope",
                     headers=auth(admin_token))
    assert res.status_code == 400
