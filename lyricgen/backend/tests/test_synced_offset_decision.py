"""Unit tests for lrclib_aligner.synced_offset_decision — the offset+trust
decision for shipping an lrclib SYNCED timeline onto the user's audio in the
WC (audio-as-truth) synced-direct fallback. Pure, no network/audio."""

from lrclib_aligner import synced_offset_decision


def test_duration_match_uses_zero_offset_and_trusts():
    # Enanitos Verdes "Mi Primer Día Sin Ti": audio 268.1 s vs lrclib 269.0 s.
    # whisperX skipped the soft "Uoh-oh-oh" intro so its first word (@38.2 s)
    # is a LATER line — the old anchor produced +36 s and flagged everything.
    # Duration match must win: offset 0, trusted (no review spam).
    offset, trust = synced_offset_decision(268.1, 269.0, 38.2, 2.04)
    assert offset == 0.0
    assert trust is True


def test_duration_match_boundary_inclusive():
    # |audio - lrc| exactly at the tolerance still counts as a match.
    offset, trust = synced_offset_decision(266.0, 269.0, 38.2, 2.04)
    assert (offset, trust) == (0.0, True)


def test_duration_mismatch_falls_back_to_anchor_with_review():
    # User uploaded an "Official Video" cut with a ~8 s added intro: durations
    # differ, so anchor whisperX's first word to the first synced line and
    # keep the review flag (the offset is an estimate).
    offset, trust = synced_offset_decision(208.0, 200.0, 10.0, 2.0)
    assert offset == 8.0
    assert trust is False


def test_anchor_out_of_range_clamped_to_zero():
    # A wildly out-of-range anchor (> 60 s) is garbage → clamp to 0, untrusted.
    offset, trust = synced_offset_decision(200.0, 150.0, 100.0, 2.0)
    assert offset == 0.0
    assert trust is False


def test_no_duration_info_uses_anchor():
    # Duration probe failed for both sides → rely on the whisperX anchor.
    offset, trust = synced_offset_decision(None, None, 10.0, 2.0)
    assert offset == 8.0
    assert trust is False


def test_no_basis_returns_zero_untrusted():
    # No duration match and no whisperX anchor → no basis; caller should fall
    # through to whisperX raw rather than ship a guessed timeline.
    offset, trust = synced_offset_decision(None, None, None, 2.0)
    assert offset == 0.0
    assert trust is False


def test_duration_match_wins_even_without_anchor():
    # Even if whisperX produced no first word, matching durations let us trust
    # the synced timeline as-is.
    offset, trust = synced_offset_decision(269.5, 269.0, None, 2.04)
    assert (offset, trust) == (0.0, True)


# ---------------------------------------------------------------------------
# verify_fn — audit 2026-08-13 (F4): two agreeing durations are both external
# metadata (user upload, lrclib scrape) and never proved the synced timeline
# actually matches what's sung. These tests cover the new content-verification
# gate that regime 1 goes through before trust=True ships.
# ---------------------------------------------------------------------------

def test_duration_match_with_high_verify_score_still_trusts():
    # Durations agree AND the audio content at offset 0 really does match
    # lrclib's opening line — trust as before.
    offset, trust = synced_offset_decision(
        268.1, 269.0, 38.2, 2.04, verify_fn=lambda: 0.9,
    )
    assert (offset, trust) == (0.0, True)


def test_duration_match_with_low_verify_score_falls_through_to_anchor():
    # Durations coincidentally agree but the audio at offset 0 does NOT match
    # lrclib's text — e.g. a different edit/version with the same runtime.
    # Do not ship a trusted-but-wrong zero offset; fall through to the
    # whisperX anchor estimate (untrusted, review flag stays on).
    offset, trust = synced_offset_decision(
        268.1, 269.0, 10.0, 2.0, verify_fn=lambda: 0.05,
    )
    assert offset == 8.0
    assert trust is False


def test_duration_match_with_low_verify_score_and_no_anchor_returns_untrusted():
    # Same as above but whisperX gave no anchor either — no basis at all,
    # caller should fall through to whisperX raw.
    offset, trust = synced_offset_decision(
        268.1, 269.0, None, 2.0, verify_fn=lambda: 0.05,
    )
    assert offset == 0.0
    assert trust is False


def test_duration_match_with_none_verify_score_treated_as_unverifiable():
    # verify_fn returning None (e.g. Whisper/slice failed) is "couldn't check",
    # not "checked and it's fine" — must not be treated as a pass.
    offset, trust = synced_offset_decision(
        268.1, 269.0, 10.0, 2.0, verify_fn=lambda: None,
    )
    assert offset == 8.0
    assert trust is False


def test_verify_score_exactly_at_threshold_trusts():
    offset, trust = synced_offset_decision(
        268.1, 269.0, 38.2, 2.04, verify_fn=lambda: 0.4, verify_min_score=0.4,
    )
    assert (offset, trust) == (0.0, True)


def test_verify_fn_not_called_when_durations_disagree():
    # The whole point of gating on duration-agreement first is to only pay
    # for the (Whisper-backed) verify call when it's actually relevant.
    calls = []

    def _tracking_verify():
        calls.append(1)
        return 0.9

    offset, trust = synced_offset_decision(
        208.0, 200.0, 10.0, 2.0, verify_fn=_tracking_verify,
    )
    assert offset == 8.0
    assert trust is False
    assert calls == [], "verify_fn must not run when durations already disagree"


def test_verify_fn_none_preserves_old_trust_everyone_behaviour():
    # Backward compatibility: existing callers that never pass verify_fn
    # (or any caller before this audit) keep the original semantics —
    # duration agreement alone is enough.
    offset, trust = synced_offset_decision(268.1, 269.0, 38.2, 2.04)
    assert (offset, trust) == (0.0, True)
