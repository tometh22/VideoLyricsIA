"""Bulk-generate the pre-approved background library.

Two generators feed the library:
  - Imagen 4: still photos. Pipeline animates them at render time via
    Ken Burns / parallax depending on the operator's chosen
    movement_style. ~$0.04 per image.
  - Veo 3.1 Fast: 5-second seamless loops. Pipeline palindromes them
    to fill the song length. ~$2 per clip.

Initial seed library (matches Tomi's launch ask):
  - 10 still photos (Imagen 4) across 10 concepts
  - 5 cinematic photorealistic videos (Veo)
  - 5 simple/illustrated videos (Veo with "animado" movement style)
  = 20 assets, ~$20.40 total Vertex spend.

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


# Each tuple: (concept, asset_type, prompt)
# asset_type: "image" → Imagen 4 still | "video_cinematic" → Veo
# photorealistic | "video_simple" → Veo "animado" / illustrated
SEED: list[tuple[str, str, str]] = [
    # ── 10 Imagen 4 stills ───────────────────────────────────────────
    ("naturaleza",  "image",
     "Cinematic wide shot of dense lush green forest canopy, golden afternoon sunlight filtering through, soft volumetric haze, deep depth of field, ultra detailed, 8k, photorealistic"),
    ("tropical",    "image",
     "Aerial shot of tropical white-sand beach with turquoise water, gentle wave foam, palm trees casting long shadows, late afternoon golden light, no people, no text"),
    ("acuatico",    "image",
     "Underwater shot of sunlight rays piercing deep blue ocean, drifting plankton, soft caustics on the seabed, cinematic, ultra clean, photorealistic"),
    ("ciudad",      "image",
     "Cinematic skyline of a modern glass city at blue hour, warm office lights, soft fog, reflections in skyscraper glass, slow drifting clouds, no people, no text"),
    ("urbano",      "image",
     "Concrete brutalist architecture, dramatic shadows of geometric stairwells, midday harsh sunlight, raw urban texture, cinematic muted palette, no people"),
    ("cosmico",     "image",
     "Deep space vista with vibrant nebula clouds in violet and teal, scattered distant stars, cinematic Hubble-style composition"),
    ("atmosferico", "image",
     "Lone cinematic mountain peak above sea of clouds at sunrise, soft pink and orange sky, stark dramatic atmosphere, cinematic"),
    ("romantico",   "image",
     "Soft warm bokeh string lights at dusk on a wooden terrace, blurred romantic atmosphere, peach and rose tones, cinematic shallow depth, no people, no text"),
    ("lujo",        "image",
     "Marble and gold textured surface with dramatic studio lighting, soft caustic reflections, premium luxury cinematic still, ultra clean"),
    ("minimalista", "image",
     "Soft pastel gradient backdrop with one floating geometric sphere casting subtle shadow, ultra clean minimal cinematic, studio look"),

    # ── 5 cinematic Veo videos (photorealistic, marquee feel) ────────
    ("cinematic",   "video_cinematic",
     "Cinematic anamorphic shot of slow drifting clouds across a vast empty highway at dusk, dramatic teal-orange sky, gentle camera dolly forward, photorealistic, 5 second seamless loop"),
    ("ciudad",      "video_cinematic",
     "Slow aerial drone shot circling a glass skyscraper at blue hour, soft window lights twinkling, gentle motion, ultra clean photorealistic, seamless loop"),
    ("naturaleza",  "video_cinematic",
     "Slow motion shot of sunlight rays moving through a misty pine forest at dawn, soft particles drifting in air, gentle camera dolly, photorealistic, seamless 5 second loop"),
    ("club",        "video_cinematic",
     "Slow motion shot of magenta and cyan laser beams cutting through smoke in a dark venue, deep contrast, ambient cinematic, photorealistic, seamless 5 second loop, no people, no text"),
    ("acuatico",    "video_cinematic",
     "Slow underwater shot of sunlight caustics rippling across the deep blue ocean floor, soft drifting bubbles, cinematic, photorealistic, seamless 5 second loop"),

    # ── 5 simple/illustrated Veo videos (2D animation feel) ─────────
    ("abstracto",   "video_simple",
     "Soft flowing pastel ink swirls in violet, teal and peach on a clean background, abstract macro, slow gentle motion, seamless 5 second loop"),
    ("animado",     "video_simple",
     "Stylised flat 2D animated illustration of slow rolling cartoon hills under a calm sun, soft pastel palette, gentle parallax motion, seamless 5 second loop, no text"),
    ("vintage",     "video_simple",
     "Soft vintage film grain texture with warm sepia color shifts, slow subtle motion, retro 8mm feel, seamless 5 second loop, no text"),
    ("minimalista", "video_simple",
     "Single curved line of light slowly drifting across a deep matte black background, abstract minimal architectural, gentle motion, seamless 5 second loop"),
    ("atmosferico", "video_simple",
     "Slow drifting fog over a still empty plain at twilight, cool blue palette, ambient minimal motion, seamless 5 second loop, no text"),
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
                    _generate_imagen_image(prompt, tmp_path)
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
