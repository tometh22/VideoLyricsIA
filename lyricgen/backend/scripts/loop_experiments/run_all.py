"""Sequential orchestrator: run all 4 loop experiments + write SUMMARY.md.

Usage:
    cd lyricgen/backend
    source venv/bin/activate
    # Env vars must be loaded — VERTEX_PROJECT, VERTEX_LOCATION, GOOGLE_APPLICATION_CREDENTIALS
    python scripts/loop_experiments/run_all.py
    python scripts/loop_experiments/run_all.py --job <other_job_id>
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

from common import load_job, audio_duration, ensure_out_dir  # noqa: E402


VARIANTS = [
    ("c", "baseline (Veo 8s + palindrome)", "current production behavior"),
    ("b", "seam-hidden (1 Imagen + randomized KB + xfade)", "same scene, no motion repetition"),
    ("a", "variety (4 Imagen + KB + xfade)", "fresh scene every ~55s"),
    ("d", "hybrid chorus/verse (Veo on chorus, Imagen on verse)", "Veo where it matters most"),
]


def _filesize_mb(p: Path) -> float:
    return p.stat().st_size / 1024 / 1024 if p.exists() else 0.0


def _video_duration(p: Path) -> float:
    if not p.exists():
        return 0.0
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
        capture_output=True, text=True,
    )
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job", default="8b9b19db22e9")
    p.add_argument("--skip", default="", help="comma-separated variant letters to skip (e.g. 'c,d')")
    p.add_argument("--only", default="", help="comma-separated variant letters to run (overrides --skip)")
    args = p.parse_args()

    skip = set(s.strip().lower() for s in args.skip.split(",") if s.strip())
    only = set(s.strip().lower() for s in args.only.split(",") if s.strip())

    job = load_job(args.job)
    out_dir = ensure_out_dir(job["out_dir"])
    audio_dur = audio_duration(job["audio_path"])

    print(f"\n{'='*70}")
    print(f"LOOP EXPERIMENTS — job {args.job}")
    print(f"Audio: {Path(job['audio_path']).name}  ({audio_dur:.1f}s)")
    print(f"Output dir: {out_dir}")
    print(f"{'='*70}\n")

    results = []
    overall_t0 = time.time()

    for letter, name, hint in VARIANTS:
        if only and letter not in only:
            continue
        if not only and letter in skip:
            print(f"⏭  Skipping option {letter.upper()}: {name}")
            continue

        print(f"\n{'-'*70}")
        print(f"▶ Option {letter.upper()}: {name}")
        print(f"  ({hint})")
        print(f"{'-'*70}")

        t0 = time.time()
        try:
            # Each run_option_*.py exposes main() with --job arg.
            # We import and call directly so the same Python process retains
            # any module-level caches (notably _get_genai_client).
            module_name = f"run_option_{letter}"
            mod = __import__(module_name)
            # Inject args into sys.argv for the module's main()
            old_argv = sys.argv
            sys.argv = [module_name, "--job", args.job]
            try:
                mod.main()
            finally:
                sys.argv = old_argv
            elapsed = time.time() - t0
            out_path = out_dir / f"option_{letter}_{'baseline' if letter=='c' else 'variety' if letter=='a' else 'seam_hidden' if letter=='b' else 'hybrid'}.mp4"
            results.append({
                "letter": letter, "name": name, "elapsed": elapsed,
                "out": out_path, "size_mb": _filesize_mb(out_path),
                "duration": _video_duration(out_path), "error": None,
            })
            print(f"  ✓ Option {letter.upper()} done in {elapsed:.0f}s")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ✗ Option {letter.upper()} FAILED after {elapsed:.0f}s: {e}")
            traceback.print_exc()
            results.append({
                "letter": letter, "name": name, "elapsed": elapsed,
                "out": None, "size_mb": 0.0, "duration": 0.0,
                "error": str(e),
            })

    overall_elapsed = time.time() - overall_t0

    # Write SUMMARY.md
    md_lines = [
        f"# Loop strategy comparison — job `{args.job}`",
        "",
        f"Audio: `{Path(job['audio_path']).name}` ({audio_dur:.1f}s)",
        f"Total time: {overall_elapsed/60:.1f} min",
        "",
        "| Variant | Out file | Duration | Size | Build time | Note |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        out_name = Path(r["out"]).name if r["out"] else "(failed)"
        note = r["error"] if r["error"] else "ok"
        md_lines.append(
            f"| **{r['letter'].upper()}** — {r['name']} | `{out_name}` | "
            f"{r['duration']:.1f}s | {r['size_mb']:.1f} MB | {r['elapsed']:.0f}s | {note} |"
        )
    md_lines += [
        "",
        "## How to compare",
        "",
        "Play all 4 in QuickTime (open all → Cmd+Shift+P for picture-in-picture, or split-screen).",
        "Focus on: (a) does the loop become noticeable? (b) does the bg distract from lyrics? "
        "(c) does the chorus feel like a different moment in D?",
        "",
        f"Then: `open {out_dir}`",
    ]
    summary_path = out_dir / "SUMMARY.md"
    summary_path.write_text("\n".join(md_lines))
    print(f"\n\n{'='*70}")
    print(f"DONE in {overall_elapsed/60:.1f} min. Summary: {summary_path}")
    print(f"{'='*70}\n")
    for r in results:
        status = "✓" if not r["error"] else "✗"
        out = Path(r["out"]).name if r["out"] else "(no output)"
        print(f"  {status} option_{r['letter']}: {out}  ({r['size_mb']:.1f} MB, {r['duration']:.0f}s)")
    print(f"\n  open {out_dir}")


if __name__ == "__main__":
    main()
