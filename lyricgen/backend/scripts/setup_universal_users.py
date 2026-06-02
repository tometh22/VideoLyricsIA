#!/usr/bin/env python3
"""Reorganización de cuentas para el modelo Universal Music (2026-06-02).

Estado final que produce este script (ver plan aprobado por Tomás):

  Tenant universal_argentina (plan 250, billing_group universal_music):
    - anapatricia.mastrangioli   (existente — se mueve CON sus videos)
    - santiago.silva             (existente — ídem)
    - roy.ramirez                (existente — ídem)

  Tenant universal_chile (plan 250, billing_group universal_music):
    - gabriela.albarracin@umusic.com   (NUEVA — historial vacío)
    - giordano.colussa@umusic.com      (NUEVA — historial vacío)

  Tenant genly (plan unlimited, admins):
    - tomas@epical.digital    (se mueve CON sus videos)
    - agus.cafisi             (ídem)

  Todo el resto de usuarios (excepto la cuenta bootstrap "admin"):
    - SOFT-DELETE (is_active=False, username → deleted_{id}, email → NULL)

Ambos tenants de Universal comparten el pool de 250/mes vía
billing_group="universal_music" (auth.get_plan_usage).

Uso:
    # Dry-run (default): muestra qué haría, NO toca nada
    python -m scripts.setup_universal_users

    # Aplicar de verdad
    python -m scripts.setup_universal_users --apply

    # Contra producción (lo corre Tomás):
    railway run --environment production --service api \
        python -m scripts.setup_universal_users --apply

Idempotente: correrlo dos veces deja el mismo estado (los usuarios ya
movidos no se re-mueven, los ya creados no se re-crean, los ya borrados
no se re-borran).
"""
import argparse
import secrets
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import SessionLocal, Job, User  # noqa: E402
from auth import create_user  # noqa: E402

# --- Definición del estado final -------------------------------------------

BILLING_GROUP = "universal_music"
PLAN_UNIVERSAL = "250"

ARGENTINA = {
    "tenant": "universal_argentina",
    "usernames": ["anapatricia.mastrangioli", "santiago.silva", "roy.ramirez"],
}

CHILE = {
    "tenant": "universal_chile",
    "new_users": [
        {"username": "gabriela.albarracin@umusic.com", "email": "Gabriela.Albarracin@umusic.com"},
        {"username": "giordano.colussa@umusic.com", "email": "giordano.colussa@umusic.com"},
    ],
}

GENLY = {
    "tenant": "genly",
    "plan": "unlimited",
    "usernames": ["tomas@epical.digital", "agus.cafisi"],
}

# Cuentas que NUNCA se tocan ni se borran.
PROTECTED_USERNAMES = {"admin"}


def log(msg, dry):
    prefix = "[DRY-RUN] " if dry else "[APPLY]   "
    print(f"{prefix}{msg}")


def move_user(db, user, tenant, plan, billing_group, dry, move_jobs=True):
    """Mueve un usuario (y opcionalmente sus jobs) a un tenant/plan/grupo."""
    changes = []
    if user.tenant_id != tenant:
        changes.append(f"tenant {user.tenant_id!r} → {tenant!r}")
    if user.plan_id != plan:
        changes.append(f"plan {user.plan_id!r} → {plan!r}")
    if (user.billing_group or None) != (billing_group or None):
        changes.append(f"billing_group {user.billing_group!r} → {billing_group!r}")

    n_jobs = 0
    if move_jobs and user.tenant_id != tenant:
        n_jobs = db.query(Job).filter(
            Job.user_id == user.id, Job.tenant_id == user.tenant_id,
        ).count()

    if not changes:
        log(f"  {user.username}: ya está OK (sin cambios)", dry)
        return

    log(f"  {user.username}: {', '.join(changes)}"
        + (f" + mover {n_jobs} videos" if n_jobs else ""), dry)

    if dry:
        return
    old_tenant = user.tenant_id
    if move_jobs and old_tenant != tenant:
        db.query(Job).filter(
            Job.user_id == user.id, Job.tenant_id == old_tenant,
        ).update({Job.tenant_id: tenant}, synchronize_session=False)
    user.tenant_id = tenant
    user.plan_id = plan
    user.billing_group = billing_group
    db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Aplicar los cambios (default: dry-run)")
    args = parser.parse_args()
    dry = not args.apply

    db = SessionLocal()
    generated_passwords = {}

    try:
        print("=" * 70)
        print("REORGANIZACIÓN UNIVERSAL MUSIC" + ("  (DRY-RUN — nada se modifica)" if dry else ""))
        print("=" * 70)

        # --- 1. Argentina: mover usuarios existentes ----------------------
        print("\n1 · Universal Argentina")
        for username in ARGENTINA["usernames"]:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                log(f"  ⚠️  {username}: NO EXISTE — verificar el username en prod", dry)
                continue
            move_user(db, user, ARGENTINA["tenant"], PLAN_UNIVERSAL, BILLING_GROUP, dry)

        # --- 2. Chile: crear usuarios nuevos -------------------------------
        print("\n2 · Universal Chile")
        for spec in CHILE["new_users"]:
            existing = db.query(User).filter(User.username == spec["username"]).first()
            if existing:
                # Ya existe (re-corrida del script): asegurar tenant/plan/grupo.
                move_user(db, existing, CHILE["tenant"], PLAN_UNIVERSAL, BILLING_GROUP, dry,
                          move_jobs=False)
                continue
            password = secrets.token_urlsafe(12)
            generated_passwords[spec["username"]] = password
            log(f"  CREAR {spec['username']} (tenant {CHILE['tenant']}, plan {PLAN_UNIVERSAL}, "
                f"grupo {BILLING_GROUP})", dry)
            if not dry:
                user = create_user(
                    db,
                    username=spec["username"],
                    password=password,
                    email=spec["email"],
                    role="user",
                    tenant_id=CHILE["tenant"],
                    plan=PLAN_UNIVERSAL,
                    # UMG Guideline 5: los operadores nuevos arrancan sin
                    # autorización de IA; se habilita desde el Admin Panel.
                    ai_authorized=False,
                    enforce_reserved=False,
                )
                user.billing_group = BILLING_GROUP
                db.commit()

        # --- 3. Genly: admins de la plataforma -----------------------------
        print("\n3 · Equipo Genly (admins)")
        for username in GENLY["usernames"]:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                log(f"  ⚠️  {username}: NO EXISTE — verificar el username en prod", dry)
                continue
            if user.role != "admin":
                log(f"  {username}: promover a admin", dry)
                if not dry:
                    user.role = "admin"
                    db.commit()
            move_user(db, user, GENLY["tenant"], GENLY["plan"], None, dry)

        # --- 4. Soft-delete del resto ---------------------------------------
        print("\n4 · Limpieza del resto de usuarios")
        keep = (
            set(ARGENTINA["usernames"])
            | {u["username"] for u in CHILE["new_users"]}
            | set(GENLY["usernames"])
            | PROTECTED_USERNAMES
        )
        others = (
            db.query(User)
            .filter(User.is_active == True)  # noqa: E712
            .filter(~User.username.in_(keep))
            .filter(~User.username.like("deleted_%"))
            .all()
        )
        if not others:
            log("  (no hay usuarios para borrar)", dry)
        for user in others:
            n_jobs = db.query(Job).filter(Job.user_id == user.id).count()
            log(f"  SOFT-DELETE {user.username} (tenant {user.tenant_id}, role {user.role}, "
                f"{n_jobs} videos quedan en DB para auditoría)", dry)
            if not dry:
                user.is_active = False
                user.email = None
                user.username = f"deleted_{user.id}"
                db.commit()

        # --- 5. Resumen -----------------------------------------------------
        print("\n" + "=" * 70)
        if dry:
            print("DRY-RUN terminado. Para aplicar: agregar --apply")
        else:
            print("✅ APLICADO. Estado final:")
            for u in db.query(User).filter(User.is_active == True).order_by(User.tenant_id).all():  # noqa: E712
                print(f"   {u.username:45s} tenant={u.tenant_id:25s} plan={u.plan_id:10s} "
                      f"grupo={u.billing_group or '—':18s} role={u.role}")
            if generated_passwords:
                print("\n🔑 PASSWORDS GENERADAS (guardarlas AHORA, no se vuelven a mostrar):")
                for username, pw in generated_passwords.items():
                    print(f"   {username}: {pw}")
        print("=" * 70)
    finally:
        db.close()


if __name__ == "__main__":
    main()
