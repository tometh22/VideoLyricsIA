"""Evidence from successful encoded bytes, never from a requested preset."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_origin(path, *, kind, original=None, model=None, provenance_id=None):
    """Bind a completed transformation/provider output to its exact bytes."""
    target = Path(path)
    data = {"sha256": file_hash(target), "kind": kind,
            "original_sha256": file_hash(original) if original else None,
            "model": model, "provenance_id": provenance_id}
    target.with_suffix(target.suffix + ".origin.json").write_text(json.dumps(data))


def write_encode_receipt(video, background, effect, applied, filter_graph):
    """Only invoked after successful ffmpeg and playable-output validation."""
    from database import SessionLocal, Job
    path = Path(video)
    with SessionLocal() as db:
        if not db.query(Job.job_id).filter(Job.job_id == path.parent.name, Job.campaign_id.isnot(None)).first():
            return
    bg = Path(background)
    origin_path = bg.with_suffix(bg.suffix + ".origin.json")
    origin = json.loads(origin_path.read_text()) if origin_path.exists() else {}
    if origin.get("sha256") != file_hash(bg):
        origin = {}
    data = {"video_sha256": file_hash(path), "background_sha256": file_hash(bg),
            "background_kind": origin.get("kind") or ("image" if bg.suffix.lower() in {".jpg", ".jpeg", ".png"} else "video"),
            "background_origin": origin,
            "effect_requested": effect, "effect_applied": effect if applied else "",
            "filter_sha256": hashlib.sha256(filter_graph.encode()).hexdigest(),
            "renderer": "ffmpeg-libass", "at": datetime.now(timezone.utc).isoformat()}
    path.with_suffix(path.suffix + ".creative.json").write_text(json.dumps(data))


def persist_render_evidence(job_id, job_dir):
    from database import SessionLocal, Job, AuditLog, AIProvenance
    path = Path(job_dir) / "lyric_video.mp4"
    if not path.is_file():
        return
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).with_for_update().first()
        if not job or not job.campaign_id:
            return
        video_hash = file_hash(path)
        sidecar = path.with_suffix(".mp4.creative.json")
        encoded = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        if encoded.get("video_sha256") != video_hash:
            encoded = {}  # A stale encode receipt cannot certify a new render.
        rp = job.render_params or {}
        models, proofs = [], []
        origin = encoded.get("background_origin") or {}
        if origin.get("provenance_id") and origin.get("sha256") == encoded.get("background_sha256"):
            call = db.query(AIProvenance).filter_by(id=origin["provenance_id"], job_id=job_id).first()
            if call and call.tool_name == origin.get("model") and str(call.response_summary or "").startswith("video_generated:"):
                models.append(call.tool_name)
                proofs.append({"id": call.id, "provider": call.tool_provider, "model": call.tool_name,
                               "prompt_hash": call.prompt_hash})
        old = rp.get("campaign_render_evidence") or {}
        if encoded.get("background_sha256") and old.get("background_sha256") == encoded["background_sha256"]:
            # Native retry/edit may download the same bound background bytes
            # without another provider call or a sidecar on the new replica.
            models = models or old.get("models", [])
            proofs = proofs or old.get("provenance", [])
            encoded["background_kind"] = old.get("background_kind", encoded.get("background_kind"))
        data = {**encoded, "video_sha256": video_hash, "at": datetime.now(timezone.utc).isoformat(),
                "models": sorted(set(models)), "provenance": proofs,
                "degraded": bool(rp.get("bg_animation_degraded")),
                "settings": {k: v for k, v in rp.items() if isinstance(v, (str, bool, int, float))}}
        job.render_params = {**rp, "campaign_render_evidence": data}
        db.add(AuditLog(user_id=job.user_id, action="campaign.creative.rendered", detail={
            "campaign_id": job.campaign_id, "tenant_id": job.tenant_id, "job_id": job_id,
            "evidence": data, "receipt": rp.get("campaign_creative_receipt")}))
        db.commit()
