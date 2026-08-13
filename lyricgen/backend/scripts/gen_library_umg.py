"""Bulk-generate the UMG-curated background library (50 assets).

Why a second script (vs extending generate_library.py):
  - The legacy script seeds 20 "core" abstract concepts (naturaleza,
    ciudad, cosmico, etc.). The UMG batch is more specific — 11 real-
    world lyric-video references from artists like AIRBAG, Beret, J Balvin,
    Yatra, Anne-Marie, etc., catalogued during the May 2026 frames-
    extraction session.
  - Keeping seeds separate lets us re-run UMG without touching legacy
    rows (the `already_populated()` guard is tag-equality based).
  - Different defaults: tenant locked to "universal-music", Veo on FAST
    model (~75% cheaper than legacy's standard), naming includes "UMG"
    prefix so the operator distinguishes at-a-glance in admin UI.

Cost & timing (50 assets, defaults as below):
  - 35 × Imagen 4 Ultra @ $0.15 = $5.25
  - 15 × Veo 3.1 Fast    @ $0.80 = $12.00
  - Total: ~$17 Vertex spend
  - Wall clock: ~30 min (Veo's 5s polling × 15 clips, parallelised by
    Vertex's own queue; Imagen is ~3s each).

Run locally (NOT in production worker):

    cd lyricgen/backend
    source venv/bin/activate
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
    export VERTEX_PROJECT=...
    export R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
           R2_ENDPOINT_URL=... R2_BUCKET=...
    export DATABASE_URL=postgresql://...
    python scripts/gen_library_umg.py

Idempotent: re-running skips (concept_slug, asset_type) pairs already
present in the DB (via tag uniqueness). Generation cache also kicks
in for Veo prompts seen before, so partial-failure recovery is free.
"""

import argparse
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage
from internal_tracking import ensure_internal_tracking_job
from pipeline import _generate_imagen_image, _generate_veo_video

# Keep import failure handling so the CLI can print a useful diagnostic, but a
# live DB is now mandatory: every paid generation needs a persistent internal
# Job before it may reach Vertex. Per-row JSON checkpoints still protect
# completed assets from later metadata-write failures.
try:
    from database import SessionLocal, BackgroundAsset
    _DB_IMPORTABLE = True
except Exception:
    SessionLocal = None
    BackgroundAsset = None
    _DB_IMPORTABLE = False


# Quality tiers — fixed defaults reflecting the UMG batch decision
# (May 2026): Imagen ULTRA for stills (premium library justifies the
# ~$0.07 premium over standard), Veo FAST for video (~75% cheaper than
# Veo standard at the cost of slightly less polish; operator can swap
# specific clips for hand-crafted ones via Admin UI if needed).
IMAGEN_MODEL = os.environ.get("LIBRARY_IMAGEN_MODEL", "imagen-4.0-ultra-generate-001")
VEO_MODEL    = os.environ.get("LIBRARY_VEO_MODEL",    "veo-3.1-fast-generate-001")


# SEED: list[tuple[concept_slug, asset_type, prompt]]
#   concept_slug : kebab-case identifier, becomes the first tag and the
#                  idempotency key (paired with asset_type).
#   asset_type   : "image"           → Imagen 4 Ultra still
#                  "video_cinematic" → Veo realistic clip
#                  "video_simple"    → Veo "animado" 2D illustrated clip
#
# Prompts engineered to:
#   - have NO people / faces / text (Imagen safety + UMG compliance)
#   - read as a *background* (subject does not compete with lyrics)
#   - hold detail at 1920×1080
#   - loop seamlessly (video assets)
#
# Bucket → UMG ref mapping (see plan file for the catalogue):
#   atardecer-cinematografico → Mi Gente, Hecha pa' mí, Cristina
#   cielo-estrellado          → Beret "Lo siento"
#   montana-nieve             → AIRBAG "Pensamientos", Stereo Hearts
#   mar-olas                  → Anne-Marie "2002"
#   ciudad-nocturna-lluvia    → 2002, A los cuatro vientos
#   playa-tropical            → varios UMG refs
#   nebulosa-cosmica          → Yatra "Cómo Mirarte"
#   abstracto-fluido          → Danny Ocean "Me Rehúso"
#   lluvia-camino             → rain-road silhouette ref
#   atardecer-urbano          → extra mood, no specific ref
SEED: list[tuple[str, str, str]] = [
    # ─── 1. Atardecer cinematográfico × 8 (images, foto-parallax intent) ───
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic horizon at golden hour, layered mountain silhouettes against vibrant gradient sky transitioning from deep magenta and coral pink at top through fiery orange to amber on the horizon, soft warm haze, no people, no text, anamorphic widescreen, shot on Arri Alexa with 50mm prime lens, fine-art landscape photography, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic golden hour over a calm tropical ocean, vivid red-orange to deep magenta sky, silhouettes of distant palm trees and shoreline reeds, soft warm reflections on perfectly still water, no people, no text, shot on Arri Alexa with 35mm prime, fine-art photography, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic dramatic sunset over a misty mountain valley, layered ridges fading into atmospheric haze, sky transitioning from molten orange at horizon to cool indigo at zenith, soft volumetric god rays, no people, no text, ultra-cinematic wide shot, shot on RED Komodo with 50mm prime, photorealistic 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic golden hour silhouette of a single volcano against a vibrant crimson sky, distant smaller mountains layered in deep purple-magenta haze, painterly atmosphere, no people, no text, anamorphic wide shot, shot on Sony Venice 2, fine-art landscape, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic field at dusk, dry golden grass swaying gently toward a low pink-and-amber horizon, soft warm haze, single dramatic silhouette of a lone leafless tree on the right third, no people, no text, shot on Arri Alexa with 85mm telephoto compression, fine-art landscape, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic ocean horizon at sunset, gentle warm-pink and lavender pastel sky reflecting on glassy water, soft pillow clouds, no people, no land, no text, minimalist composition, anamorphic widescreen, shot on Arri Alexa, fine-art landscape photography, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic late-afternoon desert landscape, soft golden dunes with long shadows, distant sandstone mesa silhouettes in warm haze, vibrant peach-and-rose sky transitioning to cobalt above, no people, no text, shot on Hasselblad medium format, fine-art landscape photography, ultra-sharp 8K"),
    ("atardecer-cinematografico", "image",
     "Cinematic photorealistic riverside dusk, calm reflective river bending through silhouetted dark riverbanks, sky in molten coral and magenta with soft cumulus, no people, no boats, no text, anamorphic wide, shot on Sony Venice 2 with 35mm prime, photorealistic 8K"),

    # ─── 2. Cielo estrellado nocturno × 5 (images, sutil drift intent) ───
    ("cielo-estrellado", "image",
     "Astrophotography composite at night, Milky Way galaxy core arching across a deep navy sky strewn with vivid stars, silhouetted dark rooftops and a single thin TV antenna on the lower edge, no people, no text, ultra-long-exposure cinematic, shot on Sony A7S III with 24mm f1.4 prime, hyper-detailed star field, ultra-sharp 8K"),
    ("cielo-estrellado", "image",
     "Astrophotography seamless wide of a star-strewn dark cobalt night sky, soft visible Milky Way band in the upper third, scattered bright stars with subtle lens flare, no foreground (pure sky composition), no people, no text, shot on Sony A7S III with 24mm f1.4 prime, ultra-detailed 8K"),
    ("cielo-estrellado", "image",
     "Astrophotography of a dark Patagonian sky with vibrant Milky Way galaxy core in the right third, vivid star clusters and nebulous dust lanes, silhouette of a single distant mountain peak at the bottom edge, no people, no text, ultra-long-exposure, shot on Sony A7S III with 14mm ultra-wide, hyper-detailed 8K"),
    ("cielo-estrellado", "image",
     "Astrophotography wide-field, scattered bright stars with subtle aurora-like green glow on the horizon, deep navy-to-violet gradient, silhouetted pine forest tree line on the bottom edge, no people, no text, ultra-long-exposure cinematic, shot on Sony A7S III with 20mm prime, fine-art 8K"),
    ("cielo-estrellado", "image",
     "Astrophotography close-up of a vivid star field with the Andromeda galaxy nebula visible on the right, deep purple-and-navy cosmos with scattered hot blue and amber stars, no foreground, no people, no text, hyper-detailed, ultra-long-exposure, shot on Sony A7S III with 200mm telephoto, fine-art 8K"),

    # ─── 3. Montaña con neblina × 5 (3 images + 2 video_cinematic) ───
    ("montana-nieve", "image",
     "Cinematic photorealistic snow-capped mountain at dusk, dramatic dark forest in the foreground with low rolling mist between the trees, moody overcast cobalt sky, the road below leading into the mist toward the peak, no people, no text, anamorphic wide, shot on Arri Alexa with 50mm prime, fine-art landscape photography, ultra-sharp 8K"),
    ("montana-nieve", "image",
     "Cinematic photorealistic single jagged mountain peak with snow at sunrise, soft pink-and-apricot atmospheric haze over a sea of low clouds, distant ridge silhouettes layered in cool indigo, no people, no text, shot on Arri Alexa with anamorphic lens, fine-art landscape, ultra-sharp 8K"),
    ("montana-nieve", "image",
     "Cinematic photorealistic vast frozen alpine valley, jagged snow peaks under a cold blue overcast sky, scattered pine trees casting long shadows, soft drifting low fog in the middle distance, no people, no text, ultra-cinematic wide shot, shot on RED Komodo with 35mm prime, fine-art landscape, ultra-sharp 8K"),
    ("montana-nieve", "video_cinematic",
     "Cinematic 5-second seamless loop, locked-off tripod shot of a snow-capped mountain valley at dawn, dramatic low rolling fog drifting slowly horizontally between dark pine trees, no camera motion, only scene motion (fog + faint cloud drift on the peak), pink-and-cobalt sky, photorealistic, shot on Arri Alexa, no people, no text"),
    ("montana-nieve", "video_cinematic",
     "Cinematic 5-second seamless loop, locked tripod shot of a dark pine forest in winter, gentle snow falling continuously, soft cool blue ambient light, mist drifting slowly between the trees, no camera motion, photorealistic, shot on RED Komodo with 50mm prime, no people, no text"),

    # ─── 4. Mar / olas costera × 5 (video_cinematic, estandar with motion) ───
    ("mar-olas", "video_cinematic",
     "Cinematic 5-second seamless loop, top-down aerial of waves breaking against rocky black coastline, white foam patterns moving in slow rhythm, deep teal ocean, photorealistic, no people, no boats, no text, shot on DJI Inspire 3 with X9 Pro camera, ultra-sharp 4K"),
    ("mar-olas", "video_cinematic",
     "Cinematic 5-second seamless loop, slow lateral pan along a pristine tropical beach, gentle azure waves curling onto white sand, distant palm tree silhouettes in the far right, photorealistic, no people, no text, shot on Arri Alexa with anamorphic lens, ultra-sharp 8K"),
    ("mar-olas", "video_cinematic",
     "Cinematic 5-second seamless loop, locked shot of waves crashing against a dramatic rocky outcrop at dusk, golden hour sunlight glinting on the foam, deep moody teal-and-amber color grade, photorealistic, no people, no text, shot on Arri Alexa, ultra-sharp 8K"),
    ("mar-olas", "video_cinematic",
     "Cinematic 5-second seamless loop, underwater half-and-half shot, top half a gentle tropical sky with drifting cumulus, bottom half clear turquoise water with sunlight caustics dancing on a sandy seabed, photorealistic, no people, no fish foreground, no text, shot on RED Komodo with underwater housing, ultra-sharp 4K"),
    ("mar-olas", "video_cinematic",
     "Cinematic 5-second seamless loop, slow drone fly-over of a deep blue ocean far from shore, gentle whitecaps in continuous motion, sunlight glinting on the surface, photorealistic, no people, no boats, no text, shot on DJI Inspire 3, ultra-sharp 4K"),

    # ─── 5. Ciudad nocturna mojada × 6 (4 image + 2 video_cinematic) ───
    ("ciudad-nocturna-lluvia", "image",
     "Cinematic photorealistic close-up of a window glass beaded with raindrops, out-of-focus warm-yellow and crimson red bokeh lights of a city street behind, dreamy depth of field, deep amber and burgundy color grade, shot on Arri Alexa with 85mm f1.4 macro lens, dark warm atmosphere, no people, no text, ultra-sharp 8K"),
    ("ciudad-nocturna-lluvia", "image",
     "Cinematic photorealistic wet city street at night, reflections of vivid neon signs (pink-magenta and electric blue) on a rain-slicked asphalt road, deep moody color grade, shallow depth of field with bokeh, no people, no text, anamorphic widescreen, shot on Sony Venice 2 with 50mm prime, ultra-sharp 8K"),
    ("ciudad-nocturna-lluvia", "image",
     "Cinematic photorealistic empty Tokyo-style alley at night, vibrant red lantern and blue neon signage reflected on wet pavement, soft drifting mist, deep teal-and-amber color grade, no people, no text, shot on Arri Alexa with 50mm anamorphic, ultra-sharp 8K"),
    ("ciudad-nocturna-lluvia", "image",
     "Cinematic photorealistic top-down aerial of a megacity at blue hour after rain, wet streets glowing with red and amber tail lights, glass-and-steel towers reflecting deep navy sky, no people, no text, shot on DJI Inspire 3, ultra-sharp 8K"),
    ("ciudad-nocturna-lluvia", "video_cinematic",
     "Cinematic 5-second seamless loop, locked shot of a window glass with raindrops slowly trickling down, out-of-focus warm bokeh of yellow and red city lights moving softly behind, ultra-shallow depth of field, deep amber-burgundy grade, photorealistic, no people, no text, shot on Arri Alexa with 85mm macro"),
    ("ciudad-nocturna-lluvia", "video_cinematic",
     "Cinematic 5-second seamless loop, slow aerial drone push over a wet city street at night, neon signs in pink and cyan reflecting on the asphalt, gentle vehicle light trails, photorealistic, no people foreground, no text, shot on DJI Inspire 3, ultra-sharp 4K"),

    # ─── 6. Naturaleza tropical / palmeras × 5 (3 image + 2 video) ───
    ("playa-tropical", "image",
     "High-end travel cinematography, top-down aerial of a deserted tropical beach, white sand contrasting against gradient turquoise to deep cobalt water, gentle white wave foam curling on shore, three palm trees casting long late-afternoon shadows, golden hour, no people, no text, shot on DJI Inspire 3, photorealistic ultra-sharp 8K"),
    ("playa-tropical", "image",
     "Cinematic photorealistic close-up of palm fronds against a vivid tropical sunset sky, deep silhouette palm leaves swaying gently, magenta-and-amber backlight, soft warm lens flare, no people, no text, shot on Arri Alexa with 50mm prime, fine-art tropical photography, ultra-sharp 8K"),
    ("playa-tropical", "image",
     "Cinematic photorealistic underwater shot just below the tropical ocean surface, sunlight rays piercing aquamarine water, soft sand on the seabed with drifting microbubbles, no people, no fish in foreground, no text, ultra-clean composition, shot on RED Komodo with underwater housing, photorealistic 8K"),
    ("playa-tropical", "video_cinematic",
     "Cinematic 5-second seamless loop, locked tripod shot of a calm tropical bay at sunset, gentle waves rolling onto white sand, three palm trees silhouetted against vibrant magenta-and-orange sky, fronds swaying very gently in warm ocean breeze, photorealistic, no people, no text, shot on Arri Alexa"),
    ("playa-tropical", "video_cinematic",
     "Cinematic 5-second seamless loop, slow aerial drone arc over a Caribbean lagoon at golden hour, turquoise water meeting white sand, distant palm-covered island in the haze, gentle wave patterns moving, photorealistic, no people, no text, shot on DJI Inspire 3, ultra-sharp 4K"),

    # ─── 7. Nebulosa cósmica × 4 (image, sutil + effect aurora intent) ───
    ("nebulosa-cosmica", "image",
     "Deep-space vista with a vibrant nebula in violet, magenta and teal cloud structures, scattered distant stars, faint galaxy spiral in the lower third, hyper-detailed gas turbulence, Hubble-style cinematic composition, dark void negative space for compositing, no people, no text, ultra-sharp 8K"),
    ("nebulosa-cosmica", "image",
     "Hubble-style deep space photograph of the Orion nebula region, vivid pink and electric blue gas clouds spiralling around a bright stellar core, scattered hot blue stars, deep cosmic void background, no people, no text, hyper-detailed astrophotography composite, ultra-sharp 8K"),
    ("nebulosa-cosmica", "image",
     "Astrophotography composite of a dreamy purple-and-blue cosmic cloudscape, soft glowing nebula tendrils, distant vibrant star clusters, gentle painterly atmosphere reminiscent of Sebastián Yatra Cómo Mirarte aesthetic, no people, no text, hyper-detailed 8K"),
    ("nebulosa-cosmica", "image",
     "Deep-space vista of a galaxy core with swirling arms of gas in deep magenta and electric teal, scattered bright stars, faint asteroid silhouettes, ultra-detailed, Hubble-style cinematic, no people, no text, ultra-sharp 8K"),

    # ─── 8. Abstracto fluido × 4 (video_simple, animado 2D intent) ───
    ("abstracto-fluido", "video_simple",
     "Stylised flat 2D motion graphic, soft flowing pastel ink swirls in violet, teal and peach blending across a clean off-white background, slow elegant fluid motion, seamless 5-second loop, no people, no text, no logos"),
    ("abstracto-fluido", "video_simple",
     "Stylised flat 2D motion graphic, vibrant warm gradient background (coral pink to deep magenta to amber) with soft undulating shapes flowing across, evocative of Danny Ocean 'Me Rehúso' aesthetic, gentle organic motion, seamless 5-second loop, no people, no text"),
    ("abstracto-fluido", "video_simple",
     "Stylised flat 2D animated wave pattern, deep teal and electric coral undulating bands flowing diagonally across a soft cream background, gentle elegant motion, seamless 5-second loop, no people, no text, no logos"),
    ("abstracto-fluido", "video_simple",
     "Stylised flat 2D animated illustration, slow rolling pastel hills with a soft glowing sun, gentle multi-layer parallax motion, dreamy palette (pink, lavender, cream), seamless 5-second loop, no people, no text"),

    # ─── 9. Bosque / lluvia nocturna × 4 (3 image + 1 video) ───
    ("lluvia-camino", "image",
     "Cinematic photorealistic narrow mountain path between dark rocky walls at night, soft moonlight illuminating heavy rainfall in slanted streaks, distant cold blue mist in the valley below, deep cool grade, no people, no text, anamorphic wide, shot on Arri Alexa with 35mm prime, fine-art moody landscape, ultra-sharp 8K"),
    ("lluvia-camino", "image",
     "Cinematic photorealistic dark forest in heavy rain, raindrops visible against a soft moonlight backlight, fog drifting between tall trees, deep cool blue-and-green grade, no people, no text, shot on Arri Alexa with 50mm prime, fine-art moody landscape, ultra-sharp 8K"),
    ("lluvia-camino", "image",
     "Cinematic photorealistic empty rain-soaked country road at dusk, reflective wet asphalt, distant farmhouse silhouette under a heavy grey sky, deep teal-and-amber grade, no people, no text, anamorphic wide, shot on RED Komodo with 35mm prime, fine-art photography, ultra-sharp 8K"),
    ("lluvia-camino", "video_cinematic",
     "Cinematic 5-second seamless loop, locked shot of a dark forest in heavy rain, continuous raindrops falling, soft mist drifting between the trees, distant lightning glow on the horizon, deep cool blue grade, photorealistic, no people, no text, shot on Arri Alexa with 50mm prime"),

    # ─── 10. Atardecer urbano × 4 (image, sutil drift intent) ───
    ("atardecer-urbano", "image",
     "Cinematic photorealistic rooftop view at sunset, layered city skyline silhouettes against a vivid magenta-and-amber sky, soft warm haze, scattered antennas and AC units in the foreground out-of-focus, no people, no text, anamorphic wide, shot on Sony Venice 2 with 50mm prime, fine-art urban photography, ultra-sharp 8K"),
    ("atardecer-urbano", "image",
     "Cinematic photorealistic skyline of a megacity at blue hour, warm office lights twinkling in deep navy sky, soft low fog rolling between towers, mirror-like reflections in skyscraper glass, anamorphic widescreen, no people, no text, shot on Sony Venice 2, teal-and-amber grade, ultra-sharp photorealistic 8K"),
    ("atardecer-urbano", "image",
     "Cinematic photorealistic empty rooftop at golden hour, weathered concrete edge in the foreground with distant city silhouette in the haze, gradient amber-and-pink sky, single bird silhouette in the upper left, no people, no text, shot on Hasselblad medium format, fine-art urban photography, ultra-sharp 8K"),
    ("atardecer-urbano", "image",
     "Cinematic photorealistic narrow alley between brick buildings at dusk, warm tungsten lamps reflecting on wet pavement, deep amber-and-burgundy grade, soft drifting steam from a vent, no people, no text, shot on Arri Alexa with 50mm prime, fine-art moody urban photography, ultra-sharp 8K"),
]

# Sanity check at module load time so a bad SEED edit fails fast.
assert len(SEED) == 50, f"SEED must be exactly 50 tuples (currently {len(SEED)})"


def existing_names_for_tenant(SessionFactory, owner_tenant_id: str | None) -> set[str]:
    """Resume cache: query DB once at startup for every `UMG …` asset name
    already present for this tenant. We skip any SEED row whose computed
    name is in this set.

    Why by `name` (and not by `tags` like the legacy script): the name is
    unique per (concept_slug, asset_index) — `UMG Montana nieve #4` —
    while tags are only unique per (concept_slug, asset_type). The
    original `already_populated()` was over-eager: one row of
    `montana-nieve,video_cinematic,umg` would mask the OTHER 4 video
    tuples for that concept. Resuming by name lets us recover row 17
    (Montana nieve #4) without skipping row 18 (Montana nieve #5).
    """
    if SessionFactory is None:
        return set()
    db = SessionFactory()
    try:
        q = (
            db.query(BackgroundAsset.name)
            .filter(BackgroundAsset.is_active == True)  # noqa: E712
            .filter(BackgroundAsset.tags.like("%,umg"))
        )
        if owner_tenant_id is None:
            q = q.filter(BackgroundAsset.owner_tenant_id.is_(None))
        else:
            q = q.filter(BackgroundAsset.owner_tenant_id == owner_tenant_id)
        return {row[0] for row in q.all()}
    finally:
        db.close()


def _try_open_db_factory():
    """Best-effort: validate DB connectivity in 5 s and return a *factory*
    (SessionLocal) that produces fresh sessions per commit. Returns None
    in no-db mode (R2 + JSON + SQL output, no Postgres writes).

    Critical change vs the previous `_try_open_db` that returned ONE
    long-lived session: a 30 min run with 5 min Veo idle windows kills
    the Railway SSL socket. `pool_pre_ping` and `pool_recycle` only fire
    on checkout, so a held session never benefits from them. By using a
    fresh session per row, every commit re-checks out and pre-pings the
    pooled conn before the INSERT.
    """
    if not _DB_IMPORTABLE or not os.environ.get("DATABASE_URL"):
        print("[DB] no DATABASE_URL or db module not importable — no-db mode")
        return None
    try:
        from sqlalchemy import create_engine, text
        url = os.environ["DATABASE_URL"]
        sep = "&" if "?" in url else "?"
        engine = create_engine(f"{url}{sep}connect_timeout=5", future=True)
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        # Use the project's SessionLocal — it already has pool_pre_ping,
        # pool_recycle=120, and TCP keepalives configured. With a fresh
        # session per commit, all three actually kick in.
        from database import SessionLocal as _SL
        return _SL
    except Exception as e:
        print(f"[DB] connect failed ({e.__class__.__name__}: {str(e)[:120]}) — no-db mode")
        return None


def _commit_one(SessionFactory, **kwargs) -> int:
    """Open a fresh session, insert one BackgroundAsset row, commit, close.
    Returns the new asset_id. Retries once on `OperationalError`
    (SSL drop) — pool_pre_ping on checkout discards the dead conn so
    the retry hits a fresh one."""
    from sqlalchemy.exc import OperationalError
    last_err = None
    for attempt in (1, 2):
        db = SessionFactory()
        try:
            asset = BackgroundAsset(**kwargs)
            db.add(asset)
            db.commit()
            return asset.id
        except OperationalError as e:
            last_err = e
            try:
                db.rollback()
            except Exception:
                pass
            if attempt == 1:
                print(f"[DB] commit OperationalError on attempt 1, retrying ({str(e)[:80]})")
                continue
            raise
        finally:
            db.close()
    raise last_err  # unreachable


def _sql_escape(s: str) -> str:
    return s.replace("'", "''") if s else ""


def _emit_sql(records: list[dict], path: str, owner_tenant_id: str | None) -> None:
    """Write idempotent INSERTs the user can paste into `railway connect
    Postgres`. Uses ON CONFLICT DO NOTHING on filename so re-running the
    same SQL is safe."""
    tenant_sql = f"'{_sql_escape(owner_tenant_id)}'" if owner_tenant_id else "NULL"
    with open(path, "w") as f:
        f.write("-- UMG library metadata import (generated by gen_library_umg.py)\n")
        f.write("-- Run inside Railway: `railway connect Postgres` then \\i this file\n\n")
        f.write("BEGIN;\n")
        for r in records:
            f.write(
                "INSERT INTO background_assets "
                "(name, filename, file_type, tags, uploaded_by, owner_tenant_id, is_active, created_at) "
                f"VALUES ('{_sql_escape(r['name'])}', '{_sql_escape(r['filename'])}', "
                f"'{_sql_escape(r['file_type'])}', '{_sql_escape(r['tags'])}', "
                f"NULL, {tenant_sql}, true, NOW()) "
                "ON CONFLICT DO NOTHING;\n"
            )
        f.write("COMMIT;\n")


def _format_name(concept_slug: str, idx_within_concept: int) -> str:
    """`UMG Atardecer cinematográfico #3` from `atardecer-cinematografico` + 3."""
    pretty = concept_slug.replace("-", " ").capitalize()
    return f"UMG {pretty} #{idx_within_concept}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("LIBRARY_OWNER_TENANT_ID", "universal-music"),
        help="owner_tenant_id to assign every generated asset (default: env "
             "LIBRARY_OWNER_TENANT_ID or 'universal-music'). Pass an empty "
             "string to generate global (visible-to-all) assets — NOT the "
             "intended use of this script, but supported for parity with the "
             "legacy generate_library.py.",
    )
    args = parser.parse_args()
    owner_tenant_id = args.tenant_id.strip() or None
    if owner_tenant_id:
        print(f"[TENANT] assets will be locked to owner_tenant_id={owner_tenant_id!r}")
    else:
        print("[TENANT] assets will be created as GLOBAL (visible to all tenants)")
    if not storage.is_enabled():
        print("ERROR: R2 not configured. Set R2_* env vars.")
        return 1
    if not os.environ.get("VERTEX_PROJECT"):
        print("ERROR: VERTEX_PROJECT not set.")
        return 1

    # Force the chosen models for this generation. The runtime render
    # path keeps using the cheaper defaults via env.
    os.environ["VEO_MODEL"] = VEO_MODEL
    print(f"Models: imagen={IMAGEN_MODEL}, veo={VEO_MODEL}")
    print(f"SEED: {len(SEED)} tuples")

    SessionFactory = _try_open_db_factory()
    no_db = SessionFactory is None
    if no_db:
        print("ERROR: database unavailable; refusing paid generation without "
              "persistent provenance tracking")
        return 1

    json_records: list[dict] = []
    json_out_path = "/tmp/library_umg_assets.json"

    # Resume support — primary source of truth is the DB itself.
    # We query every `UMG …` name already present for this tenant and
    # skip the SEED rows whose computed name is in that set. The JSON
    # file is kept as a secondary cache so a re-run without DB access
    # (no-db mode) can still resume from the last paid generation.
    resume_names: set[str] = existing_names_for_tenant(SessionFactory, owner_tenant_id)
    if resume_names:
        print(f"[RESUME] {len(resume_names)} UMG asset(s) already in DB for tenant — will skip those names")
    if os.path.exists(json_out_path):
        try:
            with open(json_out_path) as fh:
                prev = json.load(fh)
            for r in prev:
                # Re-export prior records so the final JSON/SQL stays complete.
                json_records.append(r)
                resume_names.add(r["name"])
            print(f"[RESUME] also loaded {len(prev)} record(s) from {json_out_path}")
        except Exception as e:
            print(f"[RESUME] could not load prior JSON ({e}), continuing")

    # Track per-concept count so we can name assets "UMG Atardecer #1, #2…"
    concept_seen: dict[str, int] = {}

    created = 0
    skipped = 0
    failed = 0
    try:
        for idx, (concept_slug, asset_type, prompt) in enumerate(SEED, start=1):
            concept_seen[concept_slug] = concept_seen.get(concept_slug, 0) + 1
            asset_index = concept_seen[concept_slug]
            label = f"{idx:02d}/{len(SEED)} {concept_slug} [{asset_type}] #{asset_index}"
            expected_name = _format_name(concept_slug, asset_index)

            if expected_name in resume_names:
                skipped += 1
                print(f"[SKIP] {label} — name '{expected_name}' already in DB/JSON")
                continue

            short_id = uuid.uuid4().hex[:12]
            ext = ".jpg" if asset_type == "image" else ".mp4"
            file_type = "jpg" if asset_type == "image" else "mp4"
            tmp_path = os.path.join(tempfile.gettempdir(),
                                     f"library_umg_{short_id}{ext}")

            print(f"\n[GEN] {label}")
            print(f"      prompt: {prompt[:100]}…")
            try:
                tracking_job_id = ensure_internal_tracking_job(
                    f"library-umg:{expected_name}", style=asset_type)
                if asset_type == "image":
                    _generate_imagen_image(
                        prompt, tmp_path, job_id=tracking_job_id,
                        model=IMAGEN_MODEL)
                elif asset_type == "video_cinematic":
                    _generate_veo_video(
                        prompt, tmp_path, job_id=tracking_job_id,
                        cache_namespace="library_umg",
                        movement_style="",
                        require_persistent_tracking=True,
                    )
                elif asset_type == "video_simple":
                    _generate_veo_video(
                        prompt, tmp_path, job_id=tracking_job_id,
                        cache_namespace="library_umg",
                        movement_style="animado",
                        require_persistent_tracking=True,
                    )
                else:
                    raise ValueError(f"Unknown asset_type {asset_type!r}")
            except Exception as e:
                print(f"[FAIL] generation: {e}")
                failed += 1
                continue

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

            record = {
                "name": expected_name,
                "filename": r2_key,
                "file_type": file_type,
                "tags": f"{concept_slug},{asset_type},umg",
                "concept_slug": concept_slug,
                "asset_type": asset_type,
            }
            json_records.append(record)
            resume_names.add(expected_name)

            if no_db:
                created += 1
                size_kb = os.path.getsize(tmp_path) / 1024
                print(f"[OK] (no-db) key={r2_key} ({size_kb:.0f} KB)")
            else:
                try:
                    asset_id = _commit_one(
                        SessionFactory,
                        name=record["name"],
                        filename=record["filename"],
                        file_type=record["file_type"],
                        tags=record["tags"],
                        uploaded_by=None,
                        owner_tenant_id=owner_tenant_id,
                        is_active=True,
                    )
                except Exception as e:
                    # DB write failed even after retry. The file IS in R2
                    # and the JSON record is in memory — flush the JSON
                    # NOW so the operator can rescue this row manually
                    # (via the emitted SQL) without paying for the
                    # generation again.
                    print(f"[FAIL] DB commit (final): {e}")
                    try:
                        with open(json_out_path, "w") as fh:
                            json.dump(json_records, fh, indent=2)
                        print(f"[CHECKPOINT] flushed {len(json_records)} record(s) → {json_out_path}")
                    except OSError:
                        pass
                    failed += 1
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    continue
                created += 1
                size_kb = os.path.getsize(tmp_path) / 1024
                print(f"[OK] asset_id={asset_id} key={r2_key} ({size_kb:.0f} KB)")

            # Atomic checkpoint after every successful row. If the next
            # row's Veo poll kills the SSL again, the JSON on disk
            # already reflects everything paid for so far — no replay.
            try:
                with open(json_out_path, "w") as fh:
                    json.dump(json_records, fh, indent=2)
            except OSError as e:
                print(f"[WARN] checkpoint write failed: {e}")

            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        print("\n=== UMG library bulk done ===")
        print(f"  created: {created}")
        print(f"  skipped: {skipped}")
        print(f"  failed:  {failed}")

        if json_records:
            sql_path = "/tmp/library_umg_inserts.sql"
            with open(json_out_path, "w") as fh:
                json.dump(json_records, fh, indent=2)
            _emit_sql(json_records, sql_path, owner_tenant_id)
            print(f"\n  metadata JSON: {json_out_path}")
            print(f"  SQL inserts:   {sql_path}")
            if no_db:
                print("\n  Next step: in your terminal, run")
                print(f"    railway connect Postgres < {sql_path}")
                print(f"  to register the {len(json_records)} new asset(s) in production.")

        return 0 if failed == 0 else 2
    finally:
        # No long-lived session to close — _commit_one() opens and closes
        # one per row. SessionFactory is just a sessionmaker.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
