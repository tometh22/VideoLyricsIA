"""Explicit campaign model choices; ordinary jobs retain their configured model."""
import os

VEO_LITE = "veo-3.1-lite-generate-001"


def veo_models():
    configured = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001").strip()
    models = [configured, VEO_LITE]
    static = os.environ.get("VEO_MODEL_STATIC", "").strip()
    if static:
        models.append(static)
    labels = {VEO_LITE: "Veo Lite (vista previa)", "veo-3.1-fast-generate-001": "Veo Fast",
              "veo-3.1-generate-001": "Veo 3.1"}
    return [{"id": model, "label": labels.get(model, model)} for model in dict.fromkeys(models)]


def model_for_campaign_job(job_id, fallback):
    if not job_id:
        return fallback
    from database import Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).first()
        if not job or not job.campaign_id:
            return fallback
        assignment = ((job.render_params or {}).get("campaign_creative_receipt") or {}).get("assignment") or {}
        if assignment.get("requirement") != "veo":
            return fallback
        model = assignment.get("model")
        if model not in {m["id"] for m in veo_models()}:
            raise ValueError("El modelo contractual ya no está habilitado; no se reemplazará por otro motor")
        return model
