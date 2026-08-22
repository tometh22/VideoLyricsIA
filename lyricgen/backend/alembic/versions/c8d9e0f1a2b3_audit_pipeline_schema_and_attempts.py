"""reconcile runtime schema and add pipeline attempt fences

Revision ID: c8d9e0f1a2b3
Revises: b6c7d8e9f0a1

The application historically created several tables/columns at startup.
Production release migrations must be authoritative, so this revision is
intentionally tolerant of objects already created by that legacy path.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c8d9e0f1a2b3"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql",
    )


def _inspector():
    return sa.inspect(op.get_bind())


def _add_column_if_missing(table, column):
    if column.name not in {item["name"] for item in _inspector().get_columns(table)}:
        op.add_column(table, column)


def _create_index_if_missing(name, table, columns, *, unique=False):
    if name not in {item["name"] for item in _inspector().get_indexes(table)}:
        op.create_index(name, table, columns, unique=unique)


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "sales_leads" not in tables:
        op.create_table(
            "sales_leads",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("company", sa.String(255), nullable=True),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("volume", sa.String(100), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing("ix_sales_leads_created_at", "sales_leads", ["created_at"])
    _create_index_if_missing("ix_sales_leads_email", "sales_leads", ["email"])

    if "system_youtube_token" not in tables:
        op.create_table(
            "system_youtube_token",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("encrypted_token_json", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.String(255), nullable=True),
            sa.Column("channel_name", sa.String(255), nullable=True),
            sa.Column("channel_thumbnail", sa.String(500), nullable=True),
            sa.Column("connected_by_user_id", sa.Integer(), nullable=True),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "transcription_cache" not in tables:
        op.create_table(
            "transcription_cache",
            sa.Column("cache_key", sa.String(64), primary_key=True),
            sa.Column("audio_hash", sa.String(32), nullable=False),
            sa.Column("engine", sa.String(20), nullable=False),
            sa.Column("language", sa.String(8), nullable=True),
            sa.Column("lyrics_hint_hash", sa.String(16), nullable=True),
            sa.Column("segments", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing(
        "ix_transcription_cache_audio_hash", "transcription_cache", ["audio_hash"],
    )
    _create_index_if_missing(
        "ix_transcription_cache_created_at", "transcription_cache", ["created_at"],
    )

    if "api_keys" not in tables:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("key_prefix", sa.String(12), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    _create_index_if_missing("ix_api_keys_user_id", "api_keys", ["user_id"])
    _create_index_if_missing(
        "ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True,
    )

    if "credit_grants" not in tables:
        op.create_table(
            "credit_grants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("billing_group", sa.String(100), nullable=True),
            sa.Column("tenant_id", sa.String(100), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(100), nullable=False),
            sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    for index_name, column in (
        ("ix_credit_grants_billing_group", "billing_group"),
        ("ix_credit_grants_tenant_id", "tenant_id"),
        ("ix_credit_grants_reason", "reason"),
        ("ix_credit_grants_granted_at", "granted_at"),
        ("ix_credit_grants_expires_at", "expires_at"),
    ):
        _create_index_if_missing(index_name, "credit_grants", [column])

    if "delivery_change_requests" not in tables:
        op.create_table(
            "delivery_change_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id"), nullable=False,
            ),
            sa.Column("comment", sa.Text(), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True,
            ),
            sa.Column("resolution_note", sa.Text(), nullable=True),
        )
    _create_index_if_missing(
        "ix_delivery_change_requests_delivery_id",
        "delivery_change_requests", ["delivery_id"],
    )
    _create_index_if_missing(
        "ix_dcr_pending", "delivery_change_requests", ["resolved_at", "submitted_at"],
    )

    for column in (
        sa.Column("file_sizes", _json(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_label", sa.String(120), nullable=True),
    ):
        _add_column_if_missing("deliveries", column)
    _create_index_if_missing("ix_deliveries_approved_at", "deliveries", ["approved_at"])

    for column in (
        sa.Column("song_title", sa.String(500), nullable=True),
        sa.Column("timing_source", sa.String(20), nullable=True),
        sa.Column("input_r2_key", sa.Text(), nullable=True),
        sa.Column("multipart_upload_id", sa.Text(), nullable=True),
        sa.Column("youtube_short_data", _json(), nullable=True),
        sa.Column("segments_json", _json(), nullable=True),
        sa.Column("render_params", _json(), nullable=True),
        sa.Column("edit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bg_r2_key_cached", sa.Text(), nullable=True),
        sa.Column("parent_job_id", sa.String(32), nullable=True),
        sa.Column("editing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_versions", _json(), nullable=True),
        sa.Column("active_pipeline_attempt_id", sa.String(36), nullable=True),
        sa.Column("active_transcription_attempt_id", sa.String(36), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
    ):
        _add_column_if_missing("jobs", column)
    _create_index_if_missing("ix_jobs_parent_job_id", "jobs", ["parent_job_id"])

    # The initial baseline used generic JSON. Runtime models require JSONB for
    # equality/operators. Keep SQLite portable and reconcile only PostgreSQL.
    if op.get_bind().dialect.name == "postgresql":
        for table, column in (
            ("ai_provenance", "input_data_types"),
            ("audit_log", "detail"),
            ("deliveries", "file_types"),
            ("deliveries", "file_sizes"),
            ("jobs", "umg_spec"),
            ("jobs", "s3_keys"),
            ("jobs", "youtube_data"),
            ("jobs", "youtube_short_data"),
            ("jobs", "validation_result"),
            ("jobs", "segments_json"),
            ("jobs", "render_params"),
            ("jobs", "previous_versions"),
            ("lyrics_cache", "source_urls"),
            ("ui_events", "event_data"),
            ("user_settings", "settings_json"),
        ):
            op.alter_column(
                table, column, type_=postgresql.JSONB(astext_type=sa.Text()),
                postgresql_using=f"{column}::jsonb",
            )


def downgrade():
    # Reconciliation objects may predate Alembic and contain production data;
    # dropping them on downgrade would be destructive. Only the new, audit-
    # owned nullable fence columns are safe to remove.
    columns = {item["name"] for item in _inspector().get_columns("jobs")}
    for name in ("error_code", "active_transcription_attempt_id", "active_pipeline_attempt_id"):
        if name in columns:
            op.drop_column("jobs", name)
