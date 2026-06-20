"""Stitch real con ffmpeg: el timeline multi-escena cubre TODA la canción.

Regresión 2026-06-18: el xfade solapa clips y la cadena rinde
`Σdur − (N−1)·xfade`; sin compensar, el fondo terminaba (N−1)·xfade ANTES
que el audio (cola negra/freeze al final). El fix estira la última escena
`(N−1)·xfade` para que el total quede EXACTO en audio_dur.

Necesita ffmpeg/ffprobe reales (se saltea si no están) — el resto de la
suite de escenas corre 100% offline.
"""
import os
import shutil
import subprocess

import pytest

import scenes
from scenes import Section

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requiere ffmpeg/ffprobe",
)


def _make_clip(path, color, secs=4):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c={color}:s=320x180:r=24:d={secs}", path],
        check=True,
    )


def _probe_dur(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _loop_fn(clip, duration, job_dir, target_w=1920, target_h=1080, out_name="seg.mp4"):
    """Stand-in de _prerender_looped_bg: estira un clip a `duration` exacta."""
    out = os.path.join(job_dir, out_name)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", clip,
         "-t", f"{duration:.3f}",
         "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},setsar=1",
         "-r", "24", "-an", out],
        check=True,
    )
    return out


def _stitch(sections, audio_dur, tmp_path):
    palette = ["red", "green", "blue", "orange", "purple", "teal", "black"]
    clip_for_key = {}
    for i, k in enumerate(dict.fromkeys(s.recurrence_key for s in sections)):
        p = os.path.join(str(tmp_path), f"clip_{k}.mp4")
        _make_clip(p, palette[i % len(palette)])
        clip_for_key[k] = p
    out = scenes.stitch_timeline(
        sections, clip_for_key, audio_dur, str(tmp_path),
        target_w=320, target_h=180, loop_fn=_loop_fn,
    )
    return out


def test_stitch_three_scenes_covers_full_song(tmp_path):
    sections = [
        Section(type="coro", start=0, end=54, energy=0.85, recurrence_key="coro_1"),
        Section(type="puente", start=54, end=62, energy=0.4, recurrence_key="puente"),
        Section(type="coro", start=62, end=79, energy=0.85, recurrence_key="coro_1"),
    ]
    out = _stitch(sections, 79.0, tmp_path)
    assert os.path.exists(out)
    # EXACTO: el déficit de xfade quedó compensado en la última escena.
    assert abs(_probe_dur(out) - 79.0) < 0.6


def test_stitch_six_scenes_exact_duration(tmp_path):
    # Reusa coro_1 (rima estructural) e incluye un puente (fadeblack).
    sections = [
        Section(type="intro", start=0, end=18, energy=0.25, recurrence_key="intro"),
        Section(type="verso", start=18, end=38, energy=0.5, recurrence_key="verso_1"),
        Section(type="coro", start=38, end=58, energy=0.85, recurrence_key="coro_1"),
        Section(type="verso", start=58, end=78, energy=0.5, recurrence_key="verso_2"),
        Section(type="puente", start=78, end=92, energy=0.4, recurrence_key="puente"),
        Section(type="coro", start=92, end=120, energy=0.85, recurrence_key="coro_1"),
    ]
    out = _stitch(sections, 120.0, tmp_path)
    assert abs(_probe_dur(out) - 120.0) < 0.6


def test_stitch_single_scene_no_xfade(tmp_path):
    sections = [Section(type="verso", start=0, end=40, energy=0.5, recurrence_key="scene_0")]
    out = _stitch(sections, 40.0, tmp_path)
    assert abs(_probe_dur(out) - 40.0) < 0.6


def test_stitch_covers_instrumental_outro(tmp_path):
    """Regresión 2026-06-19 (job e5bbc6a63861): las secciones cubren hasta la
    última línea cantada (Σdur=80) pero la canción dura 86s — 6s de cola
    instrumental. El timeline debe estirarse a los 86s (no 80), si no el fondo
    se congela al final Y el mismatch >3s rebota el fast-path libass a moviepy."""
    sections = [
        Section(type="intro", start=0, end=16, energy=0.25, recurrence_key="intro"),
        Section(type="verso", start=16, end=42, energy=0.5, recurrence_key="verso_1"),
        Section(type="coro", start=42, end=80, energy=0.85, recurrence_key="coro_1"),
    ]
    # Σdur = 80, pero el audio dura 86 (outro instrumental de 6s).
    out = _stitch(sections, 86.0, tmp_path)
    # El fondo cubre TODA la canción dentro de la tolerancia de la guarda (3s).
    assert abs(_probe_dur(out) - 86.0) < 0.6


def test_extract_thumbnail(tmp_path):
    clip = os.path.join(str(tmp_path), "clip.mp4")
    _make_clip(clip, "blue", secs=4)
    out = os.path.join(str(tmp_path), "thumb.jpg")
    res = scenes.extract_thumbnail(clip, out, at_seconds=1.0, width=160)
    assert res == out and os.path.exists(out) and os.path.getsize(out) > 0
    # ancho respetado (alto par por scale=-2)
    w = int(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "default=nw=1:nk=1", out],
        capture_output=True, text=True).stdout.strip())
    assert w == 160


def test_extract_thumbnail_missing_clip_returns_none(tmp_path):
    assert scenes.extract_thumbnail(os.path.join(str(tmp_path), "nope.mp4"),
                                    os.path.join(str(tmp_path), "t.jpg")) is None
