"""Foto fija + efectos — the OOM-safe photo background path.

Incident 2026-06-02 ("Rata Blanca"): the photo background rendered a
full-duration Ken Burns pan via moviepy (~7,800 frames animated one-by-one in
Python), which OOM-SIGKILLed the worker on long songs and left the job stuck
in "processing" forever. The fix removes the camera pan: the photo background
is now a STATIC image rendered by ffmpeg (C-level, bounded memory — impossible
to OOM), and the video's life/motion comes from the composable effect overlay
(snow/rain/stars/bokeh/light plus the expanded atmospheric catalogue) that
screen-blends on top.

These tests prove, with REAL ffmpeg, that:
  1. the static render produces a valid full-duration video, and
  2. every effect asset exists and screen-blends onto it without failing.
"""

import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

import fx_compositor as fx
from pipeline import _static_image_to_mp4

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not installed",
)


def _make_image(path, w=1280, h=720):
    # A smooth gradient compresses like a real AI photo (low spatial entropy),
    # unlike random noise which would balloon every keyframe. Representative of
    # what Imagen actually returns.
    xs = np.linspace(0, 255, w, dtype="uint8")
    grad = np.tile(xs, (h, 1))
    rgb = np.stack([grad, np.roll(grad, 137, axis=1), np.flip(grad, axis=1)], axis=2)
    Image.fromarray(rgb.astype("uint8")).save(path)


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True,
    )
    fields = dict(
        line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
    )
    return int(fields["width"]), int(fields["height"]), float(fields["duration"])


def test_static_image_render_is_valid_and_full_duration(tmp_path):
    """_static_image_to_mp4 renders a still as a full-duration video at the
    target dims — the cheap, OOM-safe replacement for the moviepy Ken Burns."""
    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg.mp4"
    _static_image_to_mp4(str(img), str(bg), duration=3.0)
    assert bg.exists() and bg.stat().st_size > 0
    w, h, dur = _probe(bg)
    assert (w, h) == (1920, 1080)          # RenderSpec.youtube_default()
    assert 2.5 < dur < 3.7                 # ~3s, encoder rounding tolerance


def test_static_render_handles_long_duration_cheaply(tmp_path):
    """A 5-min song's worth of static background must render fast and small —
    this is the case that OOM-killed the moviepy path."""
    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg_long.mp4"
    # 150s stands in for the 5-min "Rata Blanca" case — the render mechanism is
    # identical regardless of length, so we keep CI fast. (The moviepy path
    # OOMed because it animated every frame in Python; ffmpeg streams.)
    _static_image_to_mp4(str(img), str(bg), duration=150.0)
    w, h, dur = _probe(bg)
    assert (w, h) == (1920, 1080)
    assert dur > 145
    # An identical-frame stream compresses tiny (proves it isn't materialising
    # thousands of distinct frames the way the moviepy pan did).
    assert bg.stat().st_size < 15 * 1024 * 1024


@pytest.mark.parametrize("effect", list(fx.EFFECTS))
def test_each_effect_asset_exists_and_composites(tmp_path, effect):
    """Every effect executes through the production graph with real ffmpeg."""
    p = fx.effect_path(effect)
    assert p and os.path.exists(p), f"effect asset missing for {effect!r}"

    img = tmp_path / "bg.jpg"
    _make_image(img)
    bg = tmp_path / "bg.mp4"
    _static_image_to_mp4(str(img), str(bg), duration=2.0)

    comp = tmp_path / f"comp_{effect}.mp4"
    rhythm = (
        fx.EffectRhythm(120.0, (.08, .58, 1.08, 1.58), (1.0, .55, .8, .6))
        if fx.is_reactive_effect(effect) else None
    )
    graph, use_complex, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=320, height=180,
        effect=effect, rhythm=rhythm,
    )
    assert use_complex
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(bg),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        *extra,
        "-filter_complex", graph,
        "-map", "[out]", "-map", "1:a", "-t", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        str(comp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{effect} composite failed: {r.stderr[-300:]}"
    assert comp.exists() and comp.stat().st_size > 0


@pytest.mark.parametrize(
    "effect",
    ["snow", "rain", "light", "shadow_play", "beat_ripple"],
)
def test_apply_short_effect_overlays_on_vertical_short(tmp_path, effect):
    """The short's ffmpeg effect post-pass (_apply_short_effect) screen-blends a
    looped fx onto a finished vertical short — this is how the short finally
    gets the effect overlay it was missing entirely (client-visible divergence
    from the main video). Output stays a valid 1080x1920 video."""
    import shutil
    from pipeline import _apply_short_effect

    # A vertical 'short' (1080x1920) with a silent audio track, like moviepy writes.
    img = tmp_path / "v.jpg"
    _make_image(img, 1080, 1920)
    base = tmp_path / "base.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(img),
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2",
         "-vf", "scale=1080:1920", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", "-shortest", str(base)],
        check=True, capture_output=True,
    )
    short = tmp_path / f"short_{effect}.mp4"
    shutil.copy(base, short)               # _apply_short_effect replaces in place
    fx_path = fx.effect_path(effect)
    out = _apply_short_effect(str(short), fx_path, 24, str(tmp_path))
    assert os.path.exists(out)
    w, h, dur = _probe(out)
    assert (w, h) == (1080, 1920)          # stays vertical
    assert dur > 1.5


def test_build_video_filter_shape():
    """No effect → cheap -vf path; effect → filter_complex with a screen blend
    and the looped fx as an extra input."""
    f_none, complex_none, extra_none = fx.build_video_filter(
        ass_basename="x.ass", font_dir="/tmp", width=1920, height=1080, effect="")
    assert complex_none is False and extra_none == []

    f_snow, complex_snow, extra_snow = fx.build_video_filter(
        ass_basename="x.ass", font_dir="/tmp", width=1920, height=1080, effect="snow")
    assert complex_snow is True
    assert "blend=all_mode=screen" in f_snow
    assert "-stream_loop" in extra_snow


@pytest.mark.parametrize("effect", list(fx.PIXEL_TRANSFORM_EFFECTS))
def test_pixel_transform_changes_selected_photo_pixels(tmp_path, effect):
    """Rendered output must materially differ from the source photo itself."""
    img = tmp_path / "photo.jpg"
    _make_image(img, 320, 180)
    out = tmp_path / f"transform_{effect}.mp4"
    graph, _, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=320, height=180, effect=effect,
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "24", "-i", str(img),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        *extra,
        "-filter_complex", graph,
        "-map", "[out]", "-t", "0.8", "-c:v", "libx264",
        "-preset", "ultrafast", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    frame = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", "0.5", "-i", str(out),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True, capture_output=True, timeout=30,
    ).stdout
    rendered = np.frombuffer(frame, dtype=np.uint8).reshape(180, 320, 3)
    source = np.asarray(Image.open(img).convert("RGB"), dtype=np.uint8)
    mad = np.abs(rendered.astype(np.int16) - source.astype(np.int16)).mean()
    # Foto viva intentionally modifies a bounded subject region, so global
    # image MAD is lower than full-frame warps. Temporal locality is asserted
    # separately below; here we only prove it survives the final encode.
    threshold = (
        0.55
        if effect in {"foto_viva", "chromatic_pulse", "ink_reveal"}
        else 2.0
    )
    assert mad > threshold, f"{effect} was visually indistinguishable (MAD={mad:.2f})"


def _raw_effect_gray(effect, at=4.0):
    raw = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-ss", str(at),
            "-i", fx.effect_path(effect), "-vf", "scale=320:180",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(180, 320)


def test_corrected_mask_assets_protect_the_lyric_safe_area():
    shadows = _raw_effect_gray("shadow_play")
    ink = _raw_effect_gray("ink_reveal")
    chromatic = _raw_effect_gray("chromatic_pulse")

    # Leaf gobos enter from the corners instead of covering the centre with
    # screen-height ellipses.
    assert shadows[45:145, 80:240].mean() > 249
    assert 0.01 < (shadows < 235).mean() < 0.22
    # Ink is made of bounded brushstrokes, never a near-full-frame black mask.
    assert 0.02 < (ink < 220).mean() < 0.28
    # Chromatic energy stays peripheral so the lyric area is not color-washed.
    centre = chromatic[50:130, 90:230].mean()
    border = np.concatenate(
        [
            chromatic[:35].ravel(),
            chromatic[-35:].ravel(),
            chromatic[:, :45].ravel(),
            chromatic[:, -45:].ravel(),
        ]
    ).mean()
    assert border > centre + 3.0


def test_halftone_production_graph_preserves_neutral_channel_balance(tmp_path):
    """Regression: planar-GBR `eq=saturation` turned this effect solid green."""
    image = tmp_path / "neutral.png"
    Image.new("RGB", (320, 180), (176, 176, 176)).save(image)
    output = tmp_path / "halftone.mp4"
    graph, _, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=320, height=180, effect="halftone",
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", "24", "-i", str(image),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            *extra, "-filter_complex", graph, "-map", "[out]",
            "-t", "0.7", "-c:v", "libx264", "-preset", "ultrafast", str(output),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    raw = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-ss", "0.45", "-i", str(output),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(180, 320, 3)
    channel_means = frame.mean(axis=(0, 1))
    assert channel_means.max() - channel_means.min() < 4.0


def test_foto_viva_moves_a_bounded_region_over_time(tmp_path):
    """The local fallback reads as motion without moving the whole frame."""
    img = tmp_path / "photo.jpg"
    _make_image(img, 320, 180)
    image = Image.open(img).convert("RGB")
    # Semantic-looking high-frequency landmarks make local movement measurable
    # (the generic gradient fixture is deliberately almost textureless).
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    draw.ellipse((42, 35, 126, 119), fill=(245, 185, 54), outline=(15, 20, 45), width=7)
    draw.rectangle((205, 50, 283, 137), fill=(40, 202, 188), outline=(15, 20, 45), width=7)
    image.save(img)

    out = tmp_path / "foto_viva_motion.mp4"
    graph, _, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=320, height=180, effect="foto_viva",
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", "24", "-i", str(img),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            *extra, "-filter_complex", graph, "-map", "[out]",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", str(out),
        ],
        check=True, capture_output=True, timeout=60,
    )

    def frame(at):
        raw = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", str(out),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ],
            check=True, capture_output=True, timeout=30,
        ).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(180, 320, 3)

    delta = np.abs(frame(0.1).astype(np.int16) - frame(1.6).astype(np.int16))
    changed = delta.mean(axis=2) > 2.0
    assert delta.mean() > 2.0
    assert 0.08 < changed.mean() < 0.70
