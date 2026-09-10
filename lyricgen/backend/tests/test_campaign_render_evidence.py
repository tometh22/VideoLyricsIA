"""Actual encoded bytes and provider provenance must support campaign claims."""
import json
import shutil
import subprocess
import uuid

import numpy as np
import pytest
from PIL import Image

import campaign_render_evidence as evidence
from database import AIProvenance, AuditLog, Job
from tests.test_campaign_creative import setup  # noqa: F401
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


def make_job(db, setup, tmp_path):
    campaign, items, actor = setup
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id,
              campaign_id=campaign.id, campaign_item_id=items[0].id, workload_class="batch", artist="Test",
              filename="synthetic.wav", status="processing", render_params={})
    db.add(job); db.commit()
    folder = tmp_path / job.job_id; folder.mkdir()
    return job, folder


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_real_photo_effect_encode_and_immutable_receipt(db, setup, tmp_path, monkeypatch):
    import pipeline
    from tests.test_fx_e2e_render import _photo, _click_track, _spec
    job, folder = make_job(db, setup, tmp_path)
    photo, audio = folder / "photo.jpg", folder / "audio.mp3"
    _photo(photo); _click_track(audio)
    # Exercise the same uploaded-image conversion as the real renderer.
    from types import SimpleNamespace
    monkeypatch.setattr(pipeline, "AudioFileClip", lambda _: SimpleNamespace(duration=3., close=lambda: None))
    def no_kenburns(*args, **kwargs):
        pytest.fail("A fixed campaign photo must never enter the zoom renderer")
    monkeypatch.setattr(pipeline, "_prerender_kenburns_bg", no_kenburns)
    render = pipeline._render_lyrics_ass
    def without_text(*args, **kwargs):
        return render(*args, **kwargs, render_text=False)
    monkeypatch.setattr(pipeline, "_render_lyrics_ass", without_text)
    output, _, _ = pipeline.generate_lyric_video(str(audio), [], "auto", str(folder), "",
        bg_image_path=str(photo), spec=_spec(), font="", effect="chromatic_pulse", still_background=True)
    encoded = json.loads((folder / "lyric_video.mp4.creative.json").read_text())
    assert encoded["background_kind"] == "image"
    assert encoded["effect_applied"] == "chromatic_pulse"
    assert encoded["video_sha256"] == evidence.file_hash(output)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "0.6", "-i", output,
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
    rendered = np.frombuffer(raw, dtype=np.uint8).reshape(360, 640, 3)
    source = np.asarray(Image.open(photo).convert("RGB"), dtype=np.uint8)
    assert np.abs(rendered.astype(np.int16) - source.astype(np.int16)).mean() > .45
    evidence.persist_render_evidence(job.job_id, folder)
    db.refresh(job)
    receipt = job.render_params["campaign_render_evidence"]
    assert receipt["video_sha256"] == encoded["video_sha256"]
    assert receipt["background_kind"] == "image" and not receipt["models"]
    assert db.query(AuditLog).filter_by(action="campaign.creative.rendered").filter(
        AuditLog.detail["job_id"].as_string() == job.job_id).count() == 1


def test_provider_proof_is_bound_to_exact_background_and_job(db, setup, tmp_path):
    job, folder = make_job(db, setup, tmp_path)
    background, video = folder / "bg.mp4", folder / "lyric_video.mp4"
    background.write_bytes(b"provider-output"); video.write_bytes(b"encoded-output")
    call = AIProvenance(job_id=job.job_id, step="video_bg", tool_name="veo-test", tool_provider="google_vertex",
                        prompt_sent="synthetic scene", prompt_hash="a" * 64, response_summary="video_generated:ok")
    db.add(call); db.commit()
    evidence.write_origin(background, kind="video", model="veo-test", provenance_id=call.id)
    evidence.write_encode_receipt(video, background, "rain", False, "test")
    evidence.persist_render_evidence(job.job_id, folder)
    db.refresh(job)
    assert job.render_params["campaign_render_evidence"]["models"] == ["veo-test"]
    assert job.render_params["campaign_render_evidence"]["effect_applied"] == ""
    # A different background at the same path cannot borrow an older AI call.
    background.write_bytes(b"replacement-background")
    video.write_bytes(b"new-encoded-output")
    evidence.write_encode_receipt(video, background, "", False, "test")
    evidence.persist_render_evidence(job.job_id, folder)
    db.refresh(job)
    assert job.render_params["campaign_render_evidence"]["models"] == []
    # A stale encode receipt cannot certify replacement video bytes either.
    video.write_bytes(b"unattested-output")
    evidence.persist_render_evidence(job.job_id, folder)
    db.refresh(job)
    assert "background_sha256" not in job.render_params["campaign_render_evidence"]
