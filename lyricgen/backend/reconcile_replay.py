"""Network-free replay of a captured reconciliation and presentation chain.

Run against the captured release, in an environment with its evidence signing
configuration. Signing keys are never included in the capture. Exact baseline
mismatch blocks comparison; a recorded opaque transition is not a repair.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path


def difference(a, b):
    """Report every row, without inferring associations across changed layouts."""
    rows = []
    if len(a) != len(b) or [s.get('text') for s in a] != [s.get('text') for s in b]:
        return {'association': 'unresolved-layout-or-text-change', 'baseline': a, 'candidate': b}
    for i, (old, new) in enumerate(zip(a, b)):
        changed = {key: {'baseline': old.get(key), 'candidate': new.get(key)}
                   for key in sorted(set(old) | set(new)) if key not in old or key not in new or old.get(key) != new.get(key)}
        if changed:
            rows.append({'index': i, 'text': old.get('text'), 'changes': changed})
    return {'association': 'same-emitted-row-order; acoustic occurrence still requires review', 'rows': rows}



def word_ownership(attempt):
    source = [w for s in attempt['input']['wx_segs'] for w in s.get('words', [])]
    def identity(w):
        return (str(w.get('word', '')).strip(), w.get('start'), w.get('end'))
    index = {}
    for i, word in enumerate(source):
        index.setdefault(identity(word), []).append(i)
    return [
        {'line_index': i, 'text': seg.get('text'), 'words': [
            {'word': w, 'input_indices': index.get(identity(w), []),
             'association': 'unique' if len(index.get(identity(w), [])) == 1 else 'ambiguous-or-absent'}
            for w in seg.get('words', [])]}
        for i, seg in enumerate(attempt['output'] or [])
    ]


def verify_code(trace, backend):
    for name, expected in trace['code_sha256'].items():
        if hashlib.sha256((Path(backend) / name).read_bytes()).hexdigest() != expected:
            raise ValueError('captured_code_mismatch:' + name)


def replay(trace, reconcile):
    from post_reconcile import post_reconcile_cleanup
    from line_evidence import annotate_provider_evidence
    from transcribe_postprocess import dedup_collisions, normalize_words, strip_terminal_line_periods
    from whisperx_transcribe import _split_long_segments
    from beat_snap import snap_segments
    from chorus_trim import mark_repetitions
    from lead_in import polish
    from segment_timing import normalize_segments_timing

    if trace.get('incomplete'):
        raise ValueError('incomplete_capture')
    stages = trace['stages']
    by = {r['stage']: r['value'] for r in stages}
    attempts = []
    for i, row in enumerate(stages):
        if row['stage'] != 'reconcile_input':
            continue
        if i + 1 >= len(stages) or stages[i + 1]['stage'] != 'reconcile_output':
            raise ValueError('reconcile_did_not_complete')
        inp = row['value']
        actual = reconcile(deepcopy(inp['wx_segs']), inp['reference_text'], **inp['kwargs'])
        attempts.append({'input': inp, 'output': actual, 'captured': stages[i + 1]['value']})
    emit = by['emit_input']
    segs = deepcopy(emit['segments'])
    if attempts and attempts[-1]['captured']:
        last = attempts[-1]
        if 'gap_cluster_output' in by and by['gap_cluster_output'] != last['captured']:
            raise ValueError('gap_cluster_changed_output; cannot replay provider-dependent transition')
        old = last['captured']
        new = last['output']
        if not new:
            raise ValueError('candidate_changed_route')
        if 'cleanup_input' in by:
            if by['cleanup_input']['segments'] != old:
                raise ValueError('cleanup_input_not_reconciled_output')
            old = post_reconcile_cleanup(deepcopy(old), **by['cleanup_input']['kwargs'])
            if old != by['cleanup_output']:
                raise ValueError('baseline_cleanup_mismatch')
            new = post_reconcile_cleanup(deepcopy(new), **by['cleanup_input']['kwargs'])
        if old != segs:
            raise ValueError('unreplayed_transition_before_emit')
        segs = new
    elif attempts and attempts[-1]['output'] != attempts[-1]['captured']:
        raise ValueError('candidate_changed_fallback_route')
    reference_used = bool(emit['reference_lyrics'].strip()) if emit['content_reference_used'] is None else emit['content_reference_used']
    segs = [{k: v for k, v in s.items() if k != '_recognition_family'} for s in segs]
    annotated = annotate_provider_evidence(
        segs, source=emit['source'], timing_source=emit['source'],
        content_source='catalog_reference' if reference_used else emit['source'],
        reference_text=emit['reference_lyrics'] if reference_used else None,
        reference_id=emit['reference_id'] if reference_used else None,
    )
    result = _split_long_segments(normalize_words(dedup_collisions(annotated)))
    beats = by['beat_context']
    if beats['status'] == 'detected':
        result = snap_segments(result, beats['beats'], window_ms=beats['window_ms'])
    elif beats['status'] not in ('skipped', 'unavailable'):
        raise ValueError('unknown_beat_context')
    result = normalize_segments_timing(strip_terminal_line_periods(polish(mark_repetitions(result))))
    return {'attempts': attempts, 'annotated': annotated, 'presentation': result}


def compare(trace, baseline_fn, candidate_fn):
    """Baseline first; never run the candidate against an unverified baseline."""
    baseline = replay(trace, baseline_fn)
    recorded = {r['stage']: r['value'] for r in trace['stages']}
    checks = {
        'reconcile_exact': all(a['output'] == a['captured'] for a in baseline['attempts']),
        'annotation_exact': baseline['annotated'] == recorded['annotated_output'],
        'presentation_exact': baseline['presentation'] == recorded['presentation_output'],
    }
    if not all(checks.values()):
        return {'status': 'baseline_mismatch', 'checks': checks, 'baseline': baseline, 'candidate_executed': False}
    candidate = replay(trace, candidate_fn)
    final = recorded.get('final_machine_document')
    return {
        'status': 'stage_comparison_complete', 'checks': checks,
        'baseline': baseline, 'candidate': candidate,
        'presentation_difference': difference(baseline['presentation'], candidate['presentation']),
        'word_ownership_links': [{'baseline': word_ownership(a), 'candidate': word_ownership(b)} for a, b in zip(baseline['attempts'], candidate['attempts'])],
        'word_ownership_differences': [difference(a['output'] or [], b['output'] or []) for a, b in zip(baseline['attempts'], candidate['attempts'])],
        'post_presentation_document': final,
        'post_presentation_changed': final != baseline['presentation'],
        'post_stages': [r for r in trace['stages'] if r['stage'].startswith('post:')],
        'quality_verdict': 'pending-full-downstream-replay-and-human-review; no automatic release',
    }
