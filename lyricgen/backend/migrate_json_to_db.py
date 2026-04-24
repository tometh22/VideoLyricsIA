#!/usr/bin/env python3
"""
Migrate existing JSON data (_users.json, _jobs_*.json, _settings*.json)
into PostgreSQL.

Usage:
    python migrate_json_to_db.py [--dry-run]

Requires DATABASE_URL env var (or defaults to local PostgreSQL).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from glob import glob

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, SessionLocal, User, Job, UserSettings
from auth import pwd_context

OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")


def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: Could not read {path}: {e}")
        return {}


def migrate_users(db, dry_run=False):
    """Migrate _users.json → users table."""
    users_path = os.path.join(OUTPUTS_DIR, "_users.json")
    users_data = load_json(users_path)

    if not users_data:
        print("  No users to migrate.")
        return {}

    user_map = {}  # username → db user id
    migrated = 0
    skipped = 0

    for username, udata in users_data.items():
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            user_map[username] = existing.id
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY RUN] Would create user: {username} (role={udata.get('role')}, plan={udata.get('plan')})")
            migrated += 1
            continue

        user = User(
            username=username,
            hashed_password=udata.get("hashed_password", pwd_context.hash("changeme")),
            role=udata.get("role", "user"),
            tenant_id=udata.get("tenant_id", "default"),
            plan_id=udata.get("plan", "100"),
            is_active=True,
            created_at=datetime.fromtimestamp(
                udata.get("created_at", 0), tz=timezone.utc
            ) if udata.get("created_at") else None,
        )
        db.add(user)
        db.flush()
        user_map[username] = user.id
        migrated += 1

    if not dry_run:
        db.commit()

    print(f"  Users: {migrated} migrated, {skipped} skipped (already exist)")
    return user_map


def migrate_jobs(db, user_map, dry_run=False):
    """Migrate _jobs*.json → jobs table."""
    # Find all job files
    job_files = glob(os.path.join(OUTPUTS_DIR, "_jobs*.json"))

    if not job_files:
        print("  No job files found.")
        return

    total_migrated = 0
    total_skipped = 0

    for jf in sorted(job_files):
        fname = os.path.basename(jf)
        jobs_data = load_json(jf)

        if not jobs_data:
            continue

        # Determine tenant_id from filename
        if fname == "_jobs.json":
            file_tenant = "default"
        else:
            # _jobs_sometenant.json → sometenant
            file_tenant = fname.replace("_jobs_", "").replace(".json", "")

        migrated = 0
        skipped = 0

        for job_id, jdata in jobs_data.items():
            existing = db.query(Job).filter(Job.job_id == job_id).first()
            if existing:
                skipped += 1
                continue

            tenant_id = jdata.get("tenant_id", file_tenant)

            # Find user_id from tenant
            user_id = None
            for uname, uid in user_map.items():
                user = db.query(User).filter(User.id == uid).first()
                if user and user.tenant_id == tenant_id:
                    user_id = uid
                    break

            if not user_id:
                # Assign to first admin
                admin = db.query(User).filter(User.role == "admin").first()
                user_id = admin.id if admin else 1

            if dry_run:
                print(f"  [DRY RUN] Would create job: {job_id} ({jdata.get('artist')} - {jdata.get('filename')})")
                migrated += 1
                continue

            files = jdata.get("files", {})
            created_ts = jdata.get("created_at")

            job = Job(
                job_id=job_id,
                user_id=user_id,
                tenant_id=tenant_id,
                artist=jdata.get("artist", "Unknown"),
                style=jdata.get("style", "oscuro"),
                filename=jdata.get("filename", "unknown.mp3"),
                status=jdata.get("status", "error"),
                current_step=jdata.get("current_step", ""),
                progress=jdata.get("progress", 0),
                error=jdata.get("error"),
                video_url=files.get("video_url"),
                short_url=files.get("short_url"),
                thumbnail_url=files.get("thumbnail_url"),
                youtube_data=jdata.get("youtube"),
                created_at=datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None,
                completed_at=datetime.fromtimestamp(created_ts, tz=timezone.utc)
                    if created_ts and jdata.get("status") == "done" else None,
            )
            db.add(job)
            migrated += 1

        if not dry_run:
            db.commit()

        total_migrated += migrated
        total_skipped += skipped
        print(f"  {fname}: {migrated} migrated, {skipped} skipped")

    print(f"  Jobs total: {total_migrated} migrated, {total_skipped} skipped")


def migrate_settings(db, user_map, dry_run=False):
    """Migrate _settings*.json → user_settings table."""
    settings_files = glob(os.path.join(OUTPUTS_DIR, "_settings*.json"))

    if not settings_files:
        print("  No settings files found.")
        return

    for sf in sorted(settings_files):
        fname = os.path.basename(sf)
        data = load_json(sf)

        if not data:
            continue

        # Determine tenant
        if fname == "_settings.json":
            file_tenant = "default"
        else:
            file_tenant = fname.replace("_settings_", "").replace(".json", "")

        # Find user by tenant
        user_id = None
        for uname, uid in user_map.items():
            user = db.query(User).filter(User.id == uid).first()
            if user and user.tenant_id == file_tenant:
                user_id = uid
                break

        if not user_id:
            print(f"  {fname}: No user found for tenant '{file_tenant}', skipping")
            continue

        existing = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        if existing:
            print(f"  {fname}: Settings already exist for user {user_id}, skipping")
            continue

        if dry_run:
            print(f"  [DRY RUN] Would migrate settings for tenant '{file_tenant}'")
            continue

        settings = UserSettings(user_id=user_id, settings_json=data)
        db.add(settings)
        db.commit()
        print(f"  {fname}: Migrated settings for tenant '{file_tenant}'")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON data to PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    print("=" * 60)
    print("GenLy AI — JSON → PostgreSQL Migration")
    print("=" * 60)

    if args.dry_run:
        print("MODE: DRY RUN (no changes will be made)")
    else:
        print("MODE: LIVE (changes will be committed)")

    print(f"Outputs dir: {OUTPUTS_DIR}")
    print()

    # Init DB
    print("Initializing database...")
    init_db()
    db = SessionLocal()

    try:
        print("\n[1/3] Migrating users...")
        user_map = migrate_users(db, dry_run=args.dry_run)

        # If dry run, create a fake map for jobs/settings preview
        if args.dry_run and not user_map:
            users_data = load_json(os.path.join(OUTPUTS_DIR, "_users.json"))
            user_map = {u: i + 1 for i, u in enumerate(users_data.keys())}

        print("\n[2/3] Migrating jobs...")
        migrate_jobs(db, user_map, dry_run=args.dry_run)

        print("\n[3/3] Migrating settings...")
        migrate_settings(db, user_map, dry_run=args.dry_run)

        print("\n" + "=" * 60)
        print("Migration complete!")
        if args.dry_run:
            print("(No changes were made — run without --dry-run to apply)")
        print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
