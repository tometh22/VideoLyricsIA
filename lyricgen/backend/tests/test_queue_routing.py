"""Tenant-priority queue routing tests.

Pins down `_pick_queue` semantics:

  - B2B tenants (umg, omg) always land on the enterprise queue, even
    on plan='100'. Without this routing, agus.cafisi's batch of 5 omg
    jobs queued behind tomas's single-user work on the default queue
    on 2026-05-15 — the customer-facing wait was 80-100 min.
  - Legacy plan-based routing still works: 'unlimited' / 'enterprise'
    plan on any tenant lands on enterprise.
  - Empty/missing tenant_id falls through to plan-based routing.
  - The ENTERPRISE_TENANTS env var is the override point — adding a
    new B2B customer should require zero code, just a deploy with the
    new value.

We do NOT spin up real Redis here — `_pick_queue` is a pure decision
function once the queues are initialised. We monkeypatch `_init_redis`
to return sentinel objects representing each queue, then assert which
one comes back.
"""
from __future__ import annotations

import importlib

import queue_jobs


class _Sentinel:
    """Stand-in for an rq.Queue. _pick_queue only returns it; it never
    calls methods on it, so a marker object is enough."""
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):  # pragma: no cover
        return f"<Q {self.name}>"


def _patch_queues(monkeypatch):
    """Replace _init_redis with a function that returns marker queues."""
    q_default = _Sentinel("default")
    q_enterprise = _Sentinel("enterprise")
    monkeypatch.setattr(
        queue_jobs, "_init_redis",
        lambda: (object(), q_default, q_enterprise),
    )
    return q_default, q_enterprise


def test_omg_tenant_routes_to_enterprise_on_plan_100(monkeypatch):
    """The 2026-05-15 incident pin: agus.cafisi (omg) on plan='100'
    must NOT land on default queue. Pre-fix, this is exactly where
    the batch piled up."""
    _, q_enterprise = _patch_queues(monkeypatch)
    q = queue_jobs._pick_queue(plan="100", tenant_id="omg")
    assert q is q_enterprise, (
        f"omg tenant must route to enterprise queue regardless of plan; "
        f"got {q!r}"
    )


def test_umg_tenant_routes_to_enterprise_on_plan_100(monkeypatch):
    """Same guarantee for UMG."""
    _, q_enterprise = _patch_queues(monkeypatch)
    q = queue_jobs._pick_queue(plan="100", tenant_id="umg")
    assert q is q_enterprise


def test_enterprise_tenant_check_is_case_insensitive(monkeypatch):
    """Tenant ids in audit logs and JWTs have inconsistent casing
    historically; routing must not depend on it."""
    _, q_enterprise = _patch_queues(monkeypatch)
    for cased in ("OMG", "Omg", " omg ", "UMG"):
        q = queue_jobs._pick_queue(plan="100", tenant_id=cased)
        assert q is q_enterprise, f"case/whitespace mismatch for {cased!r}"


def test_unknown_tenant_falls_through_to_plan_routing(monkeypatch):
    """A free-tier tenant on plan='100' still lands on default."""
    q_default, q_enterprise = _patch_queues(monkeypatch)
    q = queue_jobs._pick_queue(plan="100", tenant_id="some_random_tenant")
    assert q is q_default
    # plan-based promotion still works for unknown tenants on enterprise plan
    q = queue_jobs._pick_queue(plan="enterprise", tenant_id="some_random_tenant")
    assert q is q_enterprise


def test_empty_tenant_id_falls_through_to_plan_routing(monkeypatch):
    """enqueue_pipeline's tenant_id default is '' — the routing must
    not blow up or accidentally match the empty string against the
    enterprise set."""
    q_default, q_enterprise = _patch_queues(monkeypatch)
    q = queue_jobs._pick_queue(plan="100", tenant_id="")
    assert q is q_default
    q = queue_jobs._pick_queue(plan="unlimited", tenant_id="")
    assert q is q_enterprise


def test_enterprise_tenants_env_override(monkeypatch):
    """Adding a new B2B customer should be an env-var change, not a
    code change. Verify the env value is picked up on import."""
    monkeypatch.setenv("ENTERPRISE_TENANTS", "acme,beta_corp")
    # Re-import to pick up the new env. importlib.reload re-runs module
    # body, which rebuilds `_ENTERPRISE_TENANTS`.
    importlib.reload(queue_jobs)
    _, q_enterprise = _patch_queues(monkeypatch)
    assert queue_jobs._pick_queue(plan="100", tenant_id="acme") is q_enterprise
    assert queue_jobs._pick_queue(plan="100", tenant_id="beta_corp") is q_enterprise
    # And the defaults are now NOT in the set
    q_default = queue_jobs._pick_queue(plan="100", tenant_id="omg")
    assert q_default is not q_enterprise


def test_redis_unavailable_returns_none(monkeypatch):
    """When Redis is down, _pick_queue returns None and the caller
    falls back to threads (dev path)."""
    monkeypatch.setattr(
        queue_jobs, "_init_redis",
        lambda: (None, None, None),
    )
    assert queue_jobs._pick_queue(plan="100", tenant_id="omg") is None
