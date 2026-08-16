"""quality learning feedback loop

Revision ID: a8c1e4f7b2d9
Revises: f4a6b8c0d2e4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8c1e4f7b2d9"
down_revision: Union[str, Sequence[str], None] = "f4a6b8c0d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "quality_learning_epoch", sa.BigInteger(), nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("quality_learning_invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "editor_versions",
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "correction_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(12), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("original_revision", sa.Integer(), nullable=False),
        sa.Column("approved_revision", sa.Integer(), nullable=False),
        sa.Column("approved_version_id", sa.String(36), nullable=False),
        sa.Column("original_hash", sa.String(64), nullable=False),
        sa.Column("approved_hash", sa.String(64), nullable=False),
        sa.Column("audio_hash", sa.String(64), nullable=True),
        sa.Column("pipeline_release", sa.String(64), nullable=False),
        sa.Column("pipeline_config_fingerprint", sa.String(64), nullable=False),
        sa.Column("timing_source", sa.String(64), nullable=False),
        sa.Column("pipeline_route", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("label_tier", sa.String(20), nullable=False, server_default="observed"),
        sa.Column("source_confidence", sa.String(24), nullable=False, server_default="exact"),
        sa.Column("operator_hmac", sa.String(64), nullable=True),
        sa.Column("session_hmac", sa.String(64), nullable=True),
        sa.Column("artist_hmac", sa.String(64), nullable=True),
        sa.Column("song_hmac", sa.String(64), nullable=True),
        sa.Column("hmac_key_id", sa.String(32), nullable=False, server_default="legacy-v1"),
        sa.Column("categories", postgresql.JSONB(), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("active_edit_ms", sa.BigInteger(), nullable=True),
        sa.Column("matures_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trusted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_version_id"], ["editor_versions.id"], ondelete="CASCADE"),
    )
    for name, columns, unique in (
        ("ix_correction_observations_identity_hash", ["identity_hash"], True),
        ("ix_correction_observations_job_id", ["job_id"], False),
        ("ix_correction_observations_tenant_id", ["tenant_id"], False),
        ("ix_correction_observations_approved_version_id", ["approved_version_id"], False),
        ("ix_correction_observations_pipeline_release", ["pipeline_release"], False),
        ("ix_correction_observations_label_tier", ["label_tier"], False),
        ("ix_correction_observations_matures_at", ["matures_at"], False),
        ("ix_correction_observations_created_at", ["created_at"], False),
        ("ix_correction_observations_tier_created", ["label_tier", "created_at"], False),
        ("ix_correction_observations_release_created", ["pipeline_release", "created_at"], False),
    ):
        op.create_index(name, "correction_observations", columns, unique=unique)

    op.create_table(
        "quality_patterns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("context_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="emerging"),
        sa.Column("support_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("support_tenants", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("support_artists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("observed_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("relative_risk", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ci_low", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ci_high", sa.Float(), nullable=False, server_default="0"),
        sa.Column("impact_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quality_patterns_fingerprint", "quality_patterns", ["fingerprint"], unique=True)
    op.create_index("ix_quality_patterns_category", "quality_patterns", ["category"])
    op.create_index("ix_quality_patterns_status", "quality_patterns", ["status"])

    op.create_table(
        "quality_fix_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pattern_id", sa.String(36), nullable=False),
        sa.Column("proposal_type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("candidate_config", postgresql.JSONB(), nullable=False),
        sa.Column("expected_impact", postgresql.JSONB(), nullable=False),
        sa.Column("validation_summary", postgresql.JSONB(), nullable=True),
        sa.Column("ready_artifact", postgresql.JSONB(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("last_idempotency_key", sa.String(100), nullable=True),
        sa.Column("action_idempotency_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pattern_id"], ["quality_patterns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
    )
    op.create_index("ix_quality_fix_proposals_pattern_id", "quality_fix_proposals", ["pattern_id"])
    op.create_index("ix_quality_fix_proposals_status", "quality_fix_proposals", ["status"])
    op.create_index("ix_quality_fix_proposals_idempotency", "quality_fix_proposals", ["last_idempotency_key"], unique=True)

    op.create_table(
        "quality_experiment_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("baseline_config_hash", sa.String(64), nullable=True),
        sa.Column("candidate_config_hash", sa.String(64), nullable=False),
        sa.Column("benchmark_report_hash", sa.String(64), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["proposal_id"], ["quality_fix_proposals.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_quality_experiment_runs_proposal_id", "quality_experiment_runs", ["proposal_id"])
    op.create_index("ix_quality_experiment_runs_status", "quality_experiment_runs", ["status"])


def downgrade() -> None:
    op.drop_table("quality_experiment_runs")
    op.drop_table("quality_fix_proposals")
    op.drop_table("quality_patterns")
    op.drop_table("correction_observations")
    op.drop_column("editor_versions", "provenance")
    op.drop_column("jobs", "quality_learning_invalidated_at")
    op.drop_column("jobs", "quality_learning_epoch")
