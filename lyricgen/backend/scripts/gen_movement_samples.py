"""One-off: generate PREMIUM demo clips (one per movement register) for the
wizard's "Movimiento" step. These are the sales card, so they are generated
with the real engine at its best:

  - Veo STANDARD model (veo-3.1-generate-001), NOT the fast tier.
  - No legibility blur (BG_BLUR_SIGMA=0) so the demos stay crisp.
  - "Foto + parallax" is produced via Imagen-4 Ultra (a premium still) + a
    clean LATERAL pan — the SAME engine path as the real foto-parallax render
    (Veo can't do clean 2.5D parallax from text).
  - MULTIPLE candidates per register for human selection — nothing is
    auto-picked, because "premium" is a taste call.

Each Veo take uses a distinct cache_namespace so the prompt-hash R2 cache does
NOT return the same clip for every take.

Usage (run from lyricgen/backend):
  ./venv/bin/python scripts/gen_movement_samples.py                # all, 3 takes
  ./venv/bin/python scripts/gen_movement_samples.py estatico       # subset
  TAKES=4 ./venv/bin/python scripts/gen_movement_samples.py        # more takes
  ./venv/bin/python scripts/gen_movement_samples.py --finalize estatico=2 animado=1
        ^ copy + 1080p-transcode the chosen take into public/movement_samples/

Candidates land in /tmp/movsamp/<code>_<n>.mp4 (review, then --finalize).
Needs .env with VERTEX_PROJECT / VERTEX_LOCATION / GOOGLE_APPLICATION_CREDENTIALS.
"""
import os
import subprocess
import sys

# Load .env into os.environ BEFORE importing pipeline (it reads VERTEX_* at import).
_ENV = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_ENV):
    for line in open(_ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

# Premium demo overrides — applied ONLY to this one-off generation (read at call
# time inside pipeline, so they don't touch the runtime defaults):
#   - Veo STANDARD for fidelity + far better "static" adherence than fast.
#   - No legibility blur → crisp, premium-looking demos.
os.environ["VEO_MODEL"] = os.environ.get("VEO_MODEL_DEMO", "veo-3.1-generate-001")
os.environ["VEO_MODEL_STATIC"] = os.environ["VEO_MODEL"]
os.environ["BG_BLUR_SIGMA"] = "0"

# Run from backend dir so the relative GOOGLE_APPLICATION_CREDENTIALS resolves.
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

import pipeline  # noqa: E402
from internal_tracking import ensure_internal_tracking_job  # noqa: E402

TMP = "/tmp/movsamp"
OUT = "../frontend/public/movement_samples"
os.makedirs(TMP, exist_ok=True)
TAKES = int(os.environ.get("TAKES", "3"))

# Keep every scene free of the urban-night cliché UMG rejected.
_NO_URBAN = " No city streets, no alleys, no urban night scenes, no roads, no traffic."

# Veo registers — the only two where the camera SHOULD move (Cinematográfico)
# or where the look is non-photoreal (Animado). The calm registers do NOT use
# Veo: it ignores "locked / no advance" ~half the time (measured 2026-05-22).
VEO_PROMPTS = {
    # Approved scene/motion — re-rendered on STANDARD for premium fidelity.
    "estandar": (
        "Slow, smooth and elegant cinematic glide over snow-capped mountain peaks "
        "at dawn, steady gentle camera movement, layered ridges and soft drifting "
        "clouds, warm golden light. Graceful and premium — smooth and unhurried, "
        "NOT fast, no dizzying motion. Cinematic, photorealistic, 4k, 16:9."
    ),
    # Living illustration: one authored subject moves; the frame does not
    # receive generic weather or ambient filler just to manufacture motion.
    "animado": (
        "A premium stylised 2D illustrated basketball court in bold flat shapes. "
        "One basketball makes a clean, seamless arc toward the hoop and repeats; "
        "the hoop, court lines, background, lighting and every other element remain "
        "perfectly still. Locked camera, no pan or zoom, no smoke, rain, fog, "
        "particles, drifting clouds or generic atmospheric motion. Elegant, artful, "
        "NOT photorealistic, hand-drawn animation look, 4k, 16:9."
    ),
}

# Calm registers — a premium Imagen-4 Ultra STILL + a deterministic, code-driven
# camera move (static / subtle drift / lateral pan). 100% reliable, never
# advances. Distinct SCENES per mode so the cards don't look alike. Prompts are
# stills with NO motion words (motion comes from Ken Burns, not the image).
KB_MODE = {"estatico": "static", "sutil": "subtle", "foto-parallax": "lateral"}
IMAGEN_PROMPTS = {
    # Estático → held frame. A serene tropical beach (distinct from the others).
    "estatico": (
        "A breathtaking premium photograph of a serene tropical beach at golden "
        "hour: tall palm trees, calm turquoise sea, soft glowing sky, gentle "
        "reflections on wet sand. Ultra-sharp, photographic, cinematic, 4k, 16:9. "
        "A perfectly still photo, no motion."
    ),
    # Sutil → gentle drift. A warm interior (distinct subject + palette).
    "sutil": (
        "A breathtaking premium photograph of a warm luxury loft interior at "
        "golden hour: soft sunlight pouring through tall windows, sheer curtains, "
        "lush plants, rich wood textures, intimate and calm. Ultra-sharp, "
        "photographic, cinematic, 4k, 16:9. A perfectly still photo, no motion."
    ),
    # Foto+parallax → lateral pan over layered depth (distinct mountain subject).
    "foto-parallax": (
        "A breathtaking premium landscape photograph at dawn: layered mountain "
        "ridges fading into morning mist, deep foreground pine branches, "
        "mid-ground forested slopes and distant snow peaks — a strong sense of "
        "depth across many layers — warm golden light. Ultra-sharp, photographic, "
        "cinematic, 4k, 16:9. A perfectly still photo, no motion."
    ),
}

VEO_PROMPTS = {k: v + _NO_URBAN for k, v in VEO_PROMPTS.items()}
IMAGEN_PROMPTS = {k: v + _NO_URBAN for k, v in IMAGEN_PROMPTS.items()}
ALL_CODES = list(VEO_PROMPTS) + list(IMAGEN_PROMPTS)


def _veo_take(code: str, prompt: str, n: int) -> str:
    out = f"{TMP}/{code}_{n}.mp4"
    tracking_job_id = ensure_internal_tracking_job(
        f"movement-premium:{code}:{n}", style=code)
    # Distinct namespace per take → distinct cache key → a fresh generation
    # (Veo is non-deterministic), instead of the prompt-hash cache returning
    # the same clip for every take.
    pipeline._generate_veo_video(
        prompt, out, job_id=tracking_job_id, movement_style=code,
        cache_namespace=f"movsamp-{code}-{n}",
        require_persistent_tracking=True,
    )
    return out


def _imagen_take(code: str, prompt: str, n: int) -> str:
    img = f"{TMP}/{code}_{n}.jpg"
    out = f"{TMP}/{code}_{n}.mp4"
    tracking_job_id = ensure_internal_tracking_job(
        f"movement-premium:{code}:{n}", style=code)
    pipeline._generate_imagen_image(
        prompt, img, job_id=tracking_job_id,
        model="imagen-4.0-ultra-generate-001")
    # 8s deterministic camera move over the still (1920x1080), per register.
    mode = KB_MODE.get(code, "lateral")
    pipeline._ken_burns_image_to_mp4(
        img, out, sample_duration=8.0,
        static=(mode == "static"), lateral=(mode == "lateral"), subtle=(mode == "subtle"),
    )
    return out


def _finalize(code: str, n: int) -> None:
    """Copy + 1080p-transcode a chosen take into public/movement_samples/."""
    src = f"{TMP}/{code}_{n}.mp4"
    if not os.path.exists(src):
        print(f"SKIP {code}: {src} not found")
        return
    dst = f"{OUT}/{code}.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-vf", "scale='min(1920,iw)':-2",  # cap width at 1080p, keep aspect
        "-c:v", "libx264", "-crf", "21", "-preset", "slow",
        "-an", "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
    ], check=True)
    size = os.path.getsize(dst) / 1024
    print(f"FINALIZED {code}: {src} -> {dst} ({size:.0f} KB)")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--finalize":
        for spec in args[1:]:
            code, _, n = spec.partition("=")
            _finalize(code, int(n or "1"))
        return

    only = set(args)
    for code in ALL_CODES:
        if only and code not in only:
            continue
        prompt = VEO_PROMPTS.get(code) or IMAGEN_PROMPTS[code]
        is_imagen = code in IMAGEN_PROMPTS
        for n in range(1, TAKES + 1):
            print(f"\n=== {code} take {n}/{TAKES} ({'imagen+lateral' if is_imagen else 'veo-standard'}) ===", flush=True)
            try:
                out = _imagen_take(code, prompt, n) if is_imagen else _veo_take(code, prompt, n)
                size = os.path.getsize(out) / 1024 if os.path.exists(out) else 0
                print(f"OK {code}_{n}: {size:.0f} KB -> {out}", flush=True)
            except Exception as e:
                print(f"FAIL {code}_{n}: {e}", flush=True)

    print(f"\nDONE. Review {TMP}/<code>_<n>.mp4, then finalize the winners, e.g.:")
    print("  ./venv/bin/python scripts/gen_movement_samples.py --finalize estatico=2 sutil=1 ...")


if __name__ == "__main__":
    main()
