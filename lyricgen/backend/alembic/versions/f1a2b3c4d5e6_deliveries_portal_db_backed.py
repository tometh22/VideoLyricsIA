"""deliveries portal db-backed (replace static items.json)

Revision ID: f1a2b3c4d5e6
Revises: e2f4c8d6a9b1
Create Date: 2026-05-14 00:00:00.000000

Hasta ahora el portal umg.genly.pro se alimentaba de un items.json
estático committeado en /Users/tomi/genly-deliveries/. Cada cambio
requería editar JSON + correr gen_page.py + redeploy a Vercel.

Esta migración crea la tabla `deliveries` para que admins puedan
publicar versiones desde la app con un botón, y el portal pueda
borrarlas. El backfill seedea las 18 versiones existentes leyendo el
items.json actual; si el archivo no existe en el entorno (CI,
staging) la migración no falla — solo crea la tabla vacía.

Notas de diseño:
- Sin FK a jobs.job_id: hay 2 versiones cuyas filas de jobs fueron
  hard-deleted en limpiezas viejas (81ec813ab583, aa3c60aca7f7) pero
  los R2 files siguen ahí; tienen que seguir siendo deliverables.
- artist/song/tenant snapshotted on insert para no perderlos si el
  job source cambia (rename de canción post-aprobación, etc).
- Soft delete + R2 intacto: un click de "Borrar" no debería poder
  destruir un 8GB master, así que removed_at solo oculta del listado.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e2f4c8d6a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Paths candidatos donde puede vivir items.json al correr la migration.
# El primero es el path en mi laptop; los otros son fallbacks razonables
# si la migration corre en CI o desde otro check-out. Si ninguno existe,
# el backfill se saltea silenciosamente (la tabla queda vacía y se
# llena via UI). Esto evita que un environment sin acceso al repo de
# genly-deliveries (staging, Railway prod sin el repo clonado) rompa
# el deploy.
_SEED_PATHS = [
    "/Users/tomi/genly-deliveries/items.json",
    os.environ.get("DELIVERIES_SEED_PATH", ""),
    "/app/genly-deliveries/items.json",
]


def _load_seed_data() -> list[dict] | None:
    """Read the legacy items.json and return a flat list of version
    dicts ready to insert. Returns None if no seed file is reachable."""
    for path in _SEED_PATHS:
        if path and os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            flat = []
            for song in data.get("songs", []):
                for v in song.get("versions", []):
                    flat.append(
                        {
                            "job_id": v["job_id"],
                            "label": v.get("label", "Renderizado"),
                            "file_types": v.get("file_types", []),
                            "artist": song.get("artist", ""),
                            "song_title": song.get("song", ""),
                            "tenant": v.get("tenant", "default"),
                            "frame_size": v.get("frame_size"),
                        }
                    )
            return flat
    return None


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False, server_default="Renderizado"),
        sa.Column("file_types", JSONB(), nullable=False),
        sa.Column("artist_snapshot", sa.String(length=255), nullable=False),
        sa.Column("song_title_snapshot", sa.String(length=500), nullable=False),
        sa.Column("tenant_snapshot", sa.String(length=100), nullable=False),
        sa.Column("frame_size_snapshot", sa.String(length=20), nullable=True),
        sa.Column("added_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_deliveries_job_id", "deliveries", ["job_id"])
    op.create_index("ix_deliveries_removed_at", "deliveries", ["removed_at"])
    op.create_index("ix_deliveries_active", "deliveries", ["removed_at", "added_at"])

    # --- Backfill desde items.json -----------------------------------
    seed = _load_seed_data()
    if not seed:
        return

    # User responsible for the backfill: necesitamos un FK válido a
    # users.id. Usamos el admin (id=1 en prod) si existe; si no, el
    # primer usuario admin que encontremos; si tampoco, abortamos el
    # backfill (no fallamos la migration — solo no llenamos la tabla).
    conn = op.get_bind()
    admin_id_row = conn.execute(
        sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    ).fetchone()
    if admin_id_row is None:
        return
    admin_id = admin_id_row[0]

    now = datetime.now(timezone.utc)
    rows = [
        {
            "job_id": item["job_id"],
            "label": item["label"],
            "file_types": json.dumps(item["file_types"]),
            "artist_snapshot": item["artist"],
            "song_title_snapshot": item["song_title"],
            "tenant_snapshot": item["tenant"],
            "frame_size_snapshot": item["frame_size"],
            "added_by_user_id": admin_id,
            "added_at": now,
        }
        for item in seed
    ]
    if rows:
        conn.execute(
            sa.text(
                """
                INSERT INTO deliveries (
                    job_id, label, file_types,
                    artist_snapshot, song_title_snapshot, tenant_snapshot,
                    frame_size_snapshot, added_by_user_id, added_at
                ) VALUES (
                    :job_id, :label, CAST(:file_types AS JSONB),
                    :artist_snapshot, :song_title_snapshot, :tenant_snapshot,
                    :frame_size_snapshot, :added_by_user_id, :added_at
                )
                """
            ),
            rows,
        )


def downgrade() -> None:
    op.drop_index("ix_deliveries_active", table_name="deliveries")
    op.drop_index("ix_deliveries_removed_at", table_name="deliveries")
    op.drop_index("ix_deliveries_job_id", table_name="deliveries")
    op.drop_table("deliveries")
