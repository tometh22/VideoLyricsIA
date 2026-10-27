"""A repair may only touch untouched, audio-bound drafts."""

import hashlib
from types import SimpleNamespace

from scripts.repair_campaign_catalog_reference import _eligible


SHA = "a" * 64
SUNG = "primera frase de prueba\nsegunda estrofa del ejemplo\nultima linea del canto"


def fixture():
    raw = "Letra de Track\n" + SUNG
    reference = {
        "status": "candidate", "track": "Track", "text": raw,
        "text_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source_audio_sha256": SHA, "source_asset_id": SHA,
    }
    segments = [
        {"text": line, "start": i * 2, "end": i * 2 + 2}
        for i, line in enumerate(SUNG.splitlines())
    ]
    item = SimpleNamespace(render_overrides={"source_reference": reference},
                           sha256=SHA, title="Track", duration_seconds=7)
    job = SimpleNamespace(status="transcribed_pending", segments_revision=0,
                          transcription_quality={"catalog_reference": {"status": "rejected", "used": False}},
                          input_audio_sha256=SHA, segments_json=segments)
    document = SimpleNamespace(revision=0, updated_by=None, lock_user_id=None,
                               current_segments=segments)
    return item, job, document


def test_untouched_audio_bound_heading_is_repair_candidate():
    item, job, document = fixture()
    decision, report = _eligible(item, job, document)
    assert decision == "candidate"
    assert report["allow_global_forced_alignment"] is True


def test_human_revision_or_editor_divergence_blocks_repair():
    item, job, document = fixture()
    document.revision = 1
    assert _eligible(item, job, document)[0] == "editor_changed_or_locked"
    document.revision = 0
    document.current_segments = []
    assert _eligible(item, job, document)[0] == "editor_changed_or_locked"


def test_wrong_audio_or_changed_sheet_text_blocks_repair():
    item, job, document = fixture()
    item.sha256 = "b" * 64
    assert _eligible(item, job, document)[0] == "source_binding_invalid"
    item.sha256 = SHA
    item.render_overrides["source_reference"]["text"] += " altered"
    assert _eligible(item, job, document)[0] == "source_binding_invalid"
