"""QA suite orchestrator.

Runs the post-PR-G transcription pipeline serially across the golden set,
captures per-song artifacts (segments + logs + telemetry), and invokes the
analyzer to produce a markdown report with PASS/WARN/FAIL verdict.

Usage:
  python scripts/qa/run_qa_suite.py --mode quick
  python scripts/qa/run_qa_suite.py --mode full --delay-s 30
  python scripts/qa/run_qa_suite.py --mode full --dry-run
  python scripts/qa/run_qa_suite.py --only los_abuelos_cosas_mias,spotdown_desciende
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))                  # backend/
sys.path.insert(0, str(HERE.parent))               # backend/scripts/

from qa._loader import load_golden_set, filter_songs, resolve_expected  # noqa: E402
from qa import analyze_run as _analyze                                   # noqa: E402


# Default artifact root (per-project under ~/.gstack/).
_DEFAULT_OUT_ROOT = Path.home() / ".gstack" / "projects" / "VideoLyricsIA" / "qa-runs"


def _now_ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(BACKEND), stderr=subprocess.DEVNULL)
        return out.decode().strip()[:12]
    except Exception:
        return "unknown"


def _env_flag(name: str) -> str:
    return os.environ.get(name, "")


def _snapshot_env() -> dict:
    return {
        k: _env_flag(k) for k in [
            "VOCAL_SEP_ENABLED", "FORCED_ALIGNER_ENABLED", "WHISPERX_ENABLED",
            "LRCLIB_VERIFY_ALWAYS", "BEAT_SNAP_ENABLED",
            "LYRIC_LEAD_IN_MS", "WHISPERX_MAX_LINE_S", "BEAT_SNAP_WINDOW_MS",
            "DEMUCS_VARIANT",
        ]
    }


class _LogCapture:
    """Attach a FileHandler to the root logger + redirect stdout/stderr to
    capture everything a song's run produces. Context-manager-safe; restores
    on exit so consecutive songs don't bleed into each other."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.root_logger = logging.getLogger()
        self.handler: Optional[logging.FileHandler] = None
        self.prev_stdout = None
        self.prev_stderr = None
        self.file = None

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.log_path, "w", buffering=1)
        # File handler at INFO; matches the prod handler level so we see
        # [VOCALSEP] / [FORCED] / [WHISPERX] markers.
        self.handler = logging.FileHandler(self.log_path, mode="a")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        self.root_logger.addHandler(self.handler)
        prev_level = self.root_logger.level
        if prev_level == 0 or prev_level > logging.INFO:
            self._restore_level = prev_level
            self.root_logger.setLevel(logging.INFO)
        else:
            self._restore_level = None
        # stdout/stderr go to the same file too (some scripts print directly).
        self.prev_stdout, self.prev_stderr = sys.stdout, sys.stderr
        # Tee so the user still sees progress in console:
        class _Tee:
            def __init__(self, *targets): self.targets = targets
            def write(self, s):
                for t in self.targets:
                    try: t.write(s)
                    except Exception: pass
            def flush(self):
                for t in self.targets:
                    try: t.flush()
                    except Exception: pass
        sys.stdout = _Tee(self.prev_stdout, self.file)
        sys.stderr = _Tee(self.prev_stderr, self.file)
        return self

    def __exit__(self, *exc):
        if self.handler:
            self.root_logger.removeHandler(self.handler)
            try: self.handler.close()
            except Exception: pass
        if getattr(self, "_restore_level", None) is not None:
            self.root_logger.setLevel(self._restore_level)
        sys.stdout, sys.stderr = self.prev_stdout, self.prev_stderr
        if self.file:
            try: self.file.close()
            except Exception: pass
        return False


def run_one(song: dict, run_dir: Path) -> dict:
    """Execute the pipeline for one song, capturing artifacts. Returns the
    `run` dict that goes into `runs/<id>.json`."""
    from scripts.pipeline_runner import transcribe_local_via_main

    song_id = song["id"]
    audio_path = song["path"]
    runs_subdir = run_dir / "runs"
    runs_subdir.mkdir(parents=True, exist_ok=True)
    json_path = runs_subdir / f"{song_id}.json"
    log_path = runs_subdir / f"{song_id}.log"

    if not Path(audio_path).exists():
        rec = {
            "segments": [], "source": None, "recovery_source": None,
            "reference_lyrics": "",
            "meta": {"audio_path": audio_path, "error": "file_missing"},
            "telemetry": {"elapsed_seconds": 0},
        }
        json_path.write_text(json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        log_path.write_text("file_missing\n")
        return rec

    with _LogCapture(log_path):
        print(f"\n=== QA: {song_id} === {audio_path}")
        try:
            result = transcribe_local_via_main(
                audio_path=audio_path,
                artist=song.get("artist", ""),
                title=song.get("title", ""),
                language=song.get("language", "es"),
                job_id=f"qa-{song_id}",
            )
        except Exception as e:
            import traceback
            result = {
                "segments": [], "source": None, "recovery_source": None,
                "reference_lyrics": "",
                "meta": {"audio_path": audio_path, "error": repr(e),
                         "traceback": traceback.format_exc()},
                "telemetry": {"elapsed_seconds": 0},
            }
            print(f"!!! exception: {e}")

    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elapsed = (result.get("telemetry") or {}).get("elapsed_seconds")
    print(f"=== DONE {song_id} :: timing_source={result.get('source')!r} :: elapsed={elapsed:.0f}s :: log={log_path}",
          file=sys.stderr)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="World-class QA suite for the lyric transcription pipeline.")
    p.add_argument("--mode", choices=["quick", "full"], default="full",
                   help="quick = ~4 songs, no delays, ~10min. full = ~7 songs, 30s delays, ~45min.")
    p.add_argument("--delay-s", type=int, default=None,
                   help="seconds to sleep between songs (default: 30 for full, 0 for quick)")
    p.add_argument("--only", default="", help="comma-separated song ids — runs ONLY these (overrides --mode)")
    p.add_argument("--skip", default="", help="comma-separated song ids to skip")
    p.add_argument("--dry-run", action="store_true", help="validate YAML + audio existence, no Replicate")
    p.add_argument("--out-dir", default=None, help="override artifact root (default ~/.gstack/projects/VideoLyricsIA/qa-runs/<ts>/)")
    p.add_argument("--golden-set", default=str(HERE / "golden_set.yaml"), help="path to golden_set.yaml")
    args = p.parse_args()

    data = load_golden_set(args.golden_set)
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    skip = [s.strip() for s in args.skip.split(",") if s.strip()]
    songs = filter_songs(data, mode=args.mode, only=only or None, skip=skip or None)

    if not songs:
        print("ERROR: no songs selected (check --mode/--only/--skip).", file=sys.stderr)
        return 2

    # Resolve output dir.
    if args.out_dir:
        run_dir = Path(args.out_dir).resolve()
    else:
        run_dir = _DEFAULT_OUT_ROOT / _now_ts()
    run_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the YAML used.
    shutil.copy(args.golden_set, run_dir / "golden_set.yaml")

    # Dry run: validate file existence and print plan.
    print(f"QA suite mode={args.mode} — {len(songs)} song(s)")
    print(f"Artifacts → {run_dir}")
    print()
    plan_ok = True
    for s in songs:
        present = Path(s["path"]).exists()
        marker = "✓" if present else "✗ MISSING"
        print(f"  [{marker}] {s['id']:34s}  {s['path']}")
        if not present:
            plan_ok = False
    if args.dry_run:
        print()
        print("DRY-RUN done.", "all audio files present." if plan_ok else "some files missing.")
        return 0 if plan_ok else 2
    if not plan_ok:
        print("\nERROR: some audio files missing — aborting (use --skip to bypass).", file=sys.stderr)
        return 2

    # Default delays: 30s for full, 0 for quick.
    delay = args.delay_s if args.delay_s is not None else (30 if args.mode == "full" else 0)

    # Run.
    started_at = dt.datetime.utcnow().isoformat() + "Z"
    elapseds: list[float] = []
    timing_sources: dict[str, int] = {}
    print()
    for i, s in enumerate(songs):
        t0 = time.time()
        result = run_one(s, run_dir)
        dur = time.time() - t0
        elapseds.append(dur)
        ts = result.get("source")
        if ts:
            timing_sources[ts] = timing_sources.get(ts, 0) + 1
        # Inter-song delay (skip on last).
        if delay > 0 and i < len(songs) - 1:
            print(f"... sleeping {delay}s before next song ({i+2}/{len(songs)})", file=sys.stderr)
            time.sleep(delay)

    finished_at = dt.datetime.utcnow().isoformat() + "Z"

    # meta.json
    meta = {
        "mode": args.mode,
        "n_songs": len(songs),
        "started_at": started_at,
        "finished_at": finished_at,
        "git_sha": _git_sha(),
        "env_flags": _snapshot_env(),
        "suite_version": "1.0",
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # summary.json (pre-analysis aggregates).
    summary = {
        "mode": args.mode,
        "n_songs": len(songs),
        "total_elapsed_s": round(sum(elapseds), 1),
        "p50_elapsed_s": round(statistics.median(elapseds), 1) if elapseds else None,
        "p95_elapsed_s": round(_analyze._p95(elapseds), 1) if len(elapseds) >= 2 else None,
        "timing_source_distribution": timing_sources,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    # Analyze.
    exit_code, _report_path = _analyze.analyze(str(run_dir))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
