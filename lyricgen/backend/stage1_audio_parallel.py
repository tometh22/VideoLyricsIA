"""Bounded concurrency primitive for audio-only stage-1 evidence.

Both callables receive the same immutable recording through their closure.
The ASR remains blind; the independently derived reference is joined only
after recognition finishes, before attestation and line reconciliation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def run_asr_with_pending_reference(
    asr_call: Callable[[], Any],
    reference_task: asyncio.Task,
) -> tuple[Any, Any]:
    """Run blocking ASR while an audio-reference task is already in flight.

    Exceptions are returned to the caller independently.  A Gemini failure
    therefore cannot discard a valid ASR result, and an ASR failure still
    waits for/captures the reference outcome without leaking an orphan task.
    """
    asr_result, reference_result = await asyncio.gather(
        asyncio.to_thread(asr_call),
        reference_task,
        return_exceptions=True,
    )
    return asr_result, reference_result
