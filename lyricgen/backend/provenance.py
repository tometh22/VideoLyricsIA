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
    # Veo video — Fast (no audio) at $0.10/s × 8s = $0.80 per call.
    # Standard models kept for backwards-compat with existing job rows.
    ("veo-3.1-fast-generate-001", "google_vertex"): 0.80,
    ("veo-3.1-generate-001", "google_vertex"): 3.20,
    ("veo-3.0-fast-generate-001", "google_vertex"): 0.80,
    ("veo-3.0-generate-001", "google_vertex"): 3.20,
    ("veo-2.0-generate-001", "google_vertex"): 4.00,
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
    # Whisper OpenAI API — billed at $0.006/min of audio. We pay per call,
    # but it bills per-minute; using an average song length of ~3.5 min
    # gives ~$0.021 per call. Cheap to bump if real audio lengths drift.
    ("whisper-1", "openai"): 0.021,
    ("whisper", "openai"): 0.021,
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


# Provider buckets for the global dashboard. Maps the tool_provider /
# tool_name prefix to a short user-visible label. Keep aligned with the
# Veo / Gemini / Whisper / Imagen split shown in the cost panel.
_PROVIDER_BUCKETS = (
    ("veo",     lambda n, p: n.startswith("veo-")),
    ("gemini",  lambda n, p: n.startswith("gemini-")),
    ("imagen",  lambda n, p: n.startswith("imagen-")),
    ("whisper", lambda n, p: n.startswith("whisper") and p in ("openai", "local")),
    ("other",   lambda n, p: True),  # catch-all, must stay last
)


def _bucket_for(tool_name: str, tool_provider: str) -> str:
    for label, predicate in _PROVIDER_BUCKETS:
        if predicate(tool_name, tool_provider):
            return label
    return "other"


def cost_dashboard_global(db: Session, since_days: int = 30,
                          revenue_per_video_usd: float = 8.0) -> dict:
    """Margin-style cost dashboard across all tenants.

    Powers /admin/margin. Returns enough data for the operator panel to
    show: total AI spend, per-provider breakdown, video counts (done /
    pending_review / rejected / error), rejection rate, cost per
    deliverable, and a margin estimate against an assumed revenue per
    video. `revenue_per_video_usd` is just for the margin display — it
    does not affect cost numbers.

    Whisper calls are priced via the COST_PER_CALL table — the row
    `("whisper-1", "openai")` is what catches our prod transcriptions.
    Local Whisper stays at 0 (matches actual marginal cost).
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    # --- AI spend by tool/provider ---
    rows = (
        db.query(
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id).label("calls"),
        )
        .filter(AIProvenance.created_at >= since)
        .group_by(AIProvenance.tool_name, AIProvenance.tool_provider)
        .all()
    )

    by_tool = []
    by_provider: dict[str, dict] = {}
    total_cost = 0.0
    total_calls = 0
    for tool_name, tool_provider, calls in rows:
        rate = cost_for_record(tool_name, tool_provider)
        cost = calls * rate
        bucket = _bucket_for(tool_name, tool_provider)
        total_cost += cost
        total_calls += calls
        by_tool.append({
            "tool_name": tool_name,
            "tool_provider": tool_provider,
            "calls": calls,
            "rate_per_call": rate,
            "cost": round(cost, 4),
            "provider_bucket": bucket,
        })
        agg = by_provider.setdefault(
            bucket, {"calls": 0, "cost": 0.0}
        )
        agg["calls"] += calls
        agg["cost"] += cost

    by_tool.sort(key=lambda r: r["cost"], reverse=True)
    by_provider_list = sorted(
        [{"provider": k, **v, "cost": round(v["cost"], 4)}
         for k, v in by_provider.items()],
        key=lambda r: r["cost"],
        reverse=True,
    )

    # --- Video counts (same window so the cost-per-video math is honest) ---
    video_counts = dict(
        db.query(Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.status)
        .all()
    )
    done = int(video_counts.get("done", 0))
    pending = int(video_counts.get("pending_review", 0))
    rejected = int(video_counts.get("rejected", 0))
    error = int(video_counts.get("error", 0))
    finished = done + pending + rejected + error
    deliverable = done + pending

    # Avoid divide-by-zero in fresh tenants / very short windows.
    cost_per_done = round(total_cost / done, 4) if done else None
    cost_per_deliverable = (
        round(total_cost / deliverable, 4) if deliverable else None
    )
    rejection_rate = round(rejected / finished, 4) if finished else None

    # Margin against a revenue assumption. Pure display math — caller
    # passes revenue_per_video_usd; default $8 reflects the Universal
    # contract ($2,000 / 250 videos).
    margin_per_video = None
    margin_total = None
    if cost_per_deliverable is not None and revenue_per_video_usd > 0:
        margin_per_video = round(
            revenue_per_video_usd - cost_per_deliverable, 4
        )
        margin_total = round(
            (revenue_per_video_usd - cost_per_deliverable) * deliverable, 2
        )

    # --- Per-tenant breakdown ---
    # Cost: sum cost_for_record across each tenant's provenance rows.
    tenant_rows = (
        db.query(
            Job.tenant_id,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
        .group_by(Job.tenant_id, AIProvenance.tool_name, AIProvenance.tool_provider)
        .all()
    )
    tenant_cost: dict[str, dict] = {}
    for tenant_id, tool_name, tool_provider, calls in tenant_rows:
        agg = tenant_cost.setdefault(
            tenant_id, {"calls": 0, "cost": 0.0}
        )
        rate = cost_for_record(tool_name, tool_provider)
        agg["calls"] += calls
        agg["cost"] += calls * rate

    # Video status counts per tenant (same window).
    tenant_status_rows = (
        db.query(Job.tenant_id, Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.tenant_id, Job.status)
        .all()
    )
    tenant_status: dict[str, dict[str, int]] = {}
    for tenant_id, status, n in tenant_status_rows:
        tenant_status.setdefault(tenant_id, {})[status] = int(n)

    # Union of tenants seen in spend OR jobs so a 0-cost tenant with
    # jobs still shows up (and vice versa).
    by_tenant = []
    for tid in set(tenant_cost.keys()) | set(tenant_status.keys()):
        spend = tenant_cost.get(tid, {"calls": 0, "cost": 0.0})
        sts = tenant_status.get(tid, {})
        t_done = int(sts.get("done", 0))
        t_pending = int(sts.get("pending_review", 0))
        t_rejected = int(sts.get("rejected", 0))
        t_error = int(sts.get("error", 0))
        t_finished = t_done + t_pending + t_rejected + t_error
        t_deliverable = t_done + t_pending
        by_tenant.append({
            "tenant_id": tid,
            "calls": spend["calls"],
            "cost": round(spend["cost"], 4),
            "done": t_done,
            "pending_review": t_pending,
            "rejected": t_rejected,
            "error": t_error,
            "deliverable": t_deliverable,
            "cost_per_deliverable": (
                round(spend["cost"] / t_deliverable, 4)
                if t_deliverable else None
            ),
            "rejection_rate": (
                round(t_rejected / t_finished, 4) if t_finished else None
            ),
        })
    by_tenant.sort(key=lambda r: r["cost"], reverse=True)

    # --- Per-user breakdown ---
    # Same pattern but grouped by Job.user_id + joined to User for the
    # display name. Users without finished jobs in the window get
    # filtered (no useful display).
    from database import User as UserModel
    user_rows = (
        db.query(
            Job.user_id,
            Job.tenant_id,
            UserModel.username,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .outerjoin(UserModel, UserModel.id == Job.user_id)
        .filter(AIProvenance.created_at >= since)
        .group_by(Job.user_id, Job.tenant_id, UserModel.username,
                  AIProvenance.tool_name, AIProvenance.tool_provider)
        .all()
    )
    user_cost: dict = {}
    for user_id, tenant_id, username, tool_name, tool_provider, calls in user_rows:
        key = (user_id, tenant_id)
        agg = user_cost.setdefault(key, {
            "user_id": user_id,
            "username": username,
            "tenant_id": tenant_id,
            "calls": 0,
            "cost": 0.0,
        })
        rate = cost_for_record(tool_name, tool_provider)
        agg["calls"] += calls
        agg["cost"] += calls * rate

    user_status_rows = (
        db.query(Job.user_id, Job.tenant_id, Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.user_id, Job.tenant_id, Job.status)
        .all()
    )
    user_status: dict = {}
    for user_id, tenant_id, status, n in user_status_rows:
        key = (user_id, tenant_id)
        user_status.setdefault(key, {})[status] = int(n)

    by_user = []
    for key in set(user_cost.keys()) | set(user_status.keys()):
        spend = user_cost.get(key, {
            "user_id": key[0],
            "username": None,
            "tenant_id": key[1],
            "calls": 0,
            "cost": 0.0,
        })
        sts = user_status.get(key, {})
        u_done = int(sts.get("done", 0))
        u_pending = int(sts.get("pending_review", 0))
        u_rejected = int(sts.get("rejected", 0))
        u_error = int(sts.get("error", 0))
        u_finished = u_done + u_pending + u_rejected + u_error
        u_deliverable = u_done + u_pending
        by_user.append({
            "user_id": spend["user_id"],
            "username": spend["username"],
            "tenant_id": spend["tenant_id"],
            "calls": spend["calls"],
            "cost": round(spend["cost"], 4),
            "done": u_done,
            "pending_review": u_pending,
            "rejected": u_rejected,
            "error": u_error,
            "deliverable": u_deliverable,
            "cost_per_deliverable": (
                round(spend["cost"] / u_deliverable, 4)
                if u_deliverable else None
            ),
            "rejection_rate": (
                round(u_rejected / u_finished, 4) if u_finished else None
            ),
        })
    by_user.sort(key=lambda r: r["cost"], reverse=True)

    return {
        "since": since.isoformat(),
        "since_days": since_days,
        "total_cost": round(total_cost, 4),
        "total_calls": total_calls,
        "by_tool": by_tool,
        "by_provider": by_provider_list,
        "by_tenant": by_tenant,
        "by_user": by_user,
        "video_counts": {
            "done": done,
            "pending_review": pending,
            "rejected": rejected,
            "error": error,
            "finished": finished,
            "deliverable": deliverable,
        },
        "cost_per_done": cost_per_done,
        "cost_per_deliverable": cost_per_deliverable,
        "rejection_rate": rejection_rate,
        "revenue_per_video_usd": revenue_per_video_usd,
        "margin_per_video": margin_per_video,
        "margin_total": margin_total,
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
    """Records a single AI tool invocation with timing.

    The row is INSERTED at construction (with response_summary/duration_ms
    left null to mark the call as in-flight) and UPDATED at finish().
    Pre-fix the row was inserted only at finish(), so any worker crash
    between record_ai_call(...) and recorder.finish(...) — Veo polling
    timeout, OOM kill, network error during download — left no trace of
    the API call in the audit trail. Now the call is always recorded;
    in-flight rows simply have null duration_ms / response_summary.

    Also a context manager: `with record_ai_call(...) as rec:` will call
    finish() automatically, including a synthetic error summary on the
    exception path.
    """

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
        self._row_id: int | None = None
        self._finished = False

        # Insert the "started" row immediately. If the DB hiccups here we
        # log and continue with _row_id=None; finish() then becomes a
        # no-op so provenance bookkeeping never raises out of the worker.
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
                response_summary=None,
                input_data_types=self.input_data_types,
                output_artifact=None,
                duration_ms=None,
            )
            db.add(record)
            db.commit()
            self._row_id = record.id
        except Exception as e:
            logger.error(f"Failed to insert provenance start row: {e}")
            db.rollback()
        finally:
            db.close()

    def finish(self, response_summary: str = None, output_artifact: str = None):
        """Update the in-flight provenance row with end-of-call data.

        Idempotent — calling finish() twice is safe. No-op when the
        initial INSERT failed (self._row_id is None).
        """
        if self._finished or self._row_id is None:
            self._finished = True
            return
        self._finished = True
        duration_ms = int((time.time() - self.start_time) * 1000)
        db = SessionLocal()
        try:
            updated = (
                db.query(AIProvenance)
                .filter(AIProvenance.id == self._row_id)
                .update(
                    {
                        "response_summary": response_summary[:2000] if response_summary else None,
                        "output_artifact": output_artifact,
                        "duration_ms": duration_ms,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated:
                logger.info(
                    f"Provenance recorded: job={self.job_id} step={self.step} "
                    f"tool={self.tool_name} duration={duration_ms}ms"
                )
        except Exception as e:
            logger.error(f"Failed to update provenance row: {e}")
            db.rollback()
        finally:
            db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._finished:
            return False
        if exc is not None:
            try:
                self.finish(response_summary=f"error: {exc!r}")
            except Exception:
                pass
        else:
            self.finish()
        return False  # never swallow exceptions
