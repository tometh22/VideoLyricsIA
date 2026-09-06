import math
import json
import numpy as np
import pytest

from reviewer_integral import windows, union_seconds, locate_words, spectral_continuity


def test_full_coverage_including_tail_without_flags():
    plan = windows(289.413333)
    assert union_seconds([(w['start'], w['end']) for w in plan]) == 289.4133
    assert all(w['end'] - w['start'] <= 24 for w in plan)
    assert all(a['end'] - b['start'] == 6 for a, b in zip(plan, plan[1:]))
    assert plan[-1]['end'] == 289.413333


@pytest.mark.parametrize('duration', [0, -1, math.inf, math.nan])
def test_invalid_duration_fails(duration):
    with pytest.raises(ValueError):
        windows(duration)


def test_union_does_not_double_count_overlaps_or_failed_regions():
    assert union_seconds([(0, 24), (18, 42), (60, 65)]) == 47


def test_repetitions_not_confused_by_text_match():
    req = {'tool_status': 'ok', 'response': {'words': [
        {'word': 'aquí', 'start': 1, 'end': 2}, {'word': 'aquí', 'start': 7, 'end': 8}]}}
    loc = locate_words('aquí', req, {'start': 100, 'end': 110}, {'start': 106, 'end': 109})
    assert loc['selected']['start'] == 107
    assert len(loc['occurrences']) == 2
    assert not loc['correctness_certified']
    both = locate_words('aquí', req, {'start': 100, 'end': 110}, {'start': 100, 'end': 109})
    assert both['status'] == 'occurrence_ambiguous'


def test_recognition_failure_not_correctness():
    assert locate_words('hola', {'tool_status': 'tool_error'}, {}, {})['status'] == 'recognition_failed'


def test_spectral_proxy_does_not_manufacture_endpoint_without_change():
    rate = 16000
    t = np.arange(rate * 2) / rate
    wave = (.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    result = spectral_continuity(wave, rate, .2, .6, 1.6)
    assert result['candidate_end'] is None
    assert not result['target_voice_verified']
    assert not result['phonetic_end_supported']


def test_spectral_proxy_detects_shape_change_without_pitch_detector():
    rate = 16000
    t = np.arange(rate * 2) / rate
    wave = np.where(t < 1., .1 * np.sin(2 * np.pi * 220 * t),
                    .1 * np.sin(2 * np.pi * 1800 * t)).astype(np.float32)
    result = spectral_continuity(wave, rate, .2, .6, 1.6)
    assert .9 < result['candidate_end'] < 1.1
    assert not result['automatic_apply_allowed']


def test_prepare_exports_only_prehuman_revision(tmp_path):
    from scripts.review_integral_audio import prepare
    from shadow_reference_import import digest
    old = [{'text': 'antes', 'start': 1., 'end': 2.}]
    job = {'job_id': 'e926daf14d7a', 'audio_sha256': 'a' * 64,
           'audio_revision': 'source', 'duration_seconds': 3., 'original_revision': 0,
           'original_segments': old, 'original_sha256': digest(old),
           'segments': [{'text': 'HUMAN_SECRET', 'start': 1., 'end': 2.8, 'locked': True}]}
    (tmp_path / 'snapshot.json').write_text(json.dumps({'jobs': [job]}))
    prepare(tmp_path, tmp_path / 'experiment')
    exported = (tmp_path / 'experiment/input.json').read_text()
    assert 'HUMAN_SECRET' not in exported
    assert json.loads(exported)['segments'] == old
    assert json.loads(exported)['segments_revision'] == 0


def test_evaluation_refuses_changed_frozen_results_before_reading_humans(tmp_path):
    from scripts.review_integral_audio import evaluate
    (tmp_path / 'freeze.json').write_text(json.dumps({'prediction_sha256': 'wrong'}))
    (tmp_path / 'predictions-frozen.json').write_text('{}')
    with pytest.raises(ValueError, match='predictions_changed_after_freeze'):
        evaluate(tmp_path / 'no_human_data_access', tmp_path)
