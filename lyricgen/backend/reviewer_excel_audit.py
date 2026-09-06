"""Excel-guided text repair using existing blind original-mix receipts only.

No I/O, inference, document writes or approval. Reference sequence alignment
generates hypotheses, never witnesses. A complete proposed caption must occur
uniquely in each of the two blind families at the current caption's occurrence.
Display times stay unchanged. Unsupported differences remain explicit doubts.
"""
from copy import deepcopy
from difflib import SequenceMatcher
import re
import unicodedata

from reviewer_campaign_reconcile import human_protected
from reviewer_shadow import (review_window, sequence_discrepancies, source_binding,
                             tokens, validate_snapshot)
from shadow_reference_import import digest

METHOD = "excel-guided-blind-occurrence-v3-local-reference-anchors"
TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")


def _hypothesis(song, difference):
    indices = difference['line_indices']
    if len(indices) != 1:
        return None, 'cross_caption_or_missing_caption'
    i = indices[0]
    current = song['segments'][i]['text']
    offset = sum(len(tokens(s['text'])) for s in song['segments'][:i])
    a, b = (n - offset for n in difference['baseline_token_range'])
    spans = list(TOKEN_RE.finditer(current))
    replacement = difference['reference_tokens']
    if difference['operation'] == 'delete':
        return None, 'deletion_requires_independent_absence_evidence'
    def no_accents(words):
        return ''.join(c for c in unicodedata.normalize('NFD', ' '.join(words))
                       if not unicodedata.combining(c))
    if no_accents(difference['current_tokens']) == no_accents(replacement):
        return None, 'diacritics_require_orthographic_not_acoustic_decision'
    # Cross-line insertions and large missing verses have ambiguous ownership.
    if not spans or not 0 <= a <= b <= len(spans):
        return None, 'token_ownership_unresolved'
    if a == b and a in {0, len(spans)}:
        return None, 'caption_boundary_insertion_requires_review'
    if max(b-a, len(replacement)) > 3:
        return None, 'large_difference_requires_review'
    if a == b:
        at = spans[a].start()
        proposed = current[:at] + ' '.join(replacement) + ' ' + current[at:]
    else:
        proposed = current[:spans[a].start()] + ' '.join(replacement) + current[spans[b-1].end():]
    # Do not introduce external punctuation/case or dangling apostrophes.
    proposed = re.sub(r' {2,}', ' ', proposed).strip()
    if re.search(r"\w['’](?=\s|$)", proposed) and not re.search(r"\w['’](?=\s|$)", current):
        return None, 'editorial_apostrophe_requires_review'
    if len(tokens(proposed)) < 3:
        return None, 'short_phrase_occurrence_ambiguous'
    return proposed, None


def _witness(song, i, proposed, record):
    request = record['request']
    if (request.get('tool_status') != 'ok' or request.get('received_audio') is not True
            or request.get('conditioning_texts') != [] or request.get('view') != 'mix'
            or (request.get('response') or {}).get('editorial_ambiguity')):
        return None
    if any((request.get('source') or {}).get(k) != song[k]
           for k in ('job_id', 'audio_sha256', 'audio_revision')):
        return None
    target = tokens(proposed)
    heard, owners = [], []
    annotations = record['annotations']
    for j, annotation in enumerate(annotations):
        part = tokens(annotation['text'])
        heard.extend(part)
        owners.extend([j] * len(part))
    matches = [j for j in range(len(heard)-len(target)+1)
               if heard[j:j+len(target)] == target]
    if len(matches) != 1:
        return None
    j = matches[0]
    first, last = annotations[owners[j]], annotations[owners[j+len(target)-1]]
    start, end = first['global_start'], last['global_end']
    line = song['segments'][i]
    # Provider event times are only occurrence hypotheses, not precise timing.
    # Reject any span overlapping another displayed occurrence, not just an
    # equal phrase elsewhere. No ±padding or clock transfer is introduced.
    overlaps = [n for n, s in enumerate(song['segments'])
                if max(start, s['start']) < min(end, s['end'])]
    if overlaps != [i] or not max(start, line['start']) < min(end, line['end']):
        return None
    w = request['window']
    if not w['start'] <= line['start'] < line['end'] <= w['end']:
        return None
    return {'kind': 'content', 'family': request['family'], 'tool_status': 'ok',
            'received_audio': True, 'conditioning_texts': [],
            'occurrence_verified': True, 'text': proposed,
            'occurrence_verification': 'unique_full_token_sequence_single_caption_overlap',
            'occurrence_is_not_precise_timing': True,
            'source': source_binding(song), 'original_source': request['source'],
            'evidence_sha256': record['evidence_sha256'], 'clip_sha256': request['clip_sha256'],
            'provider_window': deepcopy(w), 'matched_token_range': [j, j+len(target)],
            'matched_annotations': deepcopy(annotations[owners[j]:owners[j+len(target)-1]+1]),
            'global_start': start, 'global_end': end,
            'local_start': start-w['start'], 'local_end': end-w['start'],
            'text_rendering': 'baseline_punctuation_with_reference_lexical_hypothesis',
            'heard_tokens': heard[j:j+len(target)]}


def _local_reference_hypotheses(song, reference):
    """Recover local correspondence when whole-song repeats split the global diff.

    Similarity generates candidates only. Keep two unchanged adjacent words,
    at most three changed tokens, no deletion/diacritic inference. The exact
    complete phrase must still pass both independently heard occurrence gates.
    """
    refs = [tokens(line) for line in reference.splitlines() if tokens(line)]
    phrases = {tuple(sum(refs[i:i+n], [])) for i in range(len(refs)) for n in (1, 2)
               if len(sum(refs[i:i+n], [])) <= 24}
    result = []
    offset = 0
    for i, line in enumerate(song['segments']):
        current = tokens(line['text'])
        seen = set()
        for phrase in sorted(phrases):
            if abs(len(phrase)-len(current)) > 3 or set(current).isdisjoint(phrase):
                continue
            operations = SequenceMatcher(None, current, phrase, autojunk=False).get_opcodes()
            changed = [o for o in operations if o[0] != 'equal']
            if len(changed) != 1 or not any(o[0] == 'equal' and o[2]-o[1] >= 2 for o in operations):
                continue
            tag, a, b, c, d = changed[0]
            diff = {'operation': tag, 'baseline_token_range': [offset+a, offset+b],
                    'reference_token_range': [c, d], 'line_indices': [i],
                    'current_tokens': current[a:b], 'reference_tokens': list(phrase[c:d]),
                    'classification': 'local_reference_anchor_hypothesis',
                    'reference_phrase_tokens': list(phrase),
                    'reference_range_scope': 'local_reference_phrase_not_whole_song'}
            proposed, reason = _hypothesis(song, diff)
            if not reason and tuple(tokens(proposed)) == phrase and proposed not in seen:
                result.append(diff); seen.add(proposed)
        offset += len(current)
    return result


def audit(song, reference, cached, *, commit):
    validate_snapshot(song)
    result = {'schema': METHOD, 'source': source_binding(song), 'differences': [],
              'decisions': [], 'new_api_calls': 0, 'timing_changes': 0,
              'source_modified': False, 'reference_used_as_audio_witness': False}
    if not reference or reference.get('availability') != 'present' or reference.get('association') != 'unique_metadata_candidate' or reference.get('matched_job_id') != song['job_id']:
        result['status'] = 'no_accepted_text_reference'
        return result
    result['reference'] = {k: reference.get(k) for k in
        ('workbook_sha256', 'sheet', 'row', 'lyrics_cell', 'content_sha256')}
    diffs = sequence_discrepancies(song['segments'], reference['lyrics'])
    existing = {(tuple(d.get('baseline_token_range', [])), tuple(d.get('reference_tokens', []))) for d in diffs}
    diffs += [d for d in _local_reference_hypotheses(song, reference['lyrics'])
              if (tuple(d['baseline_token_range']), tuple(d['reference_tokens'])) not in existing]
    line_decisions = {}
    for diff in diffs:
        trace = deepcopy(diff)
        trace['audio_error_confirmed'] = False
        if diff['operation'] == 'format':
            trace['status'] = 'format_only_preserved'
        else:
            proposed, reason = _hypothesis(song, diff)
            trace.update(proposed_text=proposed)
            if reason:
                trace['status'] = reason
            else:
                i = diff['line_indices'][0]
                trace.update(start=song['segments'][i]['start'], end=song['segments'][i]['end'])
                witnesses = [w for r in cached['records'] if (w := _witness(song, i, proposed, r))]
                # Same-family overlapping windows count once, never independently.
                by_family = {}
                for witness in witnesses:
                    by_family.setdefault(witness['family'], witness)
                trace['witnesses'] = list(by_family.values())
                protected = human_protected(song, i) or bool(song.get('approved_at')) or song.get('status') in {'lyrics_approved', 'done'}
                if protected:
                    trace['status'] = 'human_protection_preserved'
                elif set(by_family) != {'openai/whisper-1', 'google/gemini-2.5-flash-audio'}:
                    trace['status'] = 'insufficient_independent_occurrence_evidence'
                else:
                    window = {**by_family['openai/whisper-1']['provider_window'], 'line_index': i}
                    decision = review_window(song, window, evidence=list(by_family.values()), commit=commit)
                    if decision['content']['decision'] == 'propose':
                        trace['status'] = 'audio_supported_candidate'
                        line_decisions.setdefault(i, []).append((trace, decision))
                    else:
                        trace['status'] = 'selector_rejected'
        result['differences'].append(trace)
    for group in line_decisions.values():
        # Don't overwrite one supported repair with another from the same baseline.
        if len(group) != 1:
            for trace, _ in group:
                trace['status'] = 'multiple_changes_same_line_require_reconciliation'
        else:
            result['decisions'].append(group[0][1])
    result['status'] = 'compared_not_certified'
    result['audit_sha256'] = digest(result)
    return result
