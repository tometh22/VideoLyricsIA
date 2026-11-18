"""Seeded projection properties; not a substitute for DB concurrency tests."""
import random

from change_request_workflow import case_state


def test_ten_thousand_state_observations_never_promote_missing_evidence():
    rng = random.Random(20260918)
    previous = None
    for _ in range(10000):
        p = dict(job_status=rng.choice(['queued', 'editing', 'done', 'pending_review', 'error', None]),
                 render_matches_editor=rng.choice([True, False, None]),
                 can_render=rng.choice([True, False]),
                 prores_pending=rng.choice([True, False]),
                 needs_publish=rng.choice([True, False, None]),
                 revision=rng.choice([None, 1, 8]))
        proposal = rng.choice([None, 'ready', 'partial', 'needs_input', 'stale', 'applied', 'partially_applied'])
        available = rng.choice([True, False])
        resolved = rng.choice([None, None, '2026-09-18'])
        source = rng.choice([None, 'manual', 'publication'])
        inputs = dict(publication=p, proposal_status=proposal, available=available,
                      resolved_at=resolved, resolution_source=source)
        state = case_state(**inputs)
        assert state == case_state(**inputs)  # No history/previous-card leakage.
        actions = state['allowed_actions']
        if resolved:
            assert actions == ['reopen']
            assert (state['activeStep'] == 5) == (source == 'publication')
        elif not available:
            assert actions == ['refresh'] and state['activeStep'] == 0
        if 'publish' in actions:
            assert available and not resolved and p['render_matches_editor'] is True
            assert p['needs_publish'] is True and not p['prores_pending']
            assert p['job_status'] not in ['queued', 'editing', 'error']
            assert proposal not in ['ready', 'partial', 'needs_input', 'stale']
        if state['activeStep'] == 5:
            assert resolved and source == 'publication'
        previous = state
    assert previous is not None


def test_partial_automatic_apply_preserves_manual_review_warning():
    state = case_state(publication={'job_status': 'done', 'render_matches_editor': True,
                                   'needs_publish': True, 'revision': 2},
                       proposal_status='partially_applied', pending_manual=2)
    assert state['pending_manual'] == 2
    assert 'sin comprobación automática' in state['detail']
    assert 'publish' in state['allowed_actions']  # Explicit final review remains possible.
