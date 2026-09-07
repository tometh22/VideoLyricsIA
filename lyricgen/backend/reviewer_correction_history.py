"""Operational history, not blind gold or training authorization.

Only four-decimal persistence equivalence is ignored. A stable identity is
necessary, not sufficient, to claim the same occurrence. Structural transitions
and jointly translated intervals remain separate from endpoint corrections.
"""
from collections import Counter
from reviewer_edit_provenance import identity, verified_migration
from shadow_reference_import import digest


def summarize(versions):
    versions = sorted(versions, key=lambda v: v['revision'])
    events = []
    for before, after in zip(versions, versions[1:]):
        old, new = before['segments'], after['segments']
        base = {'from_revision': before['revision'], 'to_revision': after['revision'],
                'actor_user_id': after.get('created_by'), 'checkpoint': after.get('reason'),
                'before_sha256': digest(old), 'after_sha256': digest(new)}
        if after['revision'] != before['revision'] + 1:
            events.append({**base, 'category': 'history_gap'}); continue
        if verified_migration(old, after):
            events.append({**base, 'category': 'verified_automatic_transform',
                           'provenance': after.get('provenance')}); continue
        old_ids, new_ids = [identity(r) for r in old], [identity(r) for r in new]
        stable = (None not in old_ids + new_ids and len(set(old_ids)) == len(old_ids)
                  and len(set(new_ids)) == len(new_ids) and old_ids == new_ids)
        if not stable:
            if digest(old) != digest(new):
                events.append({**base, 'category': 'split_merge_or_unverified_association'})
            continue
        for i, (a, b) in enumerate(zip(old, new)):
            delta = {k: round(round(float(b[k]), 4)-round(float(a[k]), 4), 4) for k in ('start', 'end')}
            text_changed = a.get('text') != b.get('text')
            if not text_changed and not any(delta.values()): continue
            if after.get('reason') not in {'manual','autosave','draft','approve'}:
                category = 'unverified_transform'
            elif text_changed:
                category = 'text_and_timing' if any(delta.values()) else 'text'
            elif delta['start'] and delta['end']:
                category = 'translated_or_reassociated_interval'
            else:
                category = 'same_phrase_endpoint_candidate'
            events.append({**base, 'line_index': i, 'line_identity': list(old_ids[i]),
                'category': category, 'baseline': {k:a[k] for k in ('start','end')},
                'submitted': {k:b[k] for k in ('start','end')}, 'delta_seconds': delta,
                'baseline_text_sha256': digest(a.get('text')), 'text_sha256': digest(b.get('text')),
                'occurrence_verified': False, 'authorship': 'version_actor_not_per_edge_intent'})
    return {'schema':'operational-correction-history-v1', 'events':events,
            'counts':dict(Counter(e['category'] for e in events)),
            'persistence_precision_seconds':.0001, 'clean_gold':False,
            'blind_annotation':False, 'automatic_training_allowed':False}
