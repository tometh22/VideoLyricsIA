import copy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import campaign_creative as creative
from database import AuditLog, Job
from tests.test_campaign_creative import setup  # noqa: F401
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


def request(items, **kwargs):
    return creative.PromptRequest(revision=0, common="Rock urbano, luz atravesando sombras.",
        songs=[creative.SongPrompt(item_id=item.id, scene=f"Escena propia {item.ordinal}") for item in items], **kwargs)


def test_prompts_keep_their_song_and_preserve_design_sources_pins_and_job_status(db, setup):
    campaign, items, actor = setup
    selected = items[:11]
    original = {}
    for n, item in enumerate(selected):
        item.render_overrides = {**item.render_overrides, 'font': 'poppins-bold', 'font_scale': 1.4,
            'movement_style': 'foto-parallax', 'effect': 'bokeh', 'lyrics_animation': 'pop',
            creative.ASSIGNMENT: {'group_id': 'photos', 'group_name': 'Fotos', 'requirement': 'photo_effect', 'model': '', 'pinned': n == 4, 'revision': 0}}
        original[item.id] = copy.deepcopy(item.render_overrides)
        db.add(Job(job_id=f'prompt-{n:08d}', campaign_id=campaign.id, campaign_item_id=item.id,
            tenant_id=campaign.tenant_id, user_id=actor['id'], artist='Rescate', filename=item.filename,
            status='lyrics_approved', workload_class='batch'))
    db.commit()
    result = creative.preview_prompts(campaign.id, request(list(reversed(selected))), actor, db)
    assert len(result['changes']) == 10 and result['skipped'] == [selected[4].id]
    for item in selected:
        db.refresh(item)
        assert item.render_overrides == original[item.id]  # preview never applies
    expected = {item.id: f'Rock urbano, luz atravesando sombras.\n\nEscena propia {item.ordinal}' for item in selected}
    assert {c['item_id']: c['after']['background_hint'] for c in result['changes']} == {key: value for key, value in expected.items() if key != selected[4].id}
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    for item in selected:
        db.refresh(item)
        if item.id == selected[4].id:
            assert item.render_overrides == original[item.id]
            continue
        assert item.render_overrides['background_hint'] == expected[item.id]
        assert item.render_overrides['match_lyrics'] is False
        assert item.render_overrides['bg_verbatim'] is False
        for key in ['font', 'font_scale', 'movement_style', 'effect', 'lyrics_animation', 'source_reference']:
            assert item.render_overrides[key] == original[item.id][key]
        assert item.render_overrides[creative.ASSIGNMENT]['requirement'] == 'photo_effect'
    assert db.query(Job).filter_by(campaign_id=campaign.id, status='lyrics_approved').count() == 11
    assert not db.query(AuditLog).filter_by(action='campaign.creative.generation').count()


def test_literal_prompts_replace_pinned_background_and_are_undoable(db, setup):
    campaign, items, actor = setup
    item = items[0]
    item.render_overrides = {'background_id': 999, 'background_hint': 'Anterior', creative.ASSIGNMENT: {
        'group_id': 'existing', 'group_name': 'Estilo existente', 'requirement': 'creative', 'pinned': True, 'revision': 0}}
    before = copy.deepcopy(item.render_overrides)
    db.commit()
    body = request([item], replace_exceptions=True, source='prompt_literal')
    result = creative.preview_prompts(campaign.id, body, actor, db)
    after = result['changes'][0]['after']
    assert after['bg_verbatim'] is True and after['background_id'] is None
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result['preview_id']), actor, db)
    db.refresh(item)
    assert item.render_overrides[creative.ASSIGNMENT]['pinned'] is True
    creative.undo(campaign.id, creative.UndoRequest(revision=1, operation_id=result['preview_id'], reason='Revertir prompts'), actor, db)
    db.refresh(item)
    assert item.render_overrides == before


def test_stale_prompt_preview_cannot_overwrite_a_later_design(db, setup):
    campaign, items, actor = setup
    first = creative.preview_prompts(campaign.id, request(items[:2]), actor, db)
    second = creative.preview_prompts(campaign.id, request(items[:2]), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=first['preview_id']), actor, db)
    with pytest.raises(HTTPException) as exc:
        creative.apply(campaign.id, creative.CommitRequest(preview_id=second['preview_id']), actor, db)
    assert exc.value.status_code == 409


@pytest.mark.parametrize('case', ['foreign', 'duplicate', 'empty', 'long', 'generated', 'pinned', 'revision', 'actor'])
def test_invalid_or_unsafe_prompt_selection_is_rejected(db, setup, case):
    campaign, items, actor = setup
    before_applies = db.query(AuditLog).filter_by(action='campaign.creative.applied').count()
    body = request(items[:1])
    if case == 'foreign': body.songs[0].item_id = 'foreign-id'
    if case == 'duplicate': body.songs.append(body.songs[0])
    if case == 'empty': body.common = ' '; body.songs[0].scene = '\n'
    if case == 'long': body.common = 'a' * 3000; body.songs[0].scene = 'b' * 2000
    if case == 'generated':
        db.add(Job(job_id='already-rendering', campaign_id=campaign.id, campaign_item_id=items[0].id,
            tenant_id=campaign.tenant_id, user_id=actor['id'], artist='Rescate', filename=items[0].filename, status='rendering'))
    if case == 'pinned': items[0].render_overrides = {creative.ASSIGNMENT: {'pinned': True}}
    if case == 'revision': body.revision = 1
    if case == 'actor': actor = {**actor, 'role': 'user', 'tenant_id': 'another-tenant'}
    db.commit()
    with pytest.raises(HTTPException):
        creative.preview_prompts(campaign.id, body, actor, db)
    assert db.query(AuditLog).filter_by(action='campaign.creative.applied').count() == before_applies


def test_prompt_request_is_bounded_and_accepts_only_prompt_sources():
    with pytest.raises(ValidationError):
        creative.PromptRequest(revision=0, source='lyrics', songs=[{'item_id': 'one'}])
    with pytest.raises(ValidationError):
        creative.PromptRequest(revision=0, songs=[{'item_id': str(i)} for i in range(513)])
