"""Balanced batch designs. Quotas are exact; the seed only mixes recipients.

Reuse the creative preview/apply ledger: preview never enqueues a render, and
apply still checks authorization, expiry, locks, revision and full fingerprint.
"""
from collections import Counter
from functools import lru_cache

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Job, get_db
import campaign_creative as creative

router = APIRouter(prefix="/batch/campaigns", tags=["campaigns"])
EDITABLE = {"transcribed_pending", "transcribed", "lyrics_approved", "transcribing",
            "transcribing_queued", "separation_ready", "separating", "separation_queued"}


@lru_cache(maxsize=1)
def catalog():
    looks = []
    for look in creative._lyric_looks.LOOKS:
        compatible = look not in creative._ALWAYS_OWN_BACKGROUND and not any(
            creative._lyric_recipes.may_own_background(look, genre)
            for genre in ("", *creative._lyric_recipes.GENRES)
        )
        looks.append({"id": look, "compatible": compatible,
                      "reason": "" if compatible else "Puede reemplazar el fondo; no participa del reparto foto/video."})
    return {"looks": looks, "effects": [e for e in creative.FIELDS["effect"]["options"] if e not in ("", "none", "foto_viva")],
            "editable_statuses": sorted(EDITABLE | {"waiting"})}


class MixRequest(creative.Strict):
    revision: int = Field(ge=0)
    item_ids: list[str] = Field(min_length=1, max_length=1000)
    looks: list[str] = Field(min_length=1, max_length=16)
    effects: list[str] = Field(default_factory=list, max_length=40)
    photo_percent: int = Field(default=50, ge=0, le=100, strict=True)
    seed: str = Field(default="campaign", min_length=1, max_length=100)
    replace_exceptions: bool = False


def groups_for(body, count):
    allowed = {look["id"] for look in catalog()["looks"] if look["compatible"]}
    if len(set(body.looks)) != len(body.looks) or not set(body.looks) <= allowed:
        creative.fail("Elegí looks compatibles con foto y video, sin repetirlos")
    if len(set(body.effects)) != len(body.effects) or not set(body.effects) <= set(catalog()["effects"]):
        creative.fail("Elegí efectos de foto válidos, sin repetirlos")
    photos = (count * body.photo_percent + 50) // 100
    if photos and not body.effects:
        creative.fail("Elegí al menos un efecto para las fotos")
    # Rotate each dimension independently. Give the remainder to a seeded
    # permutation, not always the first option in the catalogue.
    looks = sorted(body.looks, key=lambda x: creative.digest([body.seed, "looks", x]))
    effects = sorted(body.effects, key=lambda x: creative.digest([body.seed, "effects", x]))
    photo_slots = sorted(range(photos), key=lambda i: creative.digest([body.seed, "photo-slots", i]))
    photo_effects = {slot: effects[n % len(effects)] for n, slot in enumerate(photo_slots)}
    combinations = Counter()
    for n in range(count):
        kind = "photo_effect" if n < photos else "veo"
        combinations[kind, photo_effects.get(n, ""), looks[n % len(looks)]] += 1
    groups = []
    for (kind, effect, look), total in sorted(combinations.items()):
        settings = {"lyric_look": look, "lyric_genre": "", "lyric_variant": "",
                    "lyric_font_main": "", "lyric_font_key": "", "lyric_color": "#FFFFFF",
                    "lyric_sung_color": "#FFFFFF", "lyric_alt_color": "", "lyric_accent_color": "",
                    "font": "", "font_scale": 1., "lyrics_animation": "none", "line_transition": "none",
                    "movement_style": "foto-parallax" if kind == "photo_effect" else "estandar",
                    "effect": effect, "background_id": None, "background_mode": "as_is",
                    "animate_image": False, "enable_scenes": False, "scene_source": "lyrics"}
        groups.append(creative.Group(id="mix-" + creative.digest([kind, effect, look])[:20],
            name=f"{'Foto + ' + effect if effect else 'Video'} · {look}", weight=total,
            requirement=kind, model=creative.VEO_LITE if kind == "veo" else "", settings=settings))
    return groups


@router.post("/{campaign_id}/creative/mix-preview")
def preview_mix(campaign_id: str, body: MixRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = creative._campaign(db, campaign_id, current_user, True)
    selected = [i for i in creative._items(db, campaign) if i.id in set(body.item_ids)]
    # Do not silently drop foreign IDs or duplicates before entering the
    # shared preview validator.
    if len(selected) != len(body.item_ids):
        creative.fail("La selección contiene canciones ajenas o repetidas", 422)
    available = [i for i in selected if body.replace_exceptions or not (i.render_overrides or {}).get(creative.ASSIGNMENT, {}).get("pinned")]
    if not available:
        creative.fail("Todos los diseños están fijados. Activá su reemplazo si querés cambiarlos.")
    groups = groups_for(body, len(available))
    result = creative.preview(campaign_id, creative.PlanRequest(
        revision=body.revision, item_ids=body.item_ids, groups=groups, mode="count", seed=body.seed,
        replace_exceptions=body.replace_exceptions, preserve_existing_pins=True, reason="Reparto equilibrado de looks, fondos y efectos desde Preparar lote",
    ), current_user, db)
    changed_ids = [c["item_id"] for c in result["changes"]]
    ready = db.query(Job.campaign_item_id).filter(Job.campaign_id == campaign_id,
        Job.campaign_item_id.in_(changed_ids), Job.status == "lyrics_approved").all()
    result["ready_item_ids"] = sorted({r[0] for r in ready})
    result["mix"] = {"photo_percent": body.photo_percent, "total": len(changed_ids),
                     "photo_effect": sum(c["after"]["movement_style"] == "foto-parallax" for c in result["changes"]),
                     "looks": dict(Counter(c["after"]["lyric_look"] for c in result["changes"])),
                     "effects": dict(Counter(c["after"]["effect"] for c in result["changes"] if c["after"]["movement_style"] == "foto-parallax"))}
    result["mix"]["veo"] = result["mix"]["total"] - result["mix"]["photo_effect"]
    return result
