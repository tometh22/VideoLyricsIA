"""Conservative per-line ownership from explicit, contiguous editor history.

Only the two audited migrations are replayed. Neither revision number nor
actor identity alone makes a change automatic. Unknown history fails closed.
Publication recomputes this receipt from the database, never from client claims.
"""
from copy import deepcopy
from shadow_reference_import import digest
from reviewer_shadow import source_binding

SCHEMA = 'reviewer-edit-provenance-v1'


def content(row):
    return (str(row.get('text') or ''), round(float(row['start']), 4), round(float(row['end']), 4))


def identity(row):
    for key in ('segment_id', 'id', '_id'):
        if row.get(key) is not None:
            return key, str(row[key])
    return None


def verified_migration(before, version):
    if version.get('reason') != 'migration': return False
    after = version.get('segments') or []
    provenance = version.get('provenance') or {}
    expected = deepcopy(before)
    kind = provenance.get('kind')
    if kind == 'fixed_hold_backfill' and provenance.get('from_hold_s') == .25 and provenance.get('to_hold_s') == .5:
        for i, row in enumerate(expected[:-1]):
            if row.get('locked') or row.get('operator_locked'): continue
            end = float(row['end'])
            target = max(end, min(end + .25, float(expected[i+1]['start']) - .01))
            if target > end + 1e-9: row['end'] = round(target, 3)
    elif kind == 'terminal_line_period_backfill_v1':
        from transcribe_postprocess import strip_terminal_line_periods
        expected = strip_terminal_line_periods(expected)
    else:
        return False
    changed = sum(content(a) != content(b) for a, b in zip(before, expected))
    return expected == after and provenance.get('changed_lines') == changed


def derive(song, versions):
    versions = sorted(versions, key=lambda v: v['revision'])
    current = song['segments']; source = source_binding(song)
    complete = bool(versions and versions[0]['revision'] == 0
        and versions[0].get('reason') == 'transcription'
        and (versions[0].get('provenance') or {}).get('schema') == 'machine-transcription-lineage-v1'
        and [v['revision'] for v in versions] == list(range(int(song['segments_revision']) + 1))
        and digest(versions[-1]['segments']) == source['segments_sha256'])
    migrations = []
    baseline = None
    if not complete:
        reasons = ['history_missing_or_unverified'] * len(current)
    else:
        previous = versions[0]['segments']
        reasons = ['explicit_lock' if r.get('locked') or r.get('operator_locked') else None for r in previous]
        baseline = {'revision':0,'segments_sha256':digest(previous)}
        machine_prefix = True
        for version in versions[1:]:
            rows = version['segments']
            if verified_migration(previous, version):
                migrations.append({'revision':version['revision'], 'kind':version['provenance']['kind'],
                    'before_sha256':digest(previous),'after_sha256':digest(rows)})
                if machine_prefix: baseline = {'revision':version['revision'],'segments_sha256':digest(rows)}
            else:
                machine_prefix = False
                ids = [identity(r) for r in previous]
                unique = None not in ids and len(set(ids)) == len(ids)
                indexed = {key:(previous[i],reasons[i]) for i,key in enumerate(ids)} if unique else {}
                next_reasons = []
                for i,row in enumerate(rows):
                    pair = indexed.get(identity(row))
                    if pair is None and not unique and len(rows) == len(previous):
                        pair = previous[i], reasons[i]
                    if pair is None:
                        reason = 'structural_or_unassociated_human_edit'
                    else:
                        old, reason = pair
                        if content(old) != content(row):
                            reason = 'human_edit' if version.get('reason') in {'manual','autosave','draft','approve'} else 'unverified_transformation'
                    if row.get('locked') or row.get('operator_locked'): reason = 'explicit_lock'
                    next_reasons.append(reason)
                reasons = next_reasons
            previous = rows
    if song.get('approved_at') or song.get('status') in {'lyrics_approved','done'}:
        reasons = ['approved_song'] * len(current)
    lines = [{'line_index':i,'protected':bool(reason),'reason':reason or 'verified_machine_or_migration'}
             for i,reason in enumerate(reasons)]
    return {'schema':SCHEMA,'source':source,'history_sha256':digest(versions),
        'history_complete':complete,'verified_migrations':migrations,'lines':lines,
        'last_prehuman_baseline':baseline,'human_gold':False}


def protected(song, index):
    receipt = song.get('edit_provenance')
    if (not isinstance(receipt,dict) or receipt.get('schema') != SCHEMA
            or receipt.get('source') != source_binding(song)
            or len(receipt.get('lines',[])) != len(song['segments'])):
        return None
    row = receipt['lines'][index]
    return row.get('protected') is not False if row.get('line_index') == index else True


def from_database(db, song):
    from database import EditorVersion
    versions = db.query(EditorVersion).filter(EditorVersion.job_id == song['job_id']).order_by(EditorVersion.revision).all()
    return derive(song, [{'revision':v.revision,'reason':v.reason,'segments':v.segments,
        'provenance':v.provenance,'created_by':v.created_by,'is_approved':v.is_approved} for v in versions])
