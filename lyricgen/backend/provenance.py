"""AI Provenance recording — tracks every AI tool invocation for UMG compliance.

Also exposes per-tool cost rates and a tenant cost summary helper for the
admin dashboard. Rates are estimates of marginal API cost per call as of
2026-04, sourced from public pricing pages. The dict is the single source
of truth — update it when pricing changes.
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import AIProvenance, Job, SessionLocal

logger = logging.getLogger("genly.provenance")


# ---------------------------------------------------------------------------
# Cost rates — single source of truth for the cost dashboard
# ---------------------------------------------------------------------------

# Per-call estimates in USD. Veo costs assume an 8s clip generation per call
# (the palindrome-loop pattern used by this pipeline). Update when pricing
# changes or when the per-call generation length changes.
COST_PER_CALL: dict[tuple[str, str], float] = {
    # Veo video — ~$0.50/s × 8s = $4.00 per call
    ("veo-3.1-generate-001", "google_vertex"): 4.00,
    ("veo-3.0-generate-001", "google_vertex"): 4.00,
    ("veo-2.0-generate-001", "google_vertex"): 2.00,
    # Imagen still images
    ("imagen-3.0-generate-001", "google_vertex"): 0.04,
    ("imagen-3.0-fast-generate-001", "google_vertex"): 0.02,
    # Gemini text/multimodal — averaged across our prompt sizes
    ("gemini-2.5-flash", "google_vertex"): 0.01,
    ("gemini-2.5-flash-lite", "google_vertex"): 0.005,
    ("gemini-2.5-pro", "google_vertex"): 0.05,
    # Whisper local — runs on our compute, no API charge
    ("whisper", "local"): 0.0,
    ("whisper-large-v3", "local"): 0.0,
    # Human-provided fallback — no AI cost
    ("human-provided", "user_upload"): 0.0,
}

# Fallback for tools we haven't priced yet. Conservative.
DEFAULT_COST_PER_CALL = 0.01


def cost_for_record(tool_name: str, tool_provider: str) -> float:
    """Best-effort cost estimate for a single AIProvenance record."""
    return COST_PER_CALL.get((tool_name, tool_provider), DEFAULT_COST_PER_CALL)


def tenant_cost_summary(
    db: Session,
    tenant_id: str,
    since_days: int = 30,
) -> dict:
    """Summarize AI cost for one tenant over the last `since_days` days.

    Returns:
        {
          "tenant_id": str,
          "since": iso8601 timestamp,
          "total_cost": float (USD),
          "total_calls": int,
          "by_tool": [{"tool_name", "tool_provider", "calls", "cost"}, ...],
        }

    Joins AIProvenance with Job to filter by tenant_id.
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    rows = (
        db.query(
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(Job.tenant_id == tenant_id)
        .filter(AIProvenance.created_at >= since)
        .group_by(AIProvenance.tool_name, AIProvenance.tool_provider)
        .all()
    )

    by_tool = []
    total_cost = 0.0
    total_calls = 0
    for tool_name, tool_provider, calls in rows:
        rate = cost_for_record(tool_name, tool_provider)
        cost = calls * rate
        total_cost += cost
        total_calls += calls
        by_tool.append({
            "tool_name": tool_name,
            "tool_provider": tool_provider,
            "calls": calls,
            "rate_per_call": rate,
            "cost": round(cost, 4),
        })

    by_tool.sort(key=lambda r: r["cost"], reverse=True)

    return {
        "tenant_id": tenant_id,
        "since": since.isoformat(),
        "since_days": since_days,
        "total_cost": round(total_cost, 4),
        "total_calls": total_calls,
        "by_tool": by_tool,
    }


def record_ai_call(
    job_id: str,
    step: str,
    tool_name: str,
    tool_provider: str,
    prompt: str,
    input_data_types: list[str] = None,
    tool_version: str = None,
):
    """Start recording an AI call. Returns a ProvenanceRecorder.

    Usage:
        recorder = record_ai_call(job_id, "video_bg", "veo-3.1-generate-001", ...)
        # ... make the API call ...
        recorder.finish(response_summary="...", output_artifact="/path/to/file.mp4")
    """
    return ProvenanceRecorder(
        job_id=job_id,
        step=step,
        tool_name=tool_name,
        tool_provider=tool_provider,
        prompt=prompt,
        input_data_types=input_data_types,
        tool_version=tool_version,
    )


class ProvenanceRecorder:
    """Records a single AI tool invocation with timing."""

    def __init__(self, job_id, step, tool_name, tool_provider, prompt,
                 input_data_types=None, tool_version=None):
        self.job_id = job_id
        self.step = step
        self.tool_name = tool_name
        self.tool_provider = tool_provider
        self.prompt = prompt
        self.input_data_types = input_data_types
        self.tool_version = tool_version
        self.start_time = time.time()

    def finish(self, response_summary: str = None, output_artifact: str = None):
        """Persist the provenance record to the database."""
        duration_ms = int((time.time() - self.start_time) * 1000)
        db = SessionLocal()
        try:
            record = AIProvenance(
                job_id=self.job_id,
                step=self.step,
                tool_name=self.tool_name,
                tool_provider=self.tool_provider,
                tool_version=self.tool_version,
                prompt_sent=self.prompt,
                prompt_hash=hashlib.sha256(self.prompt.encode()).hexdigest(),
                response_summary=response_summary[:2000] if response_summary else None,
                input_data_types=self.input_data_types,
                output_artifact=output_artifact,
                duration_ms=duration_ms,
            )
            db.add(record)
            db.commit()
            logger.info(
                f"Provenance recorded: job={self.job_id} step={self.step} "
                f"tool={self.tool_name} duration={duration_ms}ms"
            )
        except Exception as e:
            logger.error(f"Failed to record provenance: {e}")
            db.rollback()
        finally:
            db.close()
