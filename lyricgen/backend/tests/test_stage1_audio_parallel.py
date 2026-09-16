import asyncio
import threading
from pathlib import Path

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


def test_late_audio_reference_initializes_cleanup_fallback_state():
    source = (Path(__file__).parents[1] / "main.py").read_text()
    join = source.index("await run_asr_with_pending_reference")
    fallback = source.index("_cleaned\n", join)
    initialization = source.rfind("_cleaned = None", 0, fallback)

    assert 0 <= initialization < join < fallback


def test_auto_language_join_consumes_once_and_propagates_outcome():
    from stage1_audio_parallel import join_reference_for_language
    calls = []
    async def reference():
        calls.append('reference')
        return 'Yo tengo una razón para cambiar la noche'
    async def run():
        result = await join_reference_for_language(asyncio.create_task(reference()))
        calls.append('primary')
        return result
    assert asyncio.run(run()).startswith('Yo tengo')
    assert calls == ['reference', 'primary']


def test_auto_language_join_failure_still_allows_asr_without_retry():
    from stage1_audio_parallel import join_reference_for_language
    async def reference():
        raise RuntimeError('unavailable')
    async def run():
        return await join_reference_for_language(asyncio.create_task(reference()))
    assert isinstance(asyncio.run(run()), RuntimeError)


def test_batch_auto_joins_reference_before_language_resolution_and_never_prompts():
    source = (Path(__file__).parents[1] / 'main.py').read_text()
    join = source.index('_language_reference = await join_reference_for_language')
    resolve = source.index('_reference_for_language =', join)
    primary = source.index('if _wc_enabled:', resolve)
    assert join < resolve < primary
    assert 'or _batch_audio_only_reference)' in source
