"""One-off: generate cinematic background stills with the REAL GenLy engine
(Google Imagen-4) for the landing "looks" gallery. Outputs land directly in
the landing worktree's public/ so the marketing build can use them.

Imagen-4 standard ~ $0.04/image → 6 images ≈ $0.25. Reads VERTEX_* and
GOOGLE_APPLICATION_CREDENTIALS from backend/.env (already present).

    cd lyricgen/backend && ./venv/bin/python scripts/gen_landing_bgs.py
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

from pipeline import _generate_imagen_image
from internal_tracking import ensure_internal_tracking_job

OUT = Path("/Users/tomi/VideoLyricsIA-landing/lyricgen/frontend/public/samples/looks")
OUT.mkdir(parents=True, exist_ok=True)

# Varied cinematic vibes matching the landing's lyric lines. The function
# appends "no text/logos" automatically, so these are pure scene prompts.
PROMPTS = [
    ("stage", "Empty concert stage bathed in dramatic purple and cyan spotlights, haze and volumetric light beams, cinematic, anticipatory, premium music venue atmosphere"),
    ("vinyl", "Close-up of a spinning vinyl record under moody studio light, soft bokeh, warm amber and violet tones, shallow depth of field, cinematic and intimate"),
    ("retro", "Retro 80s synthwave grid landscape at dusk, neon magenta and cyan horizon, glowing sun, cinematic nostalgic, clean and stylish"),
    ("rain", "Rain falling against a window at night with blurred neon city lights behind, violet and blue bokeh, melancholic cinematic mood, intimate"),
    ("desert", "Vast desert dunes under a deep twilight sky, gradient of indigo to warm rose, lone cinematic atmosphere, minimal and epic"),
    ("studio", "Modern dark music studio with glowing equipment LEDs, soft purple and teal rim light, cinematic, professional, premium ambience"),
    ("aurora", "Northern lights aurora over a still dark lake, reflections, teal and violet glow, serene cinematic, vast and dreamy"),
    ("smoke", "Colorful neon smoke swirling in darkness, magenta cyan and violet, abstract cinematic, elegant and moody"),
]

def main():
    ok = []
    for name, prompt in PROMPTS:
        out = OUT / f"look-{name}.png"
        try:
            print(f"[gen] {name} ...", flush=True)
            tracking_job_id = ensure_internal_tracking_job(
                f"landing-look:{name}", style="image")
            _generate_imagen_image(prompt, str(out), job_id=tracking_job_id,
                                   model="imagen-4.0-generate-001")
            size = out.stat().st_size if out.exists() else 0
            print(f"[ok]  {name} -> {out} ({size} bytes)", flush=True)
            if size > 0:
                ok.append(name)
        except Exception as e:
            print(f"[err] {name}: {e}", flush=True)
    print(f"DONE: {len(ok)}/{len(PROMPTS)} -> {sorted(ok)}")

if __name__ == "__main__":
    raise SystemExit(0 if main() is None else 0)
