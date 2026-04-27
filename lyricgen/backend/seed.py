#!/usr/bin/env python3
"""
Seed database with demo data for testing/demos.

Usage:
    python seed.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, SessionLocal, User, Job, Invoice
from auth import create_user, pwd_context

DEMO_USERS = [
    {"username": "demo_sello", "email": "sello@demo.com", "plan": "500", "role": "user"},
    {"username": "demo_artista", "email": "artista@demo.com", "plan": "100", "role": "user"},
    {"username": "demo_distribuidor", "email": "distro@demo.com", "plan": "1000", "role": "user"},
]

DEMO_JOBS = [
    {"artist": "Bad Bunny", "filename": "Bad Bunny - Monaco.mp3", "status": "done"},
    {"artist": "Peso Pluma", "filename": "Peso Pluma - Ella Baila Sola.mp3", "status": "done"},
    {"artist": "Shakira", "filename": "Shakira - Bzrp Session 53.mp3", "status": "done"},
    {"artist": "Karol G", "filename": "Karol G - TQG.mp3", "status": "done"},
    {"artist": "Feid", "filename": "Feid - Normal.mp3", "status": "done"},
    {"artist": "Rauw Alejandro", "filename": "Rauw Alejandro - Touching The Sky.mp3", "status": "processing"},
    {"artist": "Ozuna", "filename": "Ozuna - Monotonia.mp3", "status": "error"},
]


def main():
    print("=" * 60)
    print("GenLy AI — Seed Demo Data")
    print("=" * 60)

    init_db()
    db = SessionLocal()

    try:
        # Create demo users
        print("\nCreating demo users...")
        created_users = []
        for u in DEMO_USERS:
            existing = db.query(User).filter(User.username == u["username"]).first()
            if existing:
                print(f"  {u['username']}: already exists, skipping")
                created_users.append(existing)
                continue

            user = create_user(
                db,
                username=u["username"],
                password="demo12345678",
                email=u["email"],
                plan=u["plan"],
            )
            created_users.append(user)
            print(f"  {u['username']}: created (plan={u['plan']})")

        # Create demo jobs for first user
        print("\nCreating demo jobs...")
        demo_user = created_users[0]
        import uuid
        now = datetime.now(timezone.utc)

        for i, j in enumerate(DEMO_JOBS):
            job_id = uuid.uuid4().hex[:12]
            days_ago = len(DEMO_JOBS) - i
            created = now - timedelta(days=days_ago, hours=i * 2)

            job = Job(
                job_id=job_id,
                user_id=demo_user.id,
                tenant_id=demo_user.tenant_id,
                artist=j["artist"],
                style="oscuro",
                filename=j["filename"],
                status=j["status"],
                current_step="complete" if j["status"] == "done" else "whisper",
                progress=100 if j["status"] == "done" else 0,
                error="Whisper timeout" if j["status"] == "error" else None,
                created_at=created,
                completed_at=created + timedelta(minutes=4) if j["status"] == "done" else None,
            )
            db.add(job)
            print(f"  {j['artist']} — {j['status']}")

        db.commit()

        # Create demo invoices
        print("\nCreating demo invoices...")
        for i in range(3):
            month = now - timedelta(days=30 * (i + 1))
            inv = Invoice(
                user_id=demo_user.id,
                amount_cents=350000,
                currency="usd",
                status="paid",
                description=f"GenLy AI — Plan 500",
                period_start=month,
                period_end=month + timedelta(days=30),
                created_at=month,
            )
            db.add(inv)
            print(f"  Invoice: $3,500 — {month.strftime('%B %Y')}")

        db.commit()

        # Summary
        total_users = db.query(User).count()
        total_jobs = db.query(Job).count()
        total_invoices = db.query(Invoice).count()

        print(f"\n{'=' * 60}")
        print(f"Seed complete!")
        print(f"  Users: {total_users}")
        print(f"  Jobs: {total_jobs}")
        print(f"  Invoices: {total_invoices}")
        print(f"\nDemo login: demo_sello / demo12345678")
        print(f"{'=' * 60}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
