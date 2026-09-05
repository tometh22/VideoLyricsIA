from copy import deepcopy
from pathlib import Path
from html.parser import HTMLParser

import pytest

from reviewer_assist import enabled, operational_counts, prepare, publish
from reviewer_phrase_alignment import compare_occurrence_anchors, extend_context, phrase_occurrences
from reviewer_shadow import plan_windows, review_window
from shadow_reference_import import digest


def fixture():
    segments = [{"text": "Canto así", "start": 2., "end": 4.}]
    song = {"job_id": "test", "audio_sha256": "a" * 64, "audio_revision": 1,
        "segments_revision": 2, "segments": segments, "segments_sha256": digest(segments),
        "duration_seconds": 12.}
    evidence = [{"kind": "content", "family": family, "text": "Canto aquí",
        "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
        "occurrence_verified": True} for family in ("gemini", "whisper-1")]
    return song, review_window(song, plan_windows(song)[0], evidence=evidence, commit="a" * 40)


def test_default_off_never_imports_db_or_publishes(monkeypatch):
    monkeypatch.delenv("REVIEWER_ASSIST_ENABLED", raising=False)
    assert not enabled()
    assert publish(None, {}, []) == {"published": False, "reason": "reviewer_assist_disabled"}


def test_bridge_preserves_input_and_uses_existing_proposal_schema():
    song, decision = fixture()
    before = deepcopy(song)
    result = prepare(song, [decision])
    assert result["proposal"]["schema"] == "operator-review-proposal-v1"
    assert result["proposal"]["windows"][0]["proposed_segments"][0]["text"] == "Canto aquí"
    assert result["proposal"]["automatic_apply_allowed"] is False
    assert song == before


@pytest.mark.parametrize("field", ["audio_revision", "segments_revision"])
def test_stale_source_rejected(field):
    song, decision = fixture()
    song[field] += 1
    with pytest.raises(ValueError, match="stale"):
        prepare(song, [decision])


def test_protected_text_cannot_be_suggested():
    song, decision = fixture()
    song["segments"][0]["locked"] = True
    song["segments_sha256"] = digest(song["segments"])
    decision = review_window(song, plan_windows(song)[0], evidence=decision["evidence"], commit="a" * 40)
    assert prepare(song, [decision])["proposal"] is None


def test_unexamined_is_not_rejected_and_receipts_deduplicate():
    event = {"event_id": "1", "proposal_id": "a", "kind": "shown"}
    result = operational_counts(["a", "b"], [event, event])
    assert (result["shown"], result["rejected"], result["unexamined"]) == (1, 0, 2)
    assert result["objective_precision"] is None


def test_extension_requires_evidence_and_has_total_budget():
    w = {"start": 3., "end": 8.}
    assert extend_context(w, duration=100)["end"] == 8.
    expanded = extend_context(w, duration=100, truncated=True)
    assert expanded["end"] == 16.
    assert extend_context(expanded, duration=100, truncated=True,
        extension_used=expanded["extension_used"])["end"] == 16.
    assert w["end"] == 8.


def test_repeats_are_not_silently_assigned_to_first_occurrence():
    assert len(phrase_occurrences("otra vez", "otra vez y otra vez")) == 2
    assert phrase_occurrences("él", "el") == []


def test_wider_context_cannot_silently_jump_to_another_chorus():
    before = {"words": [{"word": "otra", "global_start": 10.},
                        {"word": "vez", "global_start": 11.}]}
    after = {"words": [{"word": "otra", "global_start": 20.},
                       {"word": "vez", "global_start": 21.}]}
    assert compare_occurrence_anchors(before, after)["status"] == "occurrence_drift"
    assert compare_occurrence_anchors(before, before)["same_occurrence_supported"] is True
    assert compare_occurrence_anchors({"words": []}, after)["status"] == "unverifiable"


def test_preview_has_six_real_options_not_malformed_closing_tags():
    class Options(HTMLParser):
        def __init__(self):
            super().__init__()
            self.verdict = False
            self.values = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.verdict = attrs.get("id") == "verdict"
            if tag == "option" and self.verdict:
                self.values.append(attrs.get("value"))
        def handle_endtag(self, tag):
            if tag == "select":
                self.verdict = False
    parser = Options()
    parser.feed((Path(__file__).parents[1] / "scripts/reviewer_shadow_preview.html").read_text())
    assert parser.values == ["unreviewed", "current_acceptable", "current_early",
                             "current_late", "ambiguous", "tool_failure"]


def test_operator_exposure_payload_is_not_silently_rejected():
    from product_telemetry import valid_property
    payload = {"proposal_id": "a" * 64, "total": 2, "timing_count": 0,
               "text_count": 2, "vocalization_count": 0}
    assert all(valid_property(k, v) for k, v in payload.items())
    assert not valid_property("proposal_id", "private lyric with spaces")
    assert not valid_property("total", float("nan"))
