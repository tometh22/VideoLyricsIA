import asyncio
import copy
import hashlib

import pytest
from fastapi import HTTPException

import batch_campaigns as batch
from transcription_worker import _maybe_apply_catalog_reference


TEXT = "primera frase de prueba\nsegunda estrofa del ejemplo\nultima linea del canto"


def reference():
    return dict(status="candidate", source_kind="google_sheet",
                source_url="https://docs.google.com/spreadsheets/d/example/edit",
                source_document_id="example", sheet_name="Lyrics", row_numbers=[5],
                artist="Artist", track="Track", text=TEXT,
                text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
                association_basis="exact_normalized", source_asset_id="asset",
                source_audio_sha256="a" * 64)


def item(ref):
    return batch.ManifestItem(client_id="asset", filename="track.wav", title="Track",
                              artist="Artist", technical_code="ASSET", size_bytes=100,
                              sha256="a" * 64, source_reference=ref)


@pytest.mark.parametrize("field,value", [("text", "other text"), ("source_asset_id", "other"),
                                         ("source_audio_sha256", "b" * 64),
                                         ("source_url", "https://other.example/sheet")])
def test_invalid_source_binding_rejected(field, value):
    ref = reference(); ref[field] = value
    with pytest.raises(HTTPException):
        batch._validated_source_reference(item(ref))


def test_source_contract_roundtrip_and_transport():
    from types import SimpleNamespace
    ref = batch._validated_source_reference(item(reference()))
    row = SimpleNamespace(title="Track", artist="Artist", filename="track.wav",
                          render_overrides={"source_reference": ref, "font": "test"})
    campaign = SimpleNamespace(tenant_id="test")
    for stage in ("separation", "full"):
        kwargs = batch._batch_transcription_kwargs(campaign, row, pipeline_stage=stage)
        assert kwargs["catalog_reference"] == ref
        assert kwargs["language"] == "" and kwargs["anchor_lyrics"] == ""
    assert batch._public_render_overrides(row) == {"font": "test"}
    assert "text" not in batch._source_reference_payload(row, include_text=False)
    assert batch._source_reference_payload(row, include_text=True)["text"] == TEXT


def test_manifest_api_persists_reference_and_rejects_silent_changes(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    auth = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/batch/campaigns", headers=auth, json={
        "name": "Chile fixture", "kind": "lyric_video", "destination_portal": "chile",
    })
    campaign_id = created.json()["id"]
    assert client.patch(f"/batch/campaigns/{campaign_id}", headers=auth,
                        json={"status": "paused"}).status_code == 200
    pairing = client.post(f"/batch/campaigns/{campaign_id}/upload-session", headers=auth).json()
    token = client.post("/batch/upload-sessions/exchange", json={
        "campaign_id": campaign_id, "code": pairing["pairing_code"],
    }).json()["upload_token"]
    upload_auth = {"X-Batch-Upload-Token": token}
    payload = {"items": [item(reference()).model_dump()]}
    url = f"/batch/campaigns/{campaign_id}/manifest"
    first = client.post(url, headers=upload_auth, json=payload)
    assert first.status_code == 200, first.text
    again = client.post(url, headers=upload_auth, json=payload)
    assert again.status_code == 200 and again.json()["registered_count"] == 1
    item_id = first.json()["items"][0]["item_id"]
    assert again.json()["items"][0]["item_id"] == item_id
    listing = client.get(f"/batch/campaigns/{campaign_id}/items", headers=auth).json()
    stored = listing["items"][0]
    assert stored["source_reference"]["text_sha256"] == reference()["text_sha256"]
    assert "text" not in stored["source_reference"]
    assert "source_reference" not in stored["render_overrides"]
    assert stored["job_id"] is None
    changed = copy.deepcopy(payload)
    changed["items"][0]["source_reference"]["row_numbers"] = [6]
    assert client.post(url, headers=upload_auth, json=changed).status_code == 409
    patched = client.patch(f"/batch/campaigns/{campaign_id}/items/{item_id}", headers=auth,
                           json={"render_overrides": {"source_reference": {"text": "replace"}}})
    assert patched.status_code == 422


def result():
    return {"segments": [{"text": t, "start": i * 2, "end": i * 2 + 2}
                         for i, t in enumerate(TEXT.splitlines())],
            "reference_lyrics": "independent acoustic hypothesis"}


def run(ref, monkeypatch, aligner, *, live=False):
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    return asyncio.run(_maybe_apply_catalog_reference(result(), "/tmp/audio.wav", "job", ref,
                                                     "a" * 64, live=live, aligner=aligner))


async def never(*args, **kwargs):
    raise AssertionError("alignment must not be called")


@pytest.mark.parametrize("case", ["absent", "wrong_audio", "wrong_text", "unrelated", "live"])
def test_nonvalidated_catalogue_keeps_audio_output(case, monkeypatch):
    ref = reference()
    if case == "absent": ref = {"status": "absent"}
    elif case == "wrong_audio": ref["source_audio_sha256"] = "b" * 64
    elif case == "wrong_text": ref["text"] = "changed"
    elif case == "unrelated":
        ref["text"] = "foreign words completely unrelated lyrics"
        ref["text_sha256"] = hashlib.sha256(ref["text"].encode()).hexdigest()
    out = run(ref, monkeypatch, never, live=case == "live")
    assert out["segments"] == result()["segments"]
    assert not out["catalog_reference"]["used"]


def test_catalogue_application_keeps_independent_reference(monkeypatch):
    async def aligner(base, path, job_id, text, **kwargs):
        assert text == TEXT and kwargs["content_source"] == "catalog_reference"
        base["segments"][0]["text"] = "aligned catalogue"
        base["reference_lyrics"] = text
        base["anchor_alignment"] = {"status": "applied", "timing_source": "ctc"}
        return base
    out = run(reference(), monkeypatch, aligner)
    assert out["catalog_reference"]["used"]
    assert out["reference_lyrics"] == result()["reference_lyrics"]


@pytest.mark.parametrize("raises", [False, True])
def test_aligner_failure_preserves_original_even_after_mutation(monkeypatch, raises):
    async def aligner(base, *args, **kwargs):
        base["segments"].clear()
        if raises: raise RuntimeError("fixture failure")
        base["anchor_alignment"] = {"status": "declined"}
        return base
    baseline = copy.deepcopy(result())
    out = run(reference(), monkeypatch, aligner)
    assert out["segments"] == baseline["segments"]
    assert not out["catalog_reference"]["used"]


def test_repeated_chorus_cannot_hide_an_unheard_verse(monkeypatch):
    ref = reference()
    ref["text"] = (TEXT + "\n" + "invento cuatro palabras extranjeras" + "\n" + (TEXT + "\n") * 5).strip()
    ref["text_sha256"] = hashlib.sha256(ref["text"].encode()).hexdigest()
    monkeypatch.setattr("pipeline._audio_duration", lambda _: 7)
    baseline = result()
    baseline["segments"] *= 6
    out = asyncio.run(_maybe_apply_catalog_reference(baseline, "/tmp/audio.wav", "job", ref,
                                                     "a" * 64, live=False, aligner=never))
    assert out["catalog_reference"]["used"] is False
    assert "reference_contains_unmatched_passage" in out["catalog_reference"]["attestation"]["reasons"]
