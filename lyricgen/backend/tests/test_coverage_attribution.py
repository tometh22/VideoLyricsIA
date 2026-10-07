"""Compare ASR coverage with ASR coverage, preserving the voiced-gap gate."""
import logging

import audio_coverage
import line_signals_v2
import transcription_quality
import transcription_worker as worker


def measured(monkeypatch, *, word_coverage, voiced_coverage=.8):
    report = {'audio_coverage': voiced_coverage, 'asr_word_coverage': word_coverage,
              'voiced_coverage': voiced_coverage, 'uncovered_spans': 0,
              'uncovered_seconds': 0., 'worst_span_s': 0., 'text_mismatches': 0,
              'voiced_gaps': 1, 'voiced_gap_s': 12.}
    monkeypatch.setattr(audio_coverage, 'summarize', lambda *_a, **_kw: dict(report))
    monkeypatch.setattr(audio_coverage, 'voiced_gaps', lambda *_a, **_kw: [])
    monkeypatch.setattr(transcription_quality, 'build_unsafe_windows', lambda *_a, **_kw: [])
    monkeypatch.setattr(line_signals_v2, 'enabled', lambda: False)
    return report


def result():
    return {'audio_coverage': 1.,
            'segments': [{'start': 1., 'end': 2., 'text': 'alpha'}],
            '_asr_words': [{'start': 1., 'end': 2., 'word': 'alpha'}]}


def test_voiced_gap_does_not_falsely_blame_formatter_or_postpasses(monkeypatch, caplog):
    measured(monkeypatch, word_coverage=1.)
    caplog.set_level(logging.INFO, logger='genly.transcription_worker')
    after = worker._medir_cobertura_final(result(), 'synthetic', 1., strip_internal=False)
    assert after['audio_coverage'] == .8
    assert after['coverage_warning'] is True
    assert 'CIRCUIT BREAKER' in caplog.text
    assert 'perdieron' not in caplog.text
    assert 'FORMATTER perdió' not in caplog.text


def test_actual_recognized_word_loss_is_still_reported(monkeypatch, caplog):
    measured(monkeypatch, word_coverage=.5)
    caplog.set_level(logging.INFO, logger='genly.transcription_worker')
    worker._medir_cobertura_final(result(), 'synthetic', 1., strip_internal=False)
    assert 'palabras reconocidas' in caplog.text
    assert '50%' in caplog.text


def test_remeasurement_keeps_the_original_asr_comparator(monkeypatch, caplog):
    metrics = measured(monkeypatch, word_coverage=1.)
    value = worker._medir_cobertura_final(result(), 'synthetic', 1., strip_internal=False)
    metrics['asr_word_coverage'] = .9
    caplog.set_level(logging.INFO, logger='genly.transcription_worker')
    worker._medir_cobertura_final(value, 'synthetic', 1., strip_internal=False)
    assert value['postpass_stats']['cascade_asr_word_coverage'] == 1.
    assert '10%' in caplog.text
