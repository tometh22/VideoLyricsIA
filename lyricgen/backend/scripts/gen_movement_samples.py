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

# Distinct scene + explicit camera language per movement.
PROMPTS = {
    "estatico": (
        "Locked static tripod shot, the camera does NOT move at all. A neon-lit "
        "rainy city street at night, puddles mirroring magenta and cyan light, "
        "steam drifting up, rain falling. Only the rain, steam and reflections "
        "move; the frame is perfectly still. Cinematic, photorealistic, 4k, 16:9."
    ),
    "sutil": (
        "Barely-moving shot, an almost imperceptible slow drift. A warm sunlit "
        "room at golden hour, gauze curtains swaying gently, dust motes floating "
        "in the light. Intimate, calm, soft focus. Cinematic, photorealistic, 4k, 16:9."
    ),
    "estandar": (
        "Slow cinematic drone push-in over a stormy desert highway at dusk, "
        "sweeping forward camera movement, lightning fracturing distant clouds, "
        "asphalt reflecting the dying light. Dramatic, cinematic, photorealistic, 4k, 16:9."
    ),
    "foto-parallax": (
        "Subtle parallax over a misty mountain valley at dawn, a slow horizontal "
        "camera glide that reveals depth between foreground pine trees and distant "
        "layered peaks. Photographic, contemplative. Cinematic, photorealistic, 4k, 16:9."
    ),
    "animado": (
        "Stylised 2D animated illustration, flat bold shapes and vivid colors, a "
        "vibrant abstract cityscape with deliberate cartoon-like motion. NOT "
        "photorealistic, hand-drawn animation look. 4k, 16:9."
    ),
}

for code, prompt in PROMPTS.items():
    tmp = f"/tmp/movsamp/{code}.mp4"
    print(f"\n=== generating {code} ===", flush=True)
    try:
        pipeline._generate_veo_video(prompt, tmp, movement_style=code)
        size = os.path.getsize(tmp) / 1024 if os.path.exists(tmp) else 0
        print(f"OK {code}: {size:.0f} KB -> {tmp}", flush=True)
    except Exception as e:
        print(f"FAIL {code}: {e}", flush=True)

print("\nDONE. Review /tmp/movsamp/*.mp4, then move into", OUT)
