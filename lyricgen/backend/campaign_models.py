"""Mandatory model policy for all newly generated Veo backgrounds.

Environment overrides and historical campaign choices cannot enable a more
expensive model. Historical provenance remains unchanged.
"""

VEO_LITE = "veo-3.1-lite-generate-001"
VEO_POLICY = "veo-lite-only-v1"


def veo_models():
    return [{"id": VEO_LITE, "label": "Veo Lite"}]


def model_for_campaign_job(job_id=None, fallback=None):
    """Keep the legacy call signature while enforcing Lite for every job."""
    return VEO_LITE


def effective_veo_assignment(assignment):
    """Apply current policy to a new generation without rewriting its history."""
    if not assignment or assignment.get("requirement") != "veo":
        return assignment
    result = {**assignment, "model": VEO_LITE, "model_policy": VEO_POLICY}
    if assignment.get("model") and assignment["model"] != VEO_LITE:
        result["requested_model"] = assignment["model"]
    return result
