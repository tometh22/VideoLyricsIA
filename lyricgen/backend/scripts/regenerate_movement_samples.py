"""Generate the 4 movement-style sample MP4s for the upload-zone gallery.

Calls Veo Fast directly with canonical prompts (one per style) and saves
the results to lyricgen/frontend/public/movement_samples/. Each sample
is a real platform output, not a placeholder — the operator sees exactly
what each style produces in the actual rendered video.

Cost: 4 × Veo Fast (8 s @ $0.10/s) = ~$3.20 per full run.
Time: ~2–4 minutes (Veo polling).

Run from lyricgen/backend with the venv active and Vertex credentials set:

    GOOGLE_APPLICATION_CREDENTIALS=$PWD/vertex_credentials.json \\
        python scripts/regenerate_movement_samples.py
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()

from pipeline import _generate_veo_video


# Hand-picked prompts that demonstrate each style cleanly. Each is short,
# specific, and avoids the failure modes Veo struggles with (no people,
# no text). The second positional arg to _generate_veo_video is the
# output path; we route Veo's raw 8-second 1280x720 output through ffmpeg
# afterwards to get the 720x404 5-second loops the gallery actually uses.
SAMPLES = [
    {
        "style": "sutil",
        "prompt": (
            "Tropical palm trees silhouetted against a purple-pink sunset sky, "
            "gentle sway in the breeze, slow ambient drift, calm ocean horizon "
            "in the distance. Easy to loop seamlessly. Photorealistic 4k."
        ),
    },
    {
        "style": "estandar",
        "prompt": (
            "Slow tracking shot through neon-lit rain-slicked city streets, "
            "deep blue and red reflections in the puddles, smoke rising past "
            "streetlamps, dramatic moody lighting. Photorealistic cinematic 4k."
        ),
    },
    {
        "style": "foto-parallax",
        "prompt": (
            "Wide misty mountain valley at dawn, photographic still composition "
            "with subtle camera moves and depth-of-field shifts, slow zoom on a "
            "lone tree. No moving subjects. 4k."
        ),
    },
    {
        "style": "animado",
        "prompt": (
            "Stylised 2D animated illustration of a basketball spinning slowly "
            "above an empty court, flat shapes, deliberate cartoon-like motion, "
            "saturated colors. NOT photorealistic."
        ),
    },
]

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "frontend", "public", "movement_samples",
)
os.makedirs(OUT_DIR, exist_ok=True)


def _post_process(raw_path: str, final_path: str) -> bool:
    """Crop / scale Veo's 1280x720 8 s clip into a 720x404 5 s loop suitable
    for an in-browser <video> thumbnail. Returns True on success."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_path,
             "-t", "5", "-an",
             "-vf", "scale=720:404,fps=24",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart",
             "-loglevel", "error", final_path],
            check=True, timeout=60,
        )
        return os.path.exists(final_path) and os.path.getsize(final_path) > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  ffmpeg post-process failed: {e}")
        return False


def main():
    import time as _time
    fails = 0
    for entry in SAMPLES:
        style = entry["style"]
        prompt = entry["prompt"]
        print(f"\n=== Generating sample for {style!r} ===")
        print(f"  prompt: {prompt[:90]}…")
        raw = f"/tmp/veo_sample_{style}.mp4"
        final = os.path.join(OUT_DIR, f"{style}.mp4")
        try:
            t0 = _time.time()
            _generate_veo_video(prompt, raw, job_id=f"sample-{style}",
                                cache_namespace=f"sample|{style}",
                                movement_style=style)
            elapsed = _time.time() - t0
            print(f"  Veo OK in {elapsed:.1f} s; raw={os.path.getsize(raw)/1e6:.1f} MB")
            if _post_process(raw, final):
                print(f"  → {final} ({os.path.getsize(final)/1e6:.2f} MB)")
            else:
                # Fallback: copy raw as-is so we still get a sample.
                shutil.copy(raw, final)
                print(f"  → {final} (raw, no post-process)")
            try:
                os.unlink(raw)
            except OSError:
                pass
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            fails += 1
    print(f"\n=== Done. {len(SAMPLES) - fails} OK, {fails} failed. ===")


if __name__ == "__main__":
    main()
