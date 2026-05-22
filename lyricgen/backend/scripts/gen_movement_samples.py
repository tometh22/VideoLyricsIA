"""One-off: generate 5 REAL Veo clips (one per movement style) to use as the
wizard's movement examples. Each prompt = a distinct attractive scene + that
movement's camera language, so the cards genuinely show the difference.

Run from lyricgen/backend:  ./venv/bin/python scripts/gen_movement_samples.py
Needs .env with VERTEX_PROJECT / VERTEX_LOCATION / GOOGLE_APPLICATION_CREDENTIALS.
"""
import os
import sys

# Load .env into os.environ BEFORE importing pipeline (it reads VERTEX_* at import).
_ENV = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_ENV):
    for line in open(_ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

# Run from backend dir so the relative GOOGLE_APPLICATION_CREDENTIALS resolves.
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

import pipeline  # noqa: E402

OUT = "../frontend/public/movement_samples"
os.makedirs("/tmp/movsamp", exist_ok=True)

# Premium, DIVERSE, non-biased scenes (no streets / alleys / urban-night) with
# SMOOTH motion (UMG criticised fast/dizzying moves). Each in a different
# register so the examples showcase variety, not the urban cliché.
PROMPTS = {
    "estatico": (
        "Locked static shot, the camera does NOT move at all: no pan, no zoom, "
        "no push-in, NO fly-through, the camera does NOT advance or travel "
        "forward into the scene. A flat head-on view of a deep-space nebula: "
        "vast cosmic clouds in violet, magenta and teal slowly swirling in "
        "place, distant stars gently twinkling. Only the gas clouds and stars "
        "shimmer; the camera frame is perfectly, absolutely still as if it were "
        "a photograph that breathes. Serene, premium, cinematic, 4k, 16:9."
    ),
    "sutil": (
        "Locked static camera on a tripod, the camera itself does NOT move at "
        "all. A warm luxury loft interior at golden hour. ALL the motion comes "
        "from elements INSIDE the scene, not the camera: sheer white curtains "
        "billowing gently in the breeze by a large window, dust motes drifting "
        "in the warm sunlight, plant leaves trembling softly. The frame is "
        "perfectly still while life moves within it. Calm, premium, intimate. "
        "Cinematic, photorealistic, 4k, 16:9."
    ),
    "estandar": (
        "Slow, smooth and elegant cinematic glide over snow-capped mountain "
        "peaks at dawn, steady gentle forward camera movement, layered ridges "
        "and soft drifting clouds, warm golden light. Graceful and premium — "
        "smooth and unhurried, NOT fast, no dizzying motion. Cinematic, "
        "photorealistic, 4k, 16:9."
    ),
    "foto-parallax": (
        "A single still PHOTOGRAPH brought to life with a subtle 2.5D parallax "
        "effect: a slow gentle LATERAL camera shift (sideways only, NOT forward, "
        "no push-in, no advance) that reveals depth between foreground pine "
        "branches and distant layered mountain ridges at golden hour. The scene "
        "is otherwise frozen like a real photo: NO flowing water, no moving "
        "waves, no wind, nothing animates except the parallax depth. "
        "Photographic, calm, premium. Cinematic, photorealistic, 4k, 16:9."
    ),
    "animado": (
        "Stylised 2D animated illustration, vivid flat colorful shapes, a dreamy "
        "abstract composition of flowing gradients and gentle hand-drawn motion. "
        "NOT photorealistic, artful animation look, smooth and calm. 4k, 16:9."
    ),
}
# Guard rail: keep every example free of the urban-night cliché UMG rejected.
PROMPTS = {k: v + " No city streets, no alleys, no urban night scenes, no roads, no traffic." for k, v in PROMPTS.items()}

# Some examples must render through a DIFFERENT movement path than their card
# code. "sutil" needs a truly locked camera (motion only from in-scene elements
# like a billowing curtain), so it routes through the "estatico" render path,
# which injects the no-pan/zoom/dolly/no-camera-movement negatives.
RENDER_MOVE = {"sutil": "estatico"}

# Optionally regenerate only a subset:  python gen_movement_samples.py sutil foto-parallax
ONLY = set(sys.argv[1:])

for code, prompt in PROMPTS.items():
    if ONLY and code not in ONLY:
        continue
    tmp = f"/tmp/movsamp/{code}.mp4"
    move = RENDER_MOVE.get(code, code)
    print(f"\n=== generating {code} (render_move={move}) ===", flush=True)
    try:
        pipeline._generate_veo_video(prompt, tmp, movement_style=move)
        size = os.path.getsize(tmp) / 1024 if os.path.exists(tmp) else 0
        print(f"OK {code}: {size:.0f} KB -> {tmp}", flush=True)
    except Exception as e:
        print(f"FAIL {code}: {e}", flush=True)

print("\nDONE. Review /tmp/movsamp/*.mp4, then move into", OUT)
