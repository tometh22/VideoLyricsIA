"""Créditos de regalo (promos de lanzamiento): netting vs cupo del plan,
peso de Escenas, overflow, proyección, vencimiento, asignación cross-month,
pool compartido por billing_group, reversión, y el endpoint admin de emisión.

El modelo NO muta filas al consumir: `get_plan_usage` calcula el consumo
contando los videos APROBADOS desde `granted_at` (mismo peso que la cuota:
normal=1, Escenas=N). Por eso rechazar/des-aprobar revierte el regalo solo
(cubierto en test_reject_restores_gift).
"""
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from database import Job, CreditGrant
from auth import get_plan_usage


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def _tid(p="lc"):
    return f"{p}_{_uuid.uuid4().hex[:8]}"


def _approved(db, tenant_id, *, scenes=False, when=None, user_id=1):
    """Un video aprobado este mes = consume crédito (peso 1, o N si Escenas)."""
    jid = _uuid.uuid4().hex[:12]
    db.add(Job(
        job_id=jid, tenant_id=tenant_id, user_id=user_id, artist="A", song_title=jid,
        filename="a.mp3", status="done", approved_by=user_id,
        approved_at=when or datetime.now(timezone.utc),
        scene_plan=({"scenes": [{"recurrence_key": "c1"}]} if scenes else None),
    ))
    db.commit()
    return jid


def _grant(db, *, tenant_id=None, billing_group=None, amount=30,
           reason="escenas_launch", granted_at=None, expires_at=None):
    g = CreditGrant(
        tenant_id=tenant_id, billing_group=billing_group, amount=amount,
        reason=reason, granted_at=granted_at or datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    db.add(g)
    db.commit()
    return g


# ── netting básico ──────────────────────────────────────────────────────────

def test_no_grant_usage_unchanged(db, monkeypatch):
    """Sin grant: el medidor reporta como siempre + los campos nuevos en cero."""
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    _approved(db, t)
    _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["used"] == 2
    assert u["bonus_total"] == 0 and u["bonus_remaining"] == 0 and u["bonus_used"] == 0
    assert u["scenes_credit_cost"] == 3


def test_grant_nets_before_plan(db):
    """El regalo cubre primero: 4 videos con regalo de 10 → 0 contra el plan."""
    t = _tid()
    _grant(db, tenant_id=t, amount=10)
    for _ in range(4):
        _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["used"] == 0           # nada pega contra el plan
    assert u["bonus_used"] == 4
    assert u["bonus_remaining"] == 6
    assert u["bonus_total"] == 10


def test_grant_escenas_weighs_three(db, monkeypatch):
    """Un video con Escenas descuenta 3 del regalo; uno normal, 1."""
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    _grant(db, tenant_id=t, amount=10)
    _approved(db, t, scenes=True)   # 3
    _approved(db, t)                # 1
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_used"] == 4
    assert u["bonus_remaining"] == 6
    assert u["used"] == 0


def test_grant_overflow_spills_to_plan(db):
    """Agotado el regalo (3), el resto pega contra el cupo del plan."""
    t = _tid()
    _grant(db, tenant_id=t, amount=3)
    for _ in range(5):
        _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_used"] == 3
    assert u["bonus_remaining"] == 0
    assert u["used"] == 2           # 5 - 3 de regalo


# ── medidor / proyección ────────────────────────────────────────────────────

def test_projection_and_total_available(db, monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    _grant(db, tenant_id=t, amount=30)
    u = get_plan_usage(db, 1, t, "100")     # plan 100, sin uso
    assert u["total_available"] == 130      # 100 plan + 30 regalo
    assert u["projection"]["normal"] == 130
    assert u["projection"]["escenas"] == 130 // 3   # 43


# ── vencimiento ─────────────────────────────────────────────────────────────

def test_expired_grant_ignored(db):
    t = _tid()
    _grant(db, tenant_id=t, amount=10,
           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_total"] == 0
    assert u["used"] == 1                    # el regalo vencido no cubre nada


def test_revoked_grant_ignored(db):
    t = _tid()
    g = _grant(db, tenant_id=t, amount=10)
    g.revoked = True
    db.commit()
    _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_total"] == 0 and u["used"] == 1


# ── asignación cross-month (pool una sola vez, no por mes) ───────────────────

def test_cross_month_prior_consumption(db):
    """El pool es uno solo: lo gastado el mes pasado reduce lo disponible hoy."""
    t = _tid()
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    last_month = month_start - timedelta(days=2)
    _grant(db, tenant_id=t, amount=10, granted_at=last_month - timedelta(days=1))
    for _ in range(7):                        # 7 consumidos el mes pasado
        _approved(db, t, when=last_month)
    for _ in range(5):                        # 5 este mes
        _approved(db, t, when=now)
    u = get_plan_usage(db, 1, t, "250")
    # avail al iniciar el mes = 10 - 7 = 3 → cubre 3 de los 5 → billable 2
    assert u["bonus_used"] == 3
    assert u["used"] == 2
    assert u["bonus_remaining"] == 0


# ── reversión automática (reject / un-approve) ──────────────────────────────

def test_reject_restores_gift(db):
    """Des-aprobar un job lo saca del conteo → el regalo se 'restituye' solo."""
    t = _tid()
    _grant(db, tenant_id=t, amount=10)
    jid = _approved(db, t)
    assert get_plan_usage(db, 1, t, "250")["bonus_used"] == 1
    job = db.query(Job).filter(Job.job_id == jid).first()
    job.status = "error"
    job.approved_at = None
    db.commit()
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_used"] == 0
    assert u["bonus_remaining"] == 10


# ── pool compartido por billing_group (Universal AR + CL) ───────────────────

def _make_user(client, admin_token, username, tenant, plan="250", billing_group=""):
    res = client.post("/admin/users", headers=_hdr(admin_token), json={
        "username": username, "password": "testpass12345",
        "email": f"{_uuid.uuid4().hex[:8]}@test.com",
        "plan_id": plan, "tenant_id": tenant, "billing_group": billing_group,
    })
    assert res.status_code == 200, res.text
    return res.json()


def test_grant_shared_across_billing_group(client, admin_token, db):
    s = _uuid.uuid4().hex[:6]
    bg = f"bg_{s}"
    ar = _make_user(client, admin_token, f"lc_ar_{s}", f"lc_ar_t_{s}", billing_group=bg)
    cl = _make_user(client, admin_token, f"lc_cl_{s}", f"lc_cl_t_{s}", billing_group=bg)
    _grant(db, billing_group=bg, amount=10)
    _approved(db, ar["tenant_id"], user_id=ar["id"])   # 1 en AR
    _approved(db, cl["tenant_id"], user_id=cl["id"])   # 1 en CL
    # Ambos ven el MISMO pool compartido: 2 consumidos, 8 restantes.
    u_ar = get_plan_usage(db, ar["id"], ar["tenant_id"], "250", billing_group=bg)
    u_cl = get_plan_usage(db, cl["id"], cl["tenant_id"], "250", billing_group=bg)
    assert u_ar["bonus_used"] == 2 and u_ar["bonus_remaining"] == 8
    assert u_cl["bonus_used"] == 2 and u_cl["bonus_remaining"] == 8


# ── endpoint admin de emisión ───────────────────────────────────────────────

def test_admin_grant_requires_admin(client, user_token):
    r = client.post("/admin/credit-grants", headers=_hdr(user_token), json={"scope": "all"})
    assert r.status_code == 403


def test_admin_grant_single_tenant(client, admin_token, db):
    t = _tid()
    r = client.post("/admin/credit-grants", headers=_hdr(admin_token), json={
        "scope": "tenant", "tenant_id": t, "amount": 12, "reason": f"r_{t}",
    })
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    _approved(db, t)
    u = get_plan_usage(db, 1, t, "250")
    assert u["bonus_remaining"] == 11 and u["used"] == 0


def test_admin_grant_all_dry_run_then_idempotent(client, admin_token, db):
    reason = f"launch_{_uuid.uuid4().hex[:6]}"
    # dry-run: no escribe
    r0 = client.post("/admin/credit-grants", headers=_hdr(admin_token), json={
        "scope": "all", "reason": reason, "dry_run": True,
    })
    assert r0.status_code == 200 and r0.json()["created"] == 0 and r0.json()["dry_run"] is True
    assert db.query(CreditGrant).filter(CreditGrant.reason == reason).count() == 0
    # real: crea al menos 1 (el admin existe como cuenta activa)
    r1 = client.post("/admin/credit-grants", headers=_hdr(admin_token), json={
        "scope": "all", "reason": reason,
    })
    assert r1.json()["created"] >= 1
    # idempotente: segunda corrida no re-otorga
    r2 = client.post("/admin/credit-grants", headers=_hdr(admin_token), json={
        "scope": "all", "reason": reason,
    })
    assert r2.json()["created"] == 0


def test_admin_grant_billing_group_required(client, admin_token):
    r = client.post("/admin/credit-grants", headers=_hdr(admin_token), json={
        "scope": "billing_group",
    })
    assert r.status_code == 400


def test_admin_list_credit_grants(client, admin_token, db):
    t = _tid()
    _grant(db, tenant_id=t, amount=15, reason=f"r_{t}")
    r = client.get("/admin/credit-grants", headers=_hdr(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "summary" in body
    mine = [i for i in body["items"] if i["tenant_id"] == t]
    assert len(mine) == 1
    assert mine[0]["amount"] == 15 and mine[0]["active"] is True
    assert body["summary"]["active_credits"] >= 15


def test_admin_list_credit_grants_requires_admin(client, user_token):
    r = client.get("/admin/credit-grants", headers=_hdr(user_token))
    assert r.status_code == 403
