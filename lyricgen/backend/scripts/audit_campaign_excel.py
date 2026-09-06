"""Cache-only 300-ID Excel audit; creates separate artifacts, no remote writes."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_candidate import build_candidate
from reviewer_campaign import atomic_json
from reviewer_campaign_reconcile import reconcile, human_protected
from reviewer_excel_audit import audit, METHOD, deterministic_spelling
from reviewer_shadow import source_binding, review_window
from delivery_preflight import build_delivery_preflight
from shadow_reference_import import digest, import_workbook, associate


def run(root, snapshot_path, output, commit, align_new=False):
    started = time.monotonic()
    snapshot = json.loads(snapshot_path.read_text())
    workbook = root.parent/'attachments/0r7Dxt/Copia de Lista Genly Lyrics _ Art Tracks-4.xlsx'
    imported = associate(import_workbook(workbook), snapshot['jobs'])
    manifest = json.loads((root/'campaign-300/manifest.json').read_text())
    roster = {s['job_id']: s for s in manifest['songs']}
    assert len(snapshot['jobs']) == len(roster) == 300
    assert {s['job_id'] for s in snapshot['jobs']} == set(roster)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(output/'reference-import.json', imported)
    # Freeze before the first real song, including reserved evaluation cases.
    method_sha = hashlib.sha256(Path(__file__).parents[1].joinpath('reviewer_excel_audit.py').read_bytes()).hexdigest()
    atomic_json(output/'method.json', {'method': METHOD, 'sha256': method_sha,
        'snapshot_sha256': snapshot['snapshot_sha256'], 'reference_sha256': imported['workbook_sha256'],
        'calls_allowed': 0, 'threshold_tuning_from_holdout_allowed': False})
    index = request_index(root)
    results = []
    for song in snapshot['jobs']:
        jid = song['job_id']; folder = output/jid; folder.mkdir(mode=0o700, exist_ok=True)
        refs = [r for r in imported['rows'] if r.get('matched_job_id') == jid
                and r.get('association') == 'unique_metadata_candidate' and r.get('availability') == 'present']
        ref = refs[0] if len(refs) == 1 else None
        cached = cached_receipts(song, index=index)
        report = audit(song, ref, cached, commit=commit)
        old_folder = root/'campaign-300'/jid
        candidate = None
        orthographic_lines = []
        if song['segments']:
            old = json.loads((old_folder/'candidate.json').read_text()) if (old_folder/'candidate.json').exists() else None
            if old and old['source'] == source_binding(song):
                base = old
                review = json.loads((old_folder/'review.json').read_text())
            else:
                base, review = reconcile(song, cached, commit=commit, external_reference=ref)
            decisions = deepcopy(base.get('decision_evidence', []))
            occupied = {d['window']['line_index'] for d in decisions}
            adopted = []
            for decision in report['decisions']:
                if decision['window']['line_index'] in occupied:
                    continue
                adopted.append(decision)
                decisions.append(decision)
            # An approved song's isolated candidate is also preserved verbatim.
            if song.get('status') in {'lyrics_approved', 'done'} or song.get('approved_at'):
                decisions = []
                adopted = []
            else:
                for i, line in enumerate(song['segments']):
                    if human_protected(song, i):
                        continue
                    previous = next((d for d in decisions if d['window']['line_index'] == i), None)
                    text = previous['content']['text'] if previous and previous['content']['decision'] == 'propose' else line['text']
                    corrected, _, _ = deterministic_spelling(text)
                    if corrected == text:
                        continue
                    window = previous['window'] if previous else {'line_index': i,
                        'start': max(0., line['start']-1), 'end': min(song['duration_seconds'], line['end']+1),
                        'offset_seconds': max(0., line['start']-1)}
                    evidence = deepcopy(previous['evidence']) if previous else []
                    evidence.append({'kind': 'deterministic_orthography_request',
                        'source': source_binding(song), 'tool_status': 'ok',
                        'received_audio': False, 'calls': 0})
                    revised = review_window(song, window, evidence=evidence, commit=commit)
                    decisions = [d for d in decisions if d['window']['line_index'] != i] + [revised]
                    orthographic_lines.append(i)
            candidate = build_candidate(song, decisions, external_reference=ref)
            candidate['realignments'] = deepcopy(base.get('realignments', []))
            if align_new:
                from reviewer_phrase_alignment import align_phrase
                audio = Path(roster[jid]['audio_path'])
                if adopted and hashlib.sha256(audio.read_bytes()).hexdigest() != song['audio_sha256']:
                    raise ValueError('source_audio_sha256_mismatch')
                for decision in adopted:
                    i = decision['window']['line_index']
                    line = song['segments'][i]
                    start = max(0., line['start']-1)
                    end = min(song['duration_seconds'], start+24,
                        song['segments'][i+1]['start'] if i+1 < len(song['segments']) else song['duration_seconds'])
                    alignment = align_phrase(audio, decision['content']['text'],
                        {'start': start, 'end': end, 'offset_seconds': start})
                    candidate['realignments'].append({'line_index': i,
                        'display_timing_changed': False, **alignment})
            candidate['excel_audit'] = {'method': METHOD, 'method_sha256': method_sha,
                'report_sha256': report.get('audit_sha256'), 'new_changes': len(adopted)}
            review = deepcopy(review)
            review['localized_doubts'] = [
                {'line_index': d['line_indices'][0], 'kind': 'content',
                 'reason': d['status'], 'text': ' '.join(d.get('reference_tokens', [])),
                 'proposed_text': d.get('proposed_text'), 'start': d.get('start'), 'end': d.get('end')}
                for d in report['differences'] if len(d.get('line_indices', [])) == 1
                and d['status'] != 'audio_supported_candidate']
            review['excel_audit'] = candidate['excel_audit']
            atomic_json(folder/'candidate.json', candidate)
            atomic_json(folder/'review.json', review)
        else:
            adopted = []
        preflights = {}
        for phase, segments in [('before', song['segments']), ('after', candidate['segments'] if candidate else song['segments'])]:
            preflights[phase] = build_delivery_preflight(metadata={'artist': song['artist'], 'title': song['title']},
                segments=segments, reference_trusted=False, asset={'duration': song['duration_seconds']})
        preflights.update(stage='lyrics_and_timing_only', media_checks_deferred_to_stage_2=True,
            reference_not_trusted_as_spelling_authority=True)
        atomic_json(folder/'preflight.json', preflights)
        if candidate:
            candidate['residual_qc']['delivery_preflight'] = preflights['after']
            atomic_json(folder/'candidate.json', candidate)
        atomic_json(folder/'audit.json', report)
        results.append({'job_id': jid, 'artist': song['artist'], 'title': song['title'],
            'source': source_binding(song), 'status': report['status'],
            'snapshot_status': song['status'], 'reference': report.get('reference'),
            'reference_availability': next((r['availability'] for r in imported['rows']
                if r.get('matched_job_id') == jid), 'no_accepted_association'),
            'difference_counts': dict(Counter(d['status'] for d in report['differences'])),
            'candidate_available_offline': candidate is not None,
            'new_backed_changes': len(adopted), 'previous_acoustic_status': roster[jid]['status'],
            'orthography_corrected_lines': len(orthographic_lines),
            'preflight_before': preflights['before']['decision'], 'preflight_after': preflights['after']['decision'],
            'published': False, 'source_modified': False})
    counts = Counter()
    for r in results: counts.update(r['difference_counts'])
    summary = {'songs': 300, 'with_associated_reference': sum(bool(r['reference']) for r in results),
        'workbook_availability': imported['availability_counts'],
        'associated_availability': dict(Counter(r['reference_availability'] for r in results)),
        'without_associated_reference': sum(not r['reference'] for r in results),
        'new_repair_songs': sum(r['new_backed_changes'] > 0 for r in results),
        'new_backed_changes': sum(r['new_backed_changes'] for r in results),
        'orthography_corrected_lines': sum(r['orthography_corrected_lines'] for r in results),
        'orthography_corrected_songs': sum(r['orthography_corrected_lines'] > 0 for r in results),
        'preflight_before': dict(Counter(r['preflight_before'] for r in results)),
        'preflight_after': dict(Counter(r['preflight_after'] for r in results)),
        'difference_counts': dict(counts), 'new_api_calls': 0, 'new_api_spend_usd': 0,
        'source_edits': 0, 'timing_edits': 0, 'published': 0,
        'latency_seconds': round(time.monotonic()-started, 3)}
    atomic_json(output/'report.json', {'summary': summary, 'songs': results,
        'snapshot_sha256': snapshot['snapshot_sha256'], 'method_sha256': method_sha})
    print(json.dumps(summary))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--snapshot', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--commit', required=True)
    p.add_argument('--align-new', action='store_true')
    a = p.parse_args()
    run(a.root, a.snapshot, a.output, a.commit, a.align_new)
