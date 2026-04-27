"""Tests for provenance cost helpers — used by the per-tenant cost dashboard."""

from provenance import (
    COST_PER_CALL,
    DEFAULT_COST_PER_CALL,
    cost_for_record,
    tenant_cost_summary,
)


# ---------------------------------------------------------------------------
# COST_PER_CALL lookups
# ---------------------------------------------------------------------------


def test_veo_31_cost_is_4_dollars_per_call():
    assert cost_for_record("veo-3.1-generate-001", "google_vertex") == 4.00


def test_gemini_flash_cost_is_one_cent():
    assert cost_for_record("gemini-2.5-flash", "google_vertex") == 0.01


def test_whisper_local_is_free():
    assert cost_for_record("whisper", "local") == 0.0


def test_human_provided_is_free():
    assert cost_for_record("human-provided", "user_upload") == 0.0


def test_unknown_tool_returns_default():
    assert cost_for_record("future-model-x", "future-provider") == DEFAULT_COST_PER_CALL


def test_cost_rates_dict_is_non_empty_and_only_floats():
    """Guards against typos that would break dashboard math."""
    assert len(COST_PER_CALL) >= 5
    for key, val in COST_PER_CALL.items():
        assert isinstance(key, tuple) and len(key) == 2
        assert isinstance(val, (int, float))
        assert val >= 0


# ---------------------------------------------------------------------------
# tenant_cost_summary — integration test against test DB
# ---------------------------------------------------------------------------


def test_tenant_cost_summary_empty_db_returns_zero(db):
    """No provenance records → zero cost, empty by_tool list."""
    summary = tenant_cost_summary(db, tenant_id="default", since_days=30)
    assert summary["tenant_id"] == "default"
    assert summary["total_cost"] == 0.0
    assert summary["total_calls"] == 0
    assert summary["by_tool"] == []


def test_tenant_cost_summary_groups_by_tool(db):
    """Provenance records are grouped by tool and multiplied by COST_PER_CALL."""
    from database import Job, AIProvenance

    # Make a tenant + a job + several provenance records
    job = Job(
        job_id="costtest1",
        user_id=1,
        tenant_id="cost-test-tenant",
        artist="Test",
        filename="test.mp3",
        status="done",
    )
    db.add(job)
    db.flush()

    # 3 Veo calls + 5 Gemini calls
    for _ in range(3):
        db.add(AIProvenance(
            job_id="costtest1",
            step="video_bg",
            tool_name="veo-3.1-generate-001",
            tool_provider="google_vertex",
            prompt_sent="bg prompt",
        ))
    for _ in range(5):
        db.add(AIProvenance(
            job_id="costtest1",
            step="lyrics_analysis",
            tool_name="gemini-2.5-flash",
            tool_provider="google_vertex",
            prompt_sent="lyrics prompt",
        ))
    db.commit()

    summary = tenant_cost_summary(db, tenant_id="cost-test-tenant", since_days=30)

    assert summary["total_calls"] == 8
    # 3 × 4.00 + 5 × 0.01 = 12.05
    assert summary["total_cost"] == 12.05
    # by_tool sorted by cost desc — Veo dominates
    assert summary["by_tool"][0]["tool_name"] == "veo-3.1-generate-001"
    assert summary["by_tool"][0]["calls"] == 3
    assert summary["by_tool"][0]["cost"] == 12.0
    assert summary["by_tool"][1]["tool_name"] == "gemini-2.5-flash"
    assert summary["by_tool"][1]["calls"] == 5
    assert summary["by_tool"][1]["cost"] == 0.05


def test_tenant_cost_summary_isolates_tenants(db):
    """Cost for tenant A should not include tenant B's spend."""
    from database import Job, AIProvenance

    db.add(Job(job_id="tA1", user_id=1, tenant_id="tenant-a",
               artist="A", filename="a.mp3", status="done"))
    db.add(Job(job_id="tB1", user_id=1, tenant_id="tenant-b",
               artist="B", filename="b.mp3", status="done"))
    db.flush()

    db.add(AIProvenance(job_id="tA1", step="video_bg",
                        tool_name="veo-3.1-generate-001",
                        tool_provider="google_vertex",
                        prompt_sent="x"))
    db.add(AIProvenance(job_id="tB1", step="video_bg",
                        tool_name="veo-3.1-generate-001",
                        tool_provider="google_vertex",
                        prompt_sent="y"))
    db.add(AIProvenance(job_id="tB1", step="video_bg",
                        tool_name="veo-3.1-generate-001",
                        tool_provider="google_vertex",
                        prompt_sent="y2"))
    db.commit()

    a = tenant_cost_summary(db, tenant_id="tenant-a", since_days=30)
    b = tenant_cost_summary(db, tenant_id="tenant-b", since_days=30)
    assert a["total_calls"] == 1 and a["total_cost"] == 4.0
    assert b["total_calls"] == 2 and b["total_cost"] == 8.0
