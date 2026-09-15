"""Post-freeze development accounting; never chooses a rule from human targets."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median

from reviewer_shadow_audio import file_sha, private_write


def report(directory):
    pred = json.loads((directory / 'predictions-frozen.json').read_text())
    evaluation = json.loads((directory / 'evaluation.json').read_text())
    listening = json.loads((directory / 'listening.json').read_text())
    if evaluation['freeze']['prediction_sha256'] != file_sha(directory / 'predictions-frozen.json'):
        raise ValueError('freeze_mismatch')
    methods = {}
    for cohort, rows in [('operational_all', [r for r in evaluation['rows'] if r['operational_comparator_eligible']]),
                         ('historical_extensions', [r for r in evaluation['rows'] if r['operational_comparator_eligible'] and r['historical_delta'] > 0])]:
        methods[cohort] = {}
        for method in ['ctc', 'spectral']:
            values = [r['methods'][method] for r in rows]
            errors = [v['absolute_error_seconds'] for v in values if v['end'] is not None]
            methods[cohort][method] = {'denominator': len(rows),
                'candidates': sum(v['end'] is not None for v in values),
                'closer_to_comparator': sum(v['improved'] for v in values),
                'farther_from_comparator': sum(v['worsened'] for v in values),
                'within_150ms': sum(v['within_150ms'] for v in values),
                'median_error_seconds_available_only': median(errors) if errors else None}
    requests = [q for w in listening['windows'] for q in w['requests']]
    result = {'schema': 'integral-development-report-v1',
        'freeze': evaluation['freeze'], 'implementation_commit': pred['implementation_commit'],
        'coverage_seconds': listening['coverage_seconds'],
        'lines': len(pred['lines']), 'lexical_occurrence_supported': sum(r['phrase_recognized'] for r in pred['lines']),
        'ctc_aligned_hypotheses': sum(r['ctc']['status'] == 'aligned_hypothesis' for r in pred['lines']),
        'spectral_stages': dict(Counter(r['alternative']['status'] for r in pred['lines'])),
        'selector_decisions': dict(Counter(r['selector']['decision'] for r in pred['lines'])),
        'text_candidates': sum(len(r['text_candidates']) for r in pred['lines']),
        'outside_event_hypotheses_including_overlaps': len(pred['outside_events']),
        'omissions_certified': 0, 'clean_gold_count': 0, 'development_comparison': methods,
        'calls_this_run': listening['calls_this_run'],
        'provider_failures': sum(q['tool_status'] != 'ok' for q in requests),
        'ctc_tool_failures': sum(r['ctc']['status'] == 'tool_error' for r in pred['lines']),
        'latency_seconds': {'listening': listening['latency_seconds'], 'analysis': pred['latency_seconds']},
        'usage': {'whisper_reported_seconds': sum((q.get('usage') or {}).get('seconds', 0) for q in requests if q['provider'] == 'openai'),
                  'gemini_input_tokens': sum((q.get('usage') or {}).get('prompt_token_count', 0) for q in requests if q['provider'] == 'google'),
                  'gemini_output_tokens': sum((q.get('usage') or {}).get('candidates_token_count', 0) for q in requests if q['provider'] == 'google')},
        'observed_cost_usd': None, 'cost_status': 'no_invoice_not_zero_cost',
        'decision': 'reject_spectral_v1_for_operational_repairs',
        'timing_repair_capability_demonstrated': False, 'original_documents_modified': False,
        'human_objective_precision_measured': False}
    private_write(directory / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--directory', type=Path, required=True)
    report(p.parse_args().directory)
