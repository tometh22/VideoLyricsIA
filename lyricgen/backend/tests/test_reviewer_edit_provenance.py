from copy import deepcopy
from reviewer_edit_provenance import derive, protected, verified_migration
from shadow_reference_import import digest


def fixture():
    rows = [{'id': 0, 'start': 1., 'end': 2., 'text': 'Hola.'},
            {'id': 1, 'start': 4., 'end': 5., 'text': 'Mundo'}]
    versions = [dict(revision=0, reason='transcription', segments=rows,
                     provenance={'schema': 'machine-transcription-lineage-v1'})]
    held = deepcopy(rows); held[0]['end'] = 2.25
    versions.append(dict(revision=1, reason='migration', segments=held,
        provenance=dict(kind='fixed_hold_backfill', from_hold_s=.25, to_hold_s=.5, changed_lines=1)))
    clean = deepcopy(held); clean[0]['text'] = 'Hola'
    versions.append(dict(revision=2, reason='migration', segments=clean,
        provenance=dict(kind='terminal_line_period_backfill_v1', changed_lines=1)))
    song = dict(job_id='fixture', segments=clean, original_segments=rows,
                audio_sha256='a'*64, audio_revision=1, segments_revision=2, segments_sha256=digest(clean))
    return song, versions


def test_only_exact_replayed_migrations_exempt_from_protection():
    song, versions = fixture()
    receipt = derive(song, versions)
    assert receipt['history_complete'] and len(receipt['verified_migrations']) == 2
    song['edit_provenance'] = receipt
    assert protected(song, 0) is False
    forged = deepcopy(versions[1]); forged['segments'][0]['end'] += .1
    assert not verified_migration(versions[0]['segments'], forged)
    forged = deepcopy(versions[1]); forged['reason'] = 'autosave'
    assert not verified_migration(versions[0]['segments'], forged)


def test_human_changes_approved_and_missing_history_fail_closed():
    song, versions = fixture()
    human = deepcopy(song['segments']); human[0]['text'] = 'Cambio humano'
    versions.append(dict(revision=3, reason='autosave', segments=human, provenance={}))
    song.update(segments=human, segments_revision=3, segments_sha256=digest(human))
    receipt = derive(song, versions)
    assert receipt['lines'][0]['protected'] and not receipt['lines'][1]['protected']
    assert receipt['last_prehuman_baseline']['revision'] == 2
    assert all(x['protected'] for x in derive({**song, 'status': 'lyrics_approved'}, versions)['lines'])
    assert all(x['protected'] for x in derive(song, versions[1:])['lines'])


def test_receipt_cannot_outlive_source_revision():
    song, versions = fixture(); song['edit_provenance'] = derive(song, versions)
    song['segments_revision'] += 1
    assert protected(song, 0) is None
