"""Shared utilities for the 4 loop-strategy experiments.

This module is INTENTIONALLY isolated under scripts/loop_experiments/ — none
of these helpers run in production. The 4 run_option_*.py scripts each
produce a custom 214s background mp4 with a different loop strategy, then
hand it off to `compose_with_lyrics()` here, which reuses the production
`generate_lyric_video()` for the actual lyrics/text/audio composition.

Why pass an exact-duration background:
  pipeline._get_background_clip_from_path() palindromes anything shorter
  than the audio. By making our experimental backgrounds match the audio
  duration exactly, the production compositor does NOT loop them further —
  the loop strategy we built is what the viewer sees.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))

DATASET = BACKEND / "benchmark" / "dataset"
OUT_ROOT = BACKEND / "benchmark" / "loop_experiments"


def load_job(job_id: str) -> dict:
    """Load the bench dataset artifacts for a single job. Raises if anything missing."""
    d = DATASET / job_id
    if not d.exists():
        raise FileNotFoundError(f"Bench dataset dir not found: {d}")
    audio = d / "audio.wav"
    if not audio.exists():
        audio = next(d.glob("audio.*"), None)
        if audio is None:
            raise FileNotFoundError(f"No audio.* file in {d}")
    gt_path = d / "ground_truth.json"
    if not gt_path.exists():
        raise FileNotFoundError(f"No ground_truth.json in {d}")
    meta_path = d / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No metadata.json in {d}")
    return {
        "job_id": job_id,
        "audio_path": str(audio),
        "segments": json.loads(gt_path.read_text()),
        "metadata": json.loads(meta_path.read_text()),
        "out_dir": OUT_ROOT / job_id,
    }


def audio_duration(audio_path: str) -> float:
    """Get audio duration via ffprobe (avoids importing moviepy here)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         audio_path],
        capture_output=True, text=True,
    )
    return float((r.stdout or "0").strip() or 0.0)


def ensure_out_dir(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def compose_with_lyrics(
    audio_path: str,
    segments: list[dict],
    bg_path: str,
    out_dir: str,
    artist: str,
    song_title: str,
    out_name: str,
) -> str:
    """Compose final lyric video using production generate_lyric_video.

    bg_path MUST have duration ≈ audio duration to avoid the production
    compositor re-palindroming it on top of our custom loop strategy.

    Returns the path to the rendered mp4.

    Note on isolation: generate_lyric_video writes to <job_dir>/lyric_video.mp4
    with a hardcoded filename. When 2+ option scripts run in parallel against
    the same out_dir, they race on that fixed name. We force a per-call
    subdir for the compose step, then move the result to the experiment-named
    file in out_dir, to make parallel runs safe.
    """
    from pipeline import generate_lyric_video

    import tempfile
    compose_subdir = Path(out_dir) / f".compose_{Path(out_name).stem}_{os.getpid()}"
    compose_subdir.mkdir(parents=True, exist_ok=True)

    print(f"  [compose] audio={Path(audio_path).name} bg={Path(bg_path).name} -> {out_name}")
    t0 = time.time()
    try:
        video_path, font, bg_source = generate_lyric_video(
            mp3_path=audio_path,
            segments=segments,
            style="auto",  # not used when bg is supplied
            job_dir=str(compose_subdir),
            artist=artist,
            bg_image_path=bg_path,  # mp4 is accepted (pipeline.py:6135 only branches on .jpg/.png)
            song_title=song_title,
            text_case="upper",
            font_scale=1.0,
            lyric_transition="cut",
            text_motion="none",
            text_contrast="medium",
        )
        final = Path(out_dir) / out_name
        if Path(video_path).exists():
            # Move atomically (same filesystem) — overwrites any prior render.
            Path(video_path).replace(final)
        else:
            raise RuntimeError(f"generate_lyric_video reported {video_path} but file not found")
    finally:
        # Sweep the temp compose subdir — moviepy leaves TEMP_MPY_wvf_snd.mp4
        # and friends behind.
        try:
            for leftover in compose_subdir.iterdir():
                try:
                    leftover.unlink()
                except OSError:
                    pass
            compose_subdir.rmdir()
        except OSError:
            pass

    elapsed = time.time() - t0
    size_mb = final.stat().st_size / 1024 / 1024
    print(f"  [compose] {out_name}: {size_mb:.1f} MB in {elapsed:.1f}s (font={Path(font).name})")
    return str(final)


def ffmpeg_xfade_chain(input_paths: list[str], xfade_duration: float,
                       out_path: str, target_w: int = 1920, target_h: int = 1080,
                       fps: int = 24) -> str:
    """Concat N input videos with crossfade transitions between each.

    Each input is scaled+cropped to target. xfade overlaps the last
    xfade_duration seconds of clip i with the first of clip i+1.

    Total output duration = sum(input_durations) - (N-1) * xfade_duration.
    Caller must size input clips accordingly to hit the audio duration.

    Uses ffmpeg's `xfade` filter (transition=fade). Single subprocess call.
    """
    if not input_paths:
        raise ValueError("ffmpeg_xfade_chain needs at least 1 input")
    if len(input_paths) == 1:
        # Just scale/crop and copy
        cmd = [
            "ffmpeg", "-y", "-i", input_paths[0],
            "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                   f"crop={target_w}:{target_h},setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg single-input copy failed: {r.stderr[-300:]}")
        return out_path

    # Probe durations of each input
    durations = []
    for p in input_paths:
        d = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", p],
            capture_output=True, text=True,
        )
        durations.append(float((d.stdout or "0").strip()))

    # Build filter graph:
    # [0:v]scale,crop,setpts[v0];[1:v]scale,crop,setpts[v1];...
    # [v0][v1]xfade=transition=fade:duration=X:offset=Y0[vx01];
    # [vx01][v2]xfade=...:offset=Y1[vx02]; ...
    prepped = []
    parts = []
    for i, _ in enumerate(input_paths):
        parts.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setpts=PTS-STARTPTS,fps={fps}[v{i}]"
        )
        prepped.append(f"v{i}")

    # Offset for xfade is cumulative: offset_k = sum(dur[0..k]) - (k+1)*xfade
    chain = []
    cum_offset = 0.0
    prev_label = prepped[0]
    for i in range(1, len(prepped)):
        cum_offset += durations[i - 1] - xfade_duration
        out_label = f"vx{i:02d}"
        chain.append(
            f"[{prev_label}][{prepped[i]}]xfade=transition=fade:"
            f"duration={xfade_duration}:offset={cum_offset:.3f}[{out_label}]"
        )
        prev_label = out_label

    filter_complex = ";".join(parts + chain)

    cmd = ["ffmpeg", "-y"]
    for p in input_paths:
        cmd += ["-i", p]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg xfade chain failed (n_inputs={len(input_paths)}, "
            f"durations={durations}, xfade={xfade_duration}): {r.stderr[-500:]}"
        )
    return out_path


def trim_or_pad_to_duration(in_path: str, out_path: str, target_dur: float,
                            target_w: int = 1920, target_h: int = 1080,
                            fps: int = 24) -> str:
    """Trim or freeze-extend a video to exactly target_dur seconds."""
    cur = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", in_path],
        capture_output=True, text=True,
    )
    cur_dur = float((cur.stdout or "0").strip())
    if cur_dur >= target_dur:
        # Trim
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-t", f"{target_dur:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
            out_path,
        ]
    else:
        # Extend by repeating last frame (tpad)
        extra = target_dur - cur_dur
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
            "-t", f"{target_dur:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", "-r", str(fps),
            out_path,
        ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg trim/pad failed: {r.stderr[-300:]}")
    return out_path
