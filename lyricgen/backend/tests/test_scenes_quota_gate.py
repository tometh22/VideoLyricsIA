"""Gate de cuota vs costo de Escenas (_enforce_plan_quota con credits_needed).

Un video con Escenas pesa scenes_credit_cost() créditos (default 3). El gate
compara `total_available` (plan + regalo) contra el costo del video que se
está por generar — mismo número que muestra el medidor — en vez del
"queda al menos 1" histórico. Cubre además el caso que ese cambio arregla
de rebote: un regalo emitido a mitad de mes desbloquea a una cuenta que ya
había agotado el plan (antes: medidor con créditos, gate en 402).
"""
import uuid as _uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from database import Job, CreditGrant


def _tid(p="qg"):
    return f"{p}_{_uuid.uuid4().hex[:8]}"


def _user(tenant_id, *, plan="free", allow_overage=False):
    return {"id": 1, "tenant_id": tenant_id, "plan": plan,
            "allow_overage": allow_overage}


def _approved(db, tenant_id, *, scenes=False, user_id=1):
    """Un video aprobado este mes = consume crédito (peso 1, o N si Escenas)."""
    jid = _uuid.uuid4().hex[:12]
    db.add(Job(
        job_id=jid, tenant_id=tenant_id, user_id=user_id, artist="A",
        song_title=jid, filename="a.mp3", status="done", approved_by=user_id,
        approved_at=datetime.now(timezone.utc),
        scene_plan=({"scenes": [{"recurrence_key": "c1"}]} if scenes else None),
    ))
    db.commit()
    return jid


def _gate(db, current_user, credits_needed=1):
    from main import _enforce_plan_quota
    _enforce_plan_quota(db, current_user, credits_needed=credits_needed)


# ── costo de Escenas vs crédito suelto ──────────────────────────────────────

def test_scenes_blocked_when_short_of_credits(db, monkeypatch):
    """free (cupo 5) con 4 usados → queda 1: Escenas (3) no arranca."""
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    for _ in range(4):
        _approved(db, t)
    with pytest.raises(HTTPException) as ei:
        _gate(db, _user(t), credits_needed=3)
    assert ei.value.status_code == 402
    # Mensaje específico: dice cuánto cuesta y ofrece generar sin Escenas.
    assert "Escenas" in ei.value.detail
    # Un video normal (1 crédito) sigue pasando con ese único crédito.
    _gate(db, _user(t), credits_needed=1)


def test_scenes_allowed_with_exact_credits(db, monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    for _ in range(2):
        _approved(db, t)  # quedan 3 de 5
    _gate(db, _user(t), credits_needed=3)  # no levanta


def test_exhausted_plan_still_generic_402(db):
    """Sin crédito alguno, el mensaje sigue siendo el histórico de límite."""
    t = _tid()
    for _ in range(5):
        _approved(db, t)
    with pytest.raises(HTTPException) as ei:
        _gate(db, _user(t), credits_needed=3)
    assert ei.value.status_code == 402
    assert "límite mensual" in ei.value.detail


def test_allow_overage_bypasses_scenes_gate(db, monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    for _ in range(5):
        _approved(db, t)
    _gate(db, _user(t, allow_overage=True), credits_needed=3)  # no levanta


def test_unlimited_never_blocks(db, monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    _gate(db, _user(t, plan="unlimited"), credits_needed=3)


# ── regalo mid-month: el gate ahora mira total_available ────────────────────

def test_midmonth_grant_unblocks_exhausted_plan(db, monkeypatch):
    """Cuenta que agotó el plan ANTES del regalo: el grant no cubre esos
    videos (ventana desde granted_at), pero sí habilita los próximos —
    el gate debe mirar total_available (0 de plan + 30 de regalo), no
    el remaining del plan."""
    monkeypatch.setenv("SCENES_CREDIT_COST", "3")
    t = _tid()
    for _ in range(5):
        _approved(db, t)  # free agotado
    db.add(CreditGrant(tenant_id=t, amount=30, reason="escenas_launch",
                       granted_at=datetime.now(timezone.utc)))
    db.commit()
    _gate(db, _user(t), credits_needed=1)  # pasa: regalo disponible
    _gate(db, _user(t), credits_needed=3)  # Escenas también (30 ≥ 3)
