import asyncio
import threading

from stage1_audio_parallel import run_asr_with_pending_reference


def test_asr_and_complete_audio_reference_overlap():
    barrier = threading.Barrier(2, timeout=2)

    def asr():
        barrier.wait()
        return [{"text": "heard"}]

    def reference():
        barrier.wait()
        return "audio-derived hypothesis"

    async def run():
        reference_task = asyncio.create_task(asyncio.to_thread(reference))
        return await run_asr_with_pending_reference(asr, reference_task)

    asr_result, reference_result = asyncio.run(run())
    assert asr_result == [{"text": "heard"}]
    assert reference_result == "audio-derived hypothesis"


def test_reference_failure_does_not_discard_valid_asr():
    def asr():
        return [{"text": "heard", "start": 0, "end": 1}]

    def reference():
        raise RuntimeError("provider unavailable")

    async def run():
        reference_task = asyncio.create_task(asyncio.to_thread(reference))
        return await run_asr_with_pending_reference(asr, reference_task)

    asr_result, reference_result = asyncio.run(run())
    assert asr_result == [{"text": "heard", "start": 0, "end": 1}]
    assert isinstance(reference_result, RuntimeError)
