"""AI Provenance recording — tracks every AI tool invocation for UMG compliance."""

import hashlib
import logging
import time

from database import SessionLocal, AIProvenance

logger = logging.getLogger("genly.provenance")


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
