"""Freshness gate for the JS<->Python render-parity fixture.

frontend/src/shared/renderParity.json is GENERATED from ass_render.py by
scripts/gen_render_parity_fixture.py and asserted by the frontend suite
(lib/renderParity.test.js). This test closes the loop on the Python side:
if someone edits lyric_fontsize / _FONT_SIZE_NORM / _FADE_DURATIONS_S /
fade_seconds without regenerating, the committed fixture goes stale and
this test fails with the regeneration command — instead of the JS mirror
silently drifting from the render (the "preview shows text bigger than
the video" class of bug).
"""

import json
import os

from scripts.gen_render_parity_fixture import FIXTURE_PATH, build_fixture


def test_committed_fixture_matches_current_render_math():
    path = os.path.abspath(FIXTURE_PATH)
    assert os.path.exists(path), (
        f"missing {path} — run: python3 scripts/gen_render_parity_fixture.py"
    )
    with open(path) as f:
        committed = json.load(f)
    current = build_fixture()
    assert committed == current, (
        "renderParity.json is stale vs ass_render.py — regenerate with: "
        "cd lyricgen/backend && python3 scripts/gen_render_parity_fixture.py"
    )
