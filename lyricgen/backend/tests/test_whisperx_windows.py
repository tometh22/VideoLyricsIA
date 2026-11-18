"""Regression: an uncertain Russian intro must not translate es/fr verses."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import whisperx_windows as windows
import whisperx_transcribe as wx
import replicate_budget as budget
from recognition_provenance import begin_collection, end_collection


def test_voice_windows_skip_long_intro_and_do_not_cross_verse_pause():
    planned = windows.plan_windows([(62, 81), (93, 112), (160, 179), (183, 193)], 254)
    assert planned == [(61.6, 81.4), (92.6, 112.4), (159.6, 179.4), (182.6, 193.4)]
    assert all(b - a <= 26 for a, b in planned)


def test_continuous_vocals_are_bounded_without_overlap_or_loss():
    planned = windows.plan_windows([(0, 72)], 72)
    assert planned == [(0, 26), (26, 52), (52, 72)]


def test_nearby_islands_merge_but_padding_never_overlaps():
    planned = windows.plan_windows([(10, 20), (20.2, 35), (35.2, 40)], 45)
    assert all(a[1] <= b[0] for a, b in zip(planned, planned[1:]))
    assert all(b - a <= 26 for a, b in planned)


def test_window_bounds_reject_nonfinite_and_preserve_short_voice():
    assert windows.plan_windows([(float('nan'), 4), (3, 3.2)], 5) == [(2.6, 3.6)]


def test_offsets_apply_once_to_words_and_reject_broken_bounds():
    raw = [dict(start=1, end=4, text="bonjour", words=[dict(word="bonjour", start=1, end=3.8)]),
           dict(start=0, end=float('nan'), text="invalid"),
           dict(start=20.1, end=20.2, text="starts outside window"),
           dict(start=0, end=50, text="outside window")]
    original = deepcopy(raw[0])
    result = windows.absolute_segments(raw, 160, 180)
    assert len(result) == 1
    assert result[0]['start'] == 161
    assert result[0]['words'][0]['end'] == 163.8
    assert raw[0] == original


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(windows, 'audio_windows', lambda _: [(60, 80), (160, 180)])
    def extract(args, **kwargs):
        from pathlib import Path
        Path(args[-1]).write_bytes(b'fake audio')
    monkeypatch.setattr(windows.subprocess, 'run', extract)
    calls = []
    results = [
        dict(detected_language='es', _language_detection_probability=.93,
             segments=[dict(start=2, end=5, text='Viaje hacia el corazón')]),
        dict(detected_language='fr', _language_detection_probability=.88,
             segments=[dict(start=1, end=6, text='Mon amour je viens chercher')]),
    ]
    def call(model, factory, **kwargs):
        inputs = factory()
        calls.append(({k: v for k, v in inputs.items() if k != 'audio_file'}, kwargs))
        inputs['audio_file'].close()
        return results[len(calls) - 1]
    monkeypatch.setattr(budget, 'call_with_budget', call)
    return calls, results


def test_each_window_autodetects_without_prompt_and_records_original_evidence(provider):
    calls, _ = provider
    collector, token = begin_collection()
    try:
        result = windows.transcribe_windows('audio.wav', 'provider:hash',
            {'task': 'transcribe', 'language': 'es', 'initial_prompt': 'wrong song'}, 480)
    finally:
        end_collection(token)
    assert [s['start'] for s in result['segments']] == [62, 161]
    assert 'Mon amour' in result['segments'][1]['text']
    assert all(inputs == {'task': 'transcribe'} for inputs, _ in calls)
    evidence = collector.snapshot()
    assert evidence['completed_attempt_count'] == 2
    raw = evidence['hypotheses'][1]['events'][0]
    assert raw['start'] == 1  # unmodified provider coordinates + explicit window
    assert raw['language_detection']['window_start'] == 160
    assert raw['language_detection']['detected_language'] == 'fr'
    assert raw['language_detection']['language_probability'] == .88


def test_low_confidence_retries_only_its_window_without_forcing_language(provider, monkeypatch):
    calls, results = provider
    results[0].update(detected_language='ru', _language_detection_probability=.25)
    results[0]['segments'][0]['text'] = 'Субтитры сделал автор'
    retries = []
    def retry(path, remaining):
        retries.append(remaining)
        return [dict(start=2, end=5, text='Que hago en este tren')], 'spanish'
    monkeypatch.setattr(windows, '_independent_retry', retry)
    collector, token = begin_collection()
    try:
        result = windows.transcribe_windows('audio.wav', 'provider:hash', {}, 480)
    finally:
        end_collection(token)
    assert len(retries) == 1
    assert result['segments'][0]['text'] == 'Que hago en este tren'
    assert result['segments'][1]['start'] == 161
    assert collector.snapshot()['completed_attempt_count'] == 3


def test_partial_provider_failure_is_not_published_as_complete(provider, monkeypatch):
    _, results = provider
    results[1] = None
    def unavailable(*args):
        raise RuntimeError('provider unavailable')
    monkeypatch.setattr(windows, '_independent_retry', unavailable)
    with pytest.raises(RuntimeError, match='refusing partial'):
        windows.transcribe_windows('audio.wav', 'provider:hash', {}, 480)


def test_deadline_shared_across_windows(provider, monkeypatch):
    calls, _ = provider
    ticks = iter([0, 1, 2, 481])
    monkeypatch.setattr(windows.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError, match='budget exhausted'):
        windows.transcribe_windows('audio.wav', 'provider:hash', {}, 480)
    assert len(calls) == 1
    assert calls[0][1]['total_budget_s'] == 478


def test_auto_wrapper_bypasses_legacy_cache_and_does_not_double_record(provider, monkeypatch, tmp_path):
    audio = tmp_path / 'audio.wav'; audio.write_bytes(b'fake')
    monkeypatch.setattr(wx, 'is_enabled', lambda: True)
    monkeypatch.setattr(wx, '_cache_lookup', lambda *a, **kw: pytest.fail('legacy auto cache used'))
    monkeypatch.setattr(wx, '_cache_write', lambda *a, **kw: pytest.fail('composite raw evidence lost'))
    monkeypatch.setattr(wx, '_vad_split_adlib_segments', lambda s, *a: s)
    monkeypatch.setattr(wx, '_correct_post_adlib_timing', lambda s, *a: s)
    import acoustic_match
    monkeypatch.setattr(acoustic_match, 'correct_by_acoustic_similarity', lambda s, *a: s)
    collector, token = begin_collection()
    try:
        result = wx.transcribe_whisperx(str(audio))
    finally:
        end_collection(token)
    assert len(result) == 2
    assert collector.snapshot()['completed_attempt_count'] == 2


def test_confidence_is_captured_without_provider_logs(monkeypatch):
    import sys
    prediction = SimpleNamespace(status='succeeded', output={'segments': []},
        logs='URL secret\nDetected language: ru (0.25) in first 30s of audio', reload=lambda: None)
    monkeypatch.setitem(sys.modules, 'replicate', SimpleNamespace(
        predictions=SimpleNamespace(create=lambda **kw: prediction)))
    monkeypatch.setattr(budget._t, 'sleep', lambda _: None)
    result = budget._run_predict_with_progress('model:hash', {}, deadline=float('inf'),
        call_label='WHISPERX', on_progress=lambda _: None)
    assert result == {'segments': [], '_language_detection_probability': .25}
    assert prediction.output == {'segments': []}


def test_provider_failure_recovers_only_failed_window(provider, monkeypatch):
    _, results = provider
    results[0] = None
    monkeypatch.setattr(windows, '_independent_retry', lambda *a: (
        [dict(start=2, end=5, text='Camino hacia el sol')], 'spanish'))
    result = windows.transcribe_windows('audio.wav', 'provider:hash', {}, 480)
    assert [s['start'] for s in result['segments']] == [62, 161]


def test_reference_conflict_retry_uses_audio_and_requires_improvement(provider, monkeypatch):
    reference = 'The morning light is shining\nWe are walking through the city\nYou will find your way home'
    wrong = [dict(start=61, end=66, text='La noche oscura cae sobre nosotros'),
             dict(start=67, end=72, text='Tengo frío cerca del mar'),
             dict(start=73, end=78, text='Quiero volver sin mirar atrás')]
    calls = []
    correct = [dict(start=1, end=6, text='The morning light is shining'),
               dict(start=7, end=12, text='We are walking through the city'),
               dict(start=13, end=18, text='You will find your way home')]
    def retry(*args):
        calls.append(args)
        return correct, 'english'
    monkeypatch.setattr(windows, '_independent_retry', retry)
    result = windows.recover_reference_conflict('audio.wav', wrong, reference)
    assert result != wrong
    assert result[0]['start'] == 61
    assert len(calls) == 1
    assert len(calls[0]) == 2  # audio file + deadline, no lyric text/language
    monkeypatch.setattr(windows, '_independent_retry', lambda *a: (
        [dict(start=1, end=3, text='Thanks for watching')], 'english'))
    assert windows.recover_reference_conflict('audio.wav', wrong, reference) is wrong


def test_matching_reference_never_causes_extra_provider_call(provider, monkeypatch):
    text = 'Je marche dans cette nuit avec toi'
    segments = [dict(start=161, end=167, text=text)]
    monkeypatch.setattr(windows, '_independent_retry', lambda *a: pytest.fail('unexpected retry'))
    assert windows.recover_reference_conflict('audio.wav', segments, text) is segments


def test_conflict_recovery_failure_keeps_original_for_quality_review(provider, monkeypatch):
    reference = 'The morning light is shining\nWe are walking through the city\nYou will find your way home'
    segments = [dict(start=61+i*4, end=65+i*4, text='El corazón vive cerca del mar') for i in range(3)]
    def fail(*args):
        raise TimeoutError('provider unavailable')
    monkeypatch.setattr(windows, '_independent_retry', fail)
    assert windows.recover_reference_conflict('audio.wav', segments, reference) is segments


def test_recovery_includes_tail_of_valid_phrase_crossing_window_boundary(provider, monkeypatch):
    monkeypatch.setattr(windows, 'audio_windows', lambda _: [(60, 80), (80, 100)])
    reference = 'The morning light is shining\nWe are walking through the city\nYou will find your way home'
    segments = [dict(start=61+i*4, end=65+i*4, text='El corazón vive cerca del mar') for i in range(3)]
    segments.append(dict(start=77, end=85, text='You will find your way home'))
    calls = []
    def retry(path, remaining):
        calls.append(path)
        return [dict(start=1, end=5, text='The morning light is shining')], 'english'
    monkeypatch.setattr(windows, '_independent_retry', retry)
    windows.recover_reference_conflict('audio.wav', segments, reference)
    assert len(calls) == 2
