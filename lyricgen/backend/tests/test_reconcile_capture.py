"""No paid calls: the ordinary pipeline is observed, never re-executed live."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import reconcile_capture as rc
from machine_evidence import build_machine_evidence, finalize_machine_evidence, validate_machine_evidence, MachineSnapshotMissing

BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def enable(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('RECONCILE_CAPTURE_ENABLED', '1')
    monkeypatch.setenv('RECONCILE_CAPTURE_COHORT', 'six-prospective-jobs')
    monkeypatch.setenv('RECONCILE_CAPTURE_UNTIL', (NOW + timedelta(days=1)).isoformat())


def capture(monkeypatch, tmp_path):
    enable(monkeypatch)
    audio = tmp_path / 'mix.wav'
    audio.write_bytes(b'local fixture; no decoding')
    return rc.begin('job-fixture', str(audio), route_context={'live': False}, now=NOW, reserve=lambda *args: 1)


def test_disabled_production_expired_and_reservation_failure_do_nothing(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('disabled capture attempted IO')
    monkeypatch.setattr(rc, '_digest', forbidden)
    monkeypatch.setenv('ENVIRONMENT', 'production')
    assert rc.begin('j', '/missing', route_context={}, now=NOW, reserve=forbidden).snapshot() is None
    enable(monkeypatch)
    monkeypatch.setenv('RECONCILE_CAPTURE_UNTIL', NOW.isoformat())
    assert rc.begin('j', '/missing', route_context={}, now=NOW, reserve=forbidden).snapshot() is None
    enable(monkeypatch)
    assert rc.begin('j', '/missing', route_context={}, now=NOW, reserve=lambda *a: 0).snapshot() is None
    assert rc.begin('j', '/missing', route_context={}, now=NOW, reserve=forbidden).snapshot() is None


def test_copy_audio_config_and_code_identity(monkeypatch, tmp_path):
    trace = capture(monkeypatch, tmp_path)
    assert trace.payload is not None
    source = [{'text': 'Pero antes de partir', 'start': 1., 'end': 3., 'words': []}]
    trace.record('reconcile_input', {'wx_segs': source, 'reference_text': 'exact\ntext'})
    source[0]['words'].append({'word': 'external mutation'})
    snapshot = trace.snapshot()
    assert snapshot['stages'][0]['value']['wx_segs'][0]['words'] == []
    assert snapshot['stages'][0]['value']['reference_text'] == 'exact\ntext'
    assert len(snapshot['audio']['uploaded_input']['sha256']) == 64
    assert 'whisperx_reconcile.py' in snapshot['code_sha256']
    assert 'REPLICATE_API_TOKEN' not in snapshot['configuration']
    snapshot['stages'].clear()
    assert trace.snapshot()['stages']


def test_overflow_and_nonfinite_are_explicit_not_silent_partial_success(monkeypatch, tmp_path):
    trace = capture(monkeypatch, tmp_path)
    monkeypatch.setattr(rc, 'MAX_BYTES', 1)
    trace.record('input', [1])
    assert trace.snapshot()['incomplete'] == 'ValueError'
    trace.record('ignored_after_limit', [2])
    assert trace.snapshot()['stages'] == []
    trace = rc.Capture({'stages': [], 'audio': {}})
    monkeypatch.setattr(rc, 'MAX_BYTES', 10000)
    trace.record('input', [float('nan')])
    assert trace.snapshot()['incomplete'] == 'ValueError'


def test_snapshot_uses_existing_hashed_evidence_without_recognition_attempt(monkeypatch, tmp_path):
    trace = capture(monkeypatch, tmp_path)
    selected = [{'start': 1., 'end': 2., 'text': 'hola mundo'}]
    trace.record('presentation_output', selected)
    result = {'segments': selected, '_reconcile_capture': trace.snapshot(),
              '_primary_asr_family': 'fixture-asr', '_asr_words': [{'word': 'hola', 'start': 1., 'end': 2.}]}
    evidence = build_machine_evidence(result)
    assert evidence['capture']['recognition_attempt_count'] == 1
    stored = finalize_machine_evidence(evidence, original_segments=selected, quality={'decision': 'review'}, audio_sha256='a' * 64, audio_revision=1)
    validate_machine_evidence(stored, selected)
    stages = stored['capture']['reconcile_stages']['stages']
    assert stages[-2] == {'stage': 'final_machine_document', 'value': selected}
    stored['capture']['reconcile_stages']['audio']['uploaded_input']['sha256'] = 'b' * 64
    with pytest.raises(MachineSnapshotMissing):
        validate_machine_evidence(stored, selected)


def helper(name, namespace):
    tree = ast.parse((BACKEND / 'main.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_main_helper', 'exec'), namespace)
    return namespace[name]


def test_actual_reconcile_hook_does_not_run_twice_or_modify_output(monkeypatch, tmp_path):
    import whisperx_reconcile
    trace = capture(monkeypatch, tmp_path)
    words = [{'word': w, 'start': i, 'end': i + .4} for i, w in enumerate('hola mundo hasta mañana sigue cantando noche clara'.split())]
    original = [{'start': 0., 'end': 8., 'text': '', 'words': words}]
    canonical = 'hola mundo\nhasta mañana\nsigue cantando\nnoche clara'
    expected = whisperx_reconcile.reconcile(deepcopy(original), canonical)
    real = whisperx_reconcile.reconcile
    calls = []
    def counted(*args):
        calls.append(1)
        return real(*args)
    monkeypatch.setattr(whisperx_reconcile, 'reconcile', counted)
    invoke = helper('_captured_reconcile', {'_capture': trace, 'lang': 'es'})
    result = invoke(original, canonical, route='audio_truth_catalog', alignment_path=str(tmp_path / 'mix.wav'))
    assert result == expected
    assert len(calls) == 1
    stages = trace.snapshot()['stages']
    assert stages[0]['value']['wx_segs'] == original
    assert stages[1]['value'] == expected
    result[0]['text'] = 'mutation after capture'
    assert trace.snapshot()['stages'][1]['value'][0]['text'] == 'hola mundo'


def test_actual_presentation_hook_observes_existing_beat_detection_once(monkeypatch, tmp_path):
    import beat_snap, chorus_trim, lead_in
    from whisperx_transcribe import _split_long_segments
    trace = capture(monkeypatch, tmp_path)
    monkeypatch.setenv('BEAT_SNAP_ENABLED', '1')
    calls = []
    def detect(path):
        calls.append(path)
        return 120., [1., 3.]
    monkeypatch.setattr(beat_snap, 'detect_beats', detect)
    snap = helper('_snap', {'_capture': trace, '_split_long': _split_long_segments,
                           '_beat_snap': beat_snap, '_chorus_trim': chorus_trim,
                           '_lead_in': lead_in, 'tmp_path': 'ordinary-audio'})
    source = [{'start': 1.03, 'end': 2., 'text': 'hola'}, {'start': 3.03, 'end': 4., 'text': 'hola'}]
    before = deepcopy(source)
    result = snap(source)
    assert source == before
    assert calls == ['ordinary-audio']
    stages = {r['stage']: r['value'] for r in trace.snapshot()['stages']}
    assert stages['beat_context']['beats'] == [1., 3.]
    assert stages['lead_hold_output'] == result


def test_all_reconcile_calls_in_habitual_route_are_captured_and_private_key_removed():
    tree = ast.parse((BACKEND / 'main.py').read_text())
    func = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_transcription_for_job')
    raw_calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'reconcile']
    wrapper = next(n for n in ast.walk(func) if isinstance(n, ast.FunctionDef) and n.name == '_captured_reconcile')
    assert len(raw_calls) == 1 and wrapper.lineno < raw_calls[0].lineno < wrapper.end_lineno
    assert 'r.pop("_reconcile_capture", None)' in (BACKEND / 'transcription_worker.py').read_text()


def test_full_stage_replay_requires_exact_baseline_before_candidate(monkeypatch, tmp_path):
    from line_evidence import annotate_provider_evidence
    from transcribe_postprocess import dedup_collisions, normalize_words, strip_terminal_line_periods
    from segment_timing import normalize_segments_timing
    from post_reconcile import post_reconcile_cleanup
    from whisperx_transcribe import _split_long_segments
    import beat_snap, chorus_trim, lead_in, whisperx_reconcile
    from reconcile_replay import compare
    trace = capture(monkeypatch, tmp_path)
    monkeypatch.setenv('BEAT_SNAP_ENABLED', '0')
    words = [{'word': w, 'start': i, 'end': i + .4} for i, w in enumerate('hola mundo extra Pero antes de partir sigue cantando hasta mañana'.split())]
    wx = [{'start': 0, 'end': 11, 'text': '', 'words': words}]
    ref = 'hola mundo\nPero antes de partir\nsigue cantando\nhasta mañana'
    invoke = helper('_captured_reconcile', {'_capture': trace, 'lang': 'es'})
    rec = invoke(wx, ref, route='audio_truth_catalog', alignment_path=str(tmp_path / 'mix.wav'))
    trace.record('gap_cluster_output', rec)
    trace.record('cleanup_input', {'segments': rec, 'kwargs': {'split_long_lines': False}})
    clean = post_reconcile_cleanup(deepcopy(rec), split_long_lines=False)
    trace.record('cleanup_output', clean)
    trace.record('emit_input', {'segments': clean, 'source': 'whisperx_reconciled', 'reference_lyrics': ref, 'reference_id': 'fixture:song', 'content_reference_used': None})
    annotated = annotate_provider_evidence(clean, source='whisperx_reconciled', timing_source='whisperx_reconciled', content_source='catalog_reference', reference_text=ref, reference_id='fixture:song')
    trace.record('annotated_output', annotated)
    snap = helper('_snap', {'_capture': trace, '_split_long': _split_long_segments, '_beat_snap': beat_snap, '_chorus_trim': chorus_trim, '_lead_in': lead_in, 'tmp_path': 'fixture-audio'})
    final = normalize_segments_timing(strip_terminal_line_periods(snap(normalize_words(dedup_collisions(annotated)))))
    trace.record('presentation_output', final)
    trace.record('final_machine_document', final)
    report = compare(trace.snapshot(), whisperx_reconcile.reconcile, whisperx_reconcile.reconcile)
    assert report['checks'] == {'reconcile_exact': True, 'annotation_exact': True, 'presentation_exact': True}
    assert report['presentation_difference']['rows'] == []
    assert report['word_ownership_differences'][0]['rows'] == []
    assert report['post_presentation_changed'] is False
    trace.payload['stages'][-2]['value'][0]['end'] += .123
    def forbidden_candidate(*args, **kwargs):
        raise AssertionError('candidate executed after baseline mismatch')
    failed = compare(trace.snapshot(), whisperx_reconcile.reconcile, forbidden_candidate)
    assert failed['status'] == 'baseline_mismatch'
    assert failed['candidate_executed'] is False


def test_changed_layout_never_invents_occurrence_pairing():
    from reconcile_replay import difference
    a = [{'text': 'amor', 'start': 1, 'end': 2}, {'text': 'amor', 'start': 3, 'end': 4}]
    b = [{'text': 'amor', 'start': 1, 'end': 4}]
    assert difference(a, b)['association'] == 'unresolved-layout-or-text-change'


def test_selection_precedes_outputs_and_respects_reservation_limit(monkeypatch, tmp_path):
    enable(monkeypatch)
    audio = tmp_path / 'fixture'; audio.write_bytes(b'audio')
    members = set()
    def reservation(cohort, job, maximum, ttl):
        assert maximum == 6 and ttl == 86400
        if job in members or len(members) >= maximum:
            return 0
        members.add(job)
        return len(members)
    traces = [rc.begin(str(i), str(audio), route_context={}, now=NOW, reserve=reservation) for i in range(8)]
    assert [t.snapshot()['ordinal'] for t in traces if t.snapshot()] == list(range(1, 7))
    assert all(t.snapshot()['stages'] == [] for t in traces if t.snapshot())
    assert rc.begin('0', str(audio), route_context={}, now=NOW, reserve=reservation).snapshot() is None


def test_word_occurrence_duplicates_remain_ambiguous():
    from reconcile_replay import word_ownership
    word = {'word': 'amor', 'start': 1., 'end': 2.}
    attempt = {'input': {'wx_segs': [{'words': [word, word]}]}, 'output': [{'text': 'amor', 'words': [word]}]}
    link = word_ownership(attempt)[0]['words'][0]
    assert link['input_indices'] == [0, 1]
    assert link['association'] == 'ambiguous-or-absent'


def test_first_job_window_and_ambiguous_configuration(monkeypatch, tmp_path):
    enable(monkeypatch)
    monkeypatch.delenv('RECONCILE_CAPTURE_UNTIL')
    monkeypatch.setenv('RECONCILE_CAPTURE_WINDOW_SECONDS', '604800')
    audio = tmp_path / 'identity'; audio.write_bytes(b'fixture')
    start = int(NOW.timestamp())
    trace = rc.begin('first', str(audio), route_context={}, now=NOW,
                     reserve=lambda *args: [1, start, start + 604800])
    assert trace.snapshot()['window']['seconds'] == 604800
    assert trace.snapshot()['window']['started_at_epoch'] == start
    assert rc.begin('repeat', str(audio), route_context={}, now=NOW,
                    reserve=lambda *args: [0, start, start + 604800]).snapshot() is None
    monkeypatch.setenv('RECONCILE_CAPTURE_UNTIL', (NOW + timedelta(days=1)).isoformat())
    def forbidden(*args):
        raise AssertionError('ambiguous settings must not reserve')
    assert rc.begin('j', str(audio), route_context={}, now=NOW, reserve=forbidden).snapshot() is None
