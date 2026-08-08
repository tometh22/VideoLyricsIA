"""Seed two same-workspace operators and one durable-editor browser job."""

import sys
from pathlib import Path

# Allow direct execution from ``backend/scripts`` in CI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import create_user
from database import Job, SessionLocal, init_db

JOB_ID = "e2ecollab001"
TENANT_ID = "editor_e2e_team"
PASSWORD = "EditorE2E-test-123"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        users = []
        for username in ("editor_e2e_a", "editor_e2e_b"):
            user = create_user(db, username, PASSWORD, None, tenant_id=TENANT_ID)
            users.append(user)
        segments = [
            {"_id": f"line-{index}", "start": index * 0.8, "end": index * 0.8 + 0.6, "text": text}
            for index, text in enumerate(("Primera línea", "Segunda línea", "Tercera línea", "Cuarta línea", "Quinta línea"))
        ]
        db.add(Job(
            job_id=JOB_ID,
            user_id=users[0].id,
            tenant_id=TENANT_ID,
            artist="E2E Artist",
            song_title="Collaboration",
            filename="collaboration.wav",
            style="oscuro",
            status="pending_review",
            current_step="editing",
            progress=100,
            delivery_profile="youtube",
            segments_json=segments,
            segments_revision=0,
            input_r2_key=f"inputs/{TENANT_ID}/{JOB_ID}/collaboration.wav",
            bg_r2_key_cached=f"backgrounds/{TENANT_ID}/{JOB_ID}/background.mp4",
        ))
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
