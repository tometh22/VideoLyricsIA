"""corpus reference-segments precarga (validator calibration)

Revision ID: 310138eb0e54
Revises: 4d74aae79689
Create Date: 2026-08-24 00:00:00.000000

Lets the gold-corpus annotation tool (corpus.py) start an annotator from a
reviewed transcription instead of silence, for the songs that already have
one. Schema-only — the actual data backfill (matching each CorpusSong back
to its source job's editor_documents row and cleaning current_segments
down to the annotation shape) is a separate, idempotent, re-runnable step:
see corpus_reference.backfill_reference_segments / the
scripts/backfill_corpus_reference_segments.py CLI / the admin
POST /admin/corpus/songs/backfill-references endpoint.

- corpus_songs.reference_segments: precarga payload, NULL until backfilled
  (or forever NULL for control songs / songs with no matched editor doc).
- corpus_songs.is_control: True for the "CONTROL:" songs (see `notes`)
  that must NEVER get a precarga, blind-check that annotators do just as
  well from zero.
- corpus_annotations.seeded_from_reference: True when THIS annotator's
  draft was created pre-filled from the precarga, set once at row
  creation and never revisited — the persisted "verify, don't invent"
  signal for the frontend.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "310138eb0e54"
down_revision: Union[str, Sequence[str], None] = "4d74aae79689"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "corpus_songs",
        sa.Column(
            "reference_segments",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.add_column(
        "corpus_songs",
        sa.Column(
            "is_control", sa.Boolean(), nullable=False, server_default="false",
        ),
    )
    op.add_column(
        "corpus_annotations",
        sa.Column(
            "seeded_from_reference", sa.Boolean(), nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("corpus_annotations", "seeded_from_reference")
    op.drop_column("corpus_songs", "is_control")
    op.drop_column("corpus_songs", "reference_segments")
