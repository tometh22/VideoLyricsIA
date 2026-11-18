from collections import Counter
import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import campaign_mix as mix
import campaign_creative as creative
from database import Job, AuditLog
from tests.test_campaign_creative import setup  # noqa: F401
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


def request(items=None, **kwargs):
    return mix.MixRequest(revision=0, item_ids=[i.id for i in items] if items else ['song'],
                          looks=['pincel', 'cosmico', 'cine'], effects=['bokeh', 'embers', 'rain'], **kwargs)


def totals(groups, key):
    result = Counter()
    for group in groups:
        result[getattr(group, 'requirement') if key == 'background' else group.settings[key]] += int(group.weight)
    return result


@pytest.mark.parametrize('count', [1, 2, 3, 25, 39, 50, 99, 1000])
@pytest.mark.parametrize('percent', [0, 17, 50, 73, 100])
def test_hierarchical_quotas_balance_every_axis_without_changing_background_split(count, percent):
    groups = mix.groups_for(request(photo_percent=percent), count)
    counts = totals(groups, 'background')
    assert counts['photo_effect'] == (count * percent + 50) // 100
    assert counts['veo'] + counts['photo_effect'] == count
    looks = totals(groups, 'lyric_look')
    assert max(looks.get(k, 0) for k in request().looks) - min(looks.get(k, 0) for k in request().looks) <= 1
    effects = totals([g for g in groups if g.requirement == 'photo_effect'], 'effect')
    assert max(effects.get(k, 0) for k in request().effects) - min(effects.get(k, 0) for k in request().effects) <= 1
    for kind in ('photo_effect', 'veo'):
        within = totals([g for g in groups if g.requirement == kind], 'lyric_look')
        assert max(within.get(k, 0) for k in request().looks) - min(within.get(k, 0) for k in request().looks) <= 1


def test_50_songs_yields_25_25_and_9_8_8_not_independent_random_draws():
    groups = mix.groups_for(request(), 50)
    assert totals(groups, 'background') == {'photo_effect': 25, 'veo': 25}
    assert sorted(totals([g for g in groups if g.requirement == 'photo_effect'], 'effect').values()) == [8, 8, 9]
    assert sorted(totals(groups, 'lyric_look').values()) == [16, 17, 17]
    assert [g.model_dump() for g in groups] == [g.model_dump() for g in mix.groups_for(request(), 50)]


@pytest.mark.parametrize('looks', [[''], ['', 'pincel']])
def test_classic_is_an_explicit_choice_and_balances_with_pack_looks(looks):
    body = mix.MixRequest(revision=0, item_ids=['song'], looks=looks, effects=['bokeh', 'embers'])
    groups = mix.groups_for(body, 8)
    assert totals(groups, 'background') == {'photo_effect': 4, 'veo': 4}
    assert totals(groups, 'lyric_look') == {look: 8 // len(looks) for look in looks}
    for group in groups:
        if group.settings['lyric_look'] == '':
            assert 'Clásico' in group.name
            assert group.settings['lyric_variant'] == ''
            assert group.settings['lyrics_animation'] == 'none'


@pytest.mark.parametrize('patch', [{'looks': ['pop70']}, {'looks': ['cinetico']}, {'looks': ['unknown']},
                                    {'looks': ['pincel', 'pincel']}, {'effects': ['foto_viva']}, {'effects': []}, {'effects': ['bokeh', 'bokeh']}])
def test_rejects_unsafe_or_invalid_choices(patch):
    body = request().model_copy(update=patch)
    with pytest.raises(HTTPException): mix.groups_for(body, 50)


def test_video_only_does_not_require_photo_effects_and_large_catalogue_fits():
    body = request().model_copy(update={'photo_percent': 0, 'effects': []})
    assert totals(mix.groups_for(body, 50), 'background') == {'veo': 50}
    full = request().model_copy(update={'looks': [l['id'] for l in mix.catalog()['looks'] if l['compatible']], 'effects': mix.catalog()['effects']})
    creative.PlanRequest(revision=0, item_ids=['song'], groups=mix.groups_for(full, 1000), mode='count', reason='Test complete catalogue')
    for look in mix.catalog()['looks']:
        if look['compatible']:
            assert not any(creative._lyric_recipes.may_own_background(look['id'], genre) for genre in ['', *creative._lyric_recipes.GENRES])


def test_preview_apply_scoping_pins_ready_ids_and_no_generation(db, setup):
    campaign, items, actor = setup
    for n, item in enumerate(items):
        db.add(Job(job_id=f'mix-{n:08d}', campaign_id=campaign.id, campaign_item_id=item.id, user_id=actor['id'],
                   tenant_id=campaign.tenant_id, workload_class="batch", artist="Artista", filename=item.filename, status='lyrics_approved' if n < 20 else 'transcribed_pending'))
    items[0].render_overrides = {**items[0].render_overrides, creative.ASSIGNMENT: {'pinned': True}}
    db.commit()
    generation_receipts = db.query(AuditLog).filter_by(action='campaign.creative.generation').count()
    result = mix.preview_mix(campaign.id, request(items), actor, db)
    assert result['mix']['total'] == 38 and result['mix']['photo_effect'] == result['mix']['veo'] == 19
    assert result['skipped'] == [items[0].id] and len(result['ready_item_ids']) == 19
    assert items[0].id not in result['ready_item_ids']
    assert 'source_reference' not in str(result)
    repeated = mix.preview_mix(campaign.id, request(list(reversed(items))), actor, db)
    assert result['changes'] == repeated['changes']
    reshuffled = mix.preview_mix(campaign.id, request(items, seed='another-seed'), actor, db)
    assert result['changes'] != reshuffled['changes']
    assert result['mix']['photo_effect'] == reshuffled['mix']['photo_effect']
    saved = creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    assert saved['revision'] == 1
    assert creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)['deduplicated']
    assert db.query(Job).filter_by(campaign_id=campaign.id, status='lyrics_approved').count() == 20
    assert db.query(AuditLog).filter_by(action='campaign.creative.generation').count() == generation_receipts
    assert items[0].render_overrides[creative.ASSIGNMENT] == {'pinned': True}
    for item in items[1:]:
        db.refresh(item)
        assert item.render_overrides['source_reference'] == {'status': 'absent'}
        assert item.render_overrides['lyric_variant'] == ''


def test_classic_preview_and_apply_clear_previous_pack_look(db, setup):
    campaign, items, actor = setup
    items[0].render_overrides = {'lyric_look': 'pincel', 'lyric_variant': '12'}
    db.commit()
    body = request(items).model_copy(update={'looks': ['']})
    result = mix.preview_mix(campaign.id, body, actor, db)
    assert result['mix']['looks'] == {'': len(items)}
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    for item in items:
        db.refresh(item)
        assert item.render_overrides['lyric_look'] == ''
        assert item.render_overrides['lyric_variant'] == ''


def test_stale_previews_and_rendered_songs_are_not_applied(db, setup):
    campaign, items, actor = setup
    result = mix.preview_mix(campaign.id, request(items), actor, db)
    items[0].render_overrides = {'font': 'anton'}; db.commit()
    with pytest.raises(HTTPException) as exc:
        creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    assert exc.value.status_code == 409
    db.add(Job(job_id='already-done', user_id=actor['id'], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
               campaign_item_id=items[0].id, workload_class="batch", artist='Artista', filename='song.wav', status='done')); db.commit()
    with pytest.raises(HTTPException) as exc:
        mix.preview_mix(campaign.id, request(items), actor, db)
    assert exc.value.status_code == 409


def test_foreign_ids_and_unauthorized_actor_cannot_assign(db, setup, monkeypatch):
    campaign, items, actor = setup
    monkeypatch.setenv('BATCH_CAMPAIGN_SCOPES', 'foreign')
    with pytest.raises(HTTPException) as exc:
        mix.preview_mix(campaign.id, request(items), {**actor, 'role': 'user', 'tenant_id': 'foreign'}, db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException): mix.preview_mix(campaign.id, request([SimpleNamespace(id='foreign')]), actor, db)
    with pytest.raises(HTTPException): mix.preview_mix(campaign.id, request([items[0], items[0]]), actor, db)
    with pytest.raises(ValidationError): request(photo_percent=50.5)


def test_explicit_replacement_keeps_existing_pin_and_never_rewrites_contract(db, setup):
    campaign, items, actor = setup
    original_contract = {'agreement': 'Contract unchanged', 'revision': 1}
    campaign.default_render_params = {creative.KEY: {'revision': 0, 'contract': original_contract}}
    items[0].render_overrides = {creative.ASSIGNMENT: {'pinned': True}, 'lyric_font_main': 'anton', 'lyric_variant': '12'}
    db.commit()
    result = mix.preview_mix(campaign.id, request(items, replace_exceptions=True), actor, db)
    assert not result['skipped'] and result['mix']['total'] == 39
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    db.refresh(items[0]); db.refresh(campaign)
    assert items[0].render_overrides[creative.ASSIGNMENT]['pinned'] is True
    assert items[0].render_overrides['lyric_font_main'] == ''
    assert items[0].render_overrides['lyric_variant'] == ''
    assert campaign.default_render_params[creative.KEY]['contract'] == original_contract
