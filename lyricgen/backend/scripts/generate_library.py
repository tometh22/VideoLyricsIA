"""Bulk-generate the pre-approved background library.

Quality tier (this is the marquee library — render-time generation
keeps the cheaper tiers):
  - Imagen 4 Ultra: $0.08 per still
  - Veo 3.1 standard: $0.40/s × 5s = $2 per clip

Two generators feed the library:
  - Imagen 4 Ultra: still photos. Pipeline animates them at render time
    via Ken Burns / parallax depending on the operator's chosen
    movement_style.
  - Veo 3.1 standard: 5-second seamless loops. Pipeline palindromes
    them to fill the song length.

Initial seed library (matches Tomi's launch ask):
  - 10 still photos (Imagen 4 Ultra) across 10 concepts
  - 5 cinematic photorealistic videos (Veo standard)
  - 5 simple/illustrated videos (Veo standard with "animado" style)
  = 20 assets, ~$20.80 total Vertex spend.

Run locally (NOT in production worker):

    cd lyricgen/backend
    source venv/bin/activate
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
    export VERTEX_PROJECT=...
    export R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
           R2_ENDPOINT_URL=... R2_BUCKET=...
    export DATABASE_URL=postgresql://...   # production DB
    python scripts/generate_library.py

Idempotent: skips concepts that already have ≥ N assets of the same kind
in the DB (so re-running after partial failures is safe).
"""

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage
from database import SessionLocal, BackgroundAsset
from pipeline import _generate_imagen_image, _generate_veo_video


# Quality tiers for the seed library — overridable via env if a project
# doesn't have Ultra access.
IMAGEN_MODEL = os.environ.get("LIBRARY_IMAGEN_MODEL", "imagen-4.0-ultra-generate-001")
VEO_MODEL    = os.environ.get("LIBRARY_VEO_MODEL", "veo-3.1-generate-001")

# Each tuple: (concept, asset_type, prompt)
# asset_type: "image" → Imagen 4 Ultra still
#             "video_cinematic" → Veo photorealistic
#             "video_simple" → Veo "animado" 2D illustrated
#
# Prompts are written in award-cinematography language (lens, color
# grade, motion direction) because Imagen / Veo respond strongly to
# specific cinematographic cues. Each one is engineered to:
#   - have no people / faces / text (Imagen safety + UMG compliance)
#   - read as a *background* (no dominant subject competing with lyrics)
#   - hold detail at 1920×1080 after upscaling
#   - loop seamlessly (the videos)
SEED: list[tuple[str, str, str]] = [
    # ── 10 Imagen 4 Ultra stills ─────────────────────────────────────
    ("naturaleza",  "image",
     "Award-winning landscape cinematography, dense lush green forest canopy seen from below, dappled golden hour sunlight filtering through leaves, soft volumetric god rays, hyper-detailed leaf texture, shallow depth of field, shot on Arri Alexa, 50mm prime lens, teal-and-gold color grade, photorealistic, ultra sharp 8K"),
    ("tropical",    "image",
     "High-end travel cinematography, top-down aerial of a deserted tropical beach, white sand contrasting against gradient turquoise to deep cobalt water, gentle white wave foam curling on shore, three palm trees casting long late-afternoon shadows, golden hour, shot on DJI Inspire 3, photorealistic, ultra sharp 8K"),
    ("acuatico",    "image",
     "Underwater cinematic photography, sunlight rays piercing crystalline blue ocean from above, drifting microbubbles catching highlights, soft moving caustics on a sandy seabed, slight god-ray volumetrics, shot on a RED Komodo with underwater housing, deep blue color palette, hyper-detailed, photorealistic 8K"),
    ("ciudad",      "image",
     "Cinematic skyline of a modern glass-and-steel megacity at blue hour, warm office lights twinkling against deep navy sky, soft low fog rolling between towers, mirror-like reflections in skyscraper glass, gentle drifting clouds, anamorphic widescreen composition, shot on Sony Venice 2, teal-and-amber grade, ultra sharp photorealistic 8K"),
    ("urbano",      "image",
     "Brutalist concrete architecture lit by harsh midday sun, dramatic geometric shadows from a stairwell cascading down a wall, raw textured concrete, severe symmetrical composition, low-saturation muted palette with one accent of cobalt blue, shot on Hasselblad medium format, fine-art photography, ultra sharp 8K"),
    ("cosmico",     "image",
     "Deep-space vista with a vibrant nebula in violet, magenta and teal cloud structures, scattered distant stars, faint galaxy spiral in the lower third, hyper-detailed gas turbulence, Hubble-style cinematic composition, dark void negative space for compositing, ultra sharp photorealistic 8K"),
    ("atmosferico", "image",
     "Single cinematic mountain peak above an endless sea of low clouds at sunrise, soft pink and apricot sky transitioning to deep cobalt at the top, distant ridge silhouettes, dramatic atmospheric layering, shot on Arri Alexa with anamorphic lens, fine-art landscape photography, ultra sharp 8K"),
    ("romantico",   "image",
     "Warm intimate bokeh of fairy string lights wrapped around a wooden pergola at dusk, blurred peach-and-rose color palette, soft creamy bokeh circles, late golden hour, shallow depth of field shot on 85mm f1.4 prime lens, romantic atmosphere, ultra sharp 8K, no people"),
    ("lujo",        "image",
     "Premium product photography of a polished black marble surface with thin gold veins, a single soft caustic highlight from diffused studio light, ultra luxurious editorial composition, shot on Hasselblad medium format with macro lens, hyper-detailed, photorealistic 8K"),
    ("minimalista", "image",
     "Architectural minimalism, soft pastel gradient wall in cream and dusty rose, one matte off-white sphere floating slightly above the floor casting a long soft shadow, single key light from upper left, ultra clean composition, shot on Hasselblad medium format, editorial photography, ultra sharp 8K"),

    # ── 5 cinematic Veo videos (photorealistic, marquee feel) ───────
    ("cinematic",   "video_cinematic",
     "Cinematic anamorphic 5-second seamless loop, slow camera dolly forward along an empty rain-soaked highway at dusk, dramatic teal-orange sky with slow drifting cumulus, gentle lens flare, ultra wide vista, photorealistic, shot on Arri Alexa, no text, no people"),
    ("ciudad",      "video_cinematic",
     "Cinematic 5-second seamless loop, slow aerial drone orbit around a single glass skyscraper at blue hour, soft warm office lights twinkling individually, mirror reflections of pink-violet sky in the glass, gentle smooth motion, photorealistic, ultra sharp"),
    ("naturaleza",  "video_cinematic",
     "Cinematic 5-second seamless loop, slow camera push through a misty pine forest at dawn, golden god rays cutting between trees, dust motes drifting through the light, ultra slow tracking shot, shot on Arri Alexa, photorealistic teal-and-gold grade"),
    ("club",        "video_cinematic",
     "Cinematic 5-second seamless loop, slow lateral motion through magenta and cyan laser beams cutting volumetric stage smoke, deep dark room, high contrast, ambient atmospheric, photorealistic, no people, no text"),
    ("acuatico",    "video_cinematic",
     "Cinematic 5-second seamless loop, slow underwater tracking shot just below the ocean surface, sunlight caustics dancing on the seabed, soft floating microbubbles, deep blue color palette, photorealistic ultra clean"),

    # ── 5 simple/illustrated Veo videos (2D animation feel) ─────────
    ("abstracto",   "video_simple",
     "Stylised flat 2D motion graphic, soft flowing pastel ink swirls in violet, teal and peach blending across a clean off-white background, slow elegant fluid motion, seamless 5-second loop, no text, no logos"),
    ("animado",     "video_simple",
     "Stylised flat 2D animated illustration in the style of a modern children's book, slow rolling pastel hills with a soft glowing sun, gentle multi-layer parallax motion, dreamy palette, seamless 5-second loop, no text"),
    ("vintage",     "video_simple",
     "Stylised retro 8mm film aesthetic, soft warm sepia frames with subtle grain, gentle dust scratches drifting upward, slow vintage color cycling, seamless 5-second loop, no text, no people"),
    ("minimalista", "video_simple",
     "Stylised minimal motion graphic, a single luminous curved line slowly tracing across a deep matte black background then resetting, ultra clean architectural composition, seamless 5-second loop"),
    ("atmosferico", "video_simple",
     "Stylised cinematic 5-second seamless loop, slow drifting fog rolling across an empty cool-blue plain at twilight, ambient minimal motion, painterly atmospheric quality, no text, no people"),
]


def already_populated(db, concept: str, asset_type: str) -> bool:
    """Idempotency guard: skip if a row already exists for this exact
    (concept, asset_type) pair. Tags carry both fields, separated by comma.
    """
    target_tags = f"{concept},{asset_type}"
    return (
        db.query(BackgroundAsset)
        .filter(BackgroundAsset.tags == target_tags)
        .filter(BackgroundAsset.is_active == True)
        .count() > 0
    )


def main() -> int:
    if not storage.is_enabled():
        print("ERROR: R2 not configured. Set R2_* env vars.")
        return 1
    if not os.environ.get("VERTEX_PROJECT"):
        print("ERROR: VERTEX_PROJECT not set.")
        return 1

    # Force the higher-quality models for this generation. The runtime
    # render path keeps using the cheaper defaults via env.
    os.environ["VEO_MODEL"] = VEO_MODEL
    print(f"Models: imagen={IMAGEN_MODEL}, veo={VEO_MODEL}")

    db = SessionLocal()
    created = 0
    skipped = 0
    failed = 0
    try:
        for idx, (concept, asset_type, prompt) in enumerate(SEED, start=1):
            label = f"{idx:02d}/{len(SEED)} {concept} [{asset_type}]"

            if already_populated(db, concept, asset_type):
                print(f"[SKIP] {label}: already in library")
                skipped += 1
                continue

            short_id = uuid.uuid4().hex[:12]
            ext = ".jpg" if asset_type == "image" else ".mp4"
            file_type = "jpg" if asset_type == "image" else "mp4"
            tmp_path = os.path.join(tempfile.gettempdir(),
                                     f"library_{short_id}{ext}")

            print(f"\n[GEN] {label}")
            print(f"      prompt: {prompt[:100]}…")
            try:
                if asset_type == "image":
                    _generate_imagen_image(prompt, tmp_path, model=IMAGEN_MODEL)
                elif asset_type == "video_cinematic":
                    _generate_veo_video(
                        prompt, tmp_path,
                        cache_namespace="library",
                        movement_style="",
                    )
                elif asset_type == "video_simple":
                    _generate_veo_video(
                        prompt, tmp_path,
                        cache_namespace="library",
                        movement_style="animado",
                    )
                else:
                    raise ValueError(f"Unknown asset_type {asset_type!r}")
            except Exception as e:
                print(f"[FAIL] generation: {e}")
                failed += 1
                continue

            # Upload to R2 — `library/` prefix is the read-path signal.
            r2_key = f"library/{short_id}{ext}"
            try:
                storage.upload_file(tmp_path, r2_key)
            except Exception as e:
                print(f"[FAIL] R2 upload: {e}")
                failed += 1
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                continue

            asset = BackgroundAsset(
                name=f"{concept.title()} — {asset_type.replace('_', ' ')}",
                filename=r2_key,
                file_type=file_type,
                tags=f"{concept},{asset_type}",
                uploaded_by=None,
                is_active=True,
            )
            db.add(asset)
            db.commit()
            created += 1

            size_kb = os.path.getsize(tmp_path) / 1024
            print(f"[OK] asset_id={asset.id} key={r2_key} ({size_kb:.0f} KB)")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        print(f"\n=== Library bulk done ===")
        print(f"  created: {created}")
        print(f"  skipped: {skipped}")
        print(f"  failed:  {failed}")
        return 0 if failed == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
