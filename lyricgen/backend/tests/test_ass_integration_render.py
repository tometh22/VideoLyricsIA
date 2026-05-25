"""End-to-end integration test for the libass fast render path.

Renders a tiny clip through the REAL `_render_lyrics_ass` (ass_render +
ffmpeg `subtitles` burn + faststart) and ffprobes the output to prove it
is browser-playable: video + audio streams, duration ≈ audio, and the
moov atom at the FRONT (faststart). This is the automated gate that would
have caught the unplayable-video incident (124 MB MP4 with moov at the
end) before it shipped.

Skips when the local ffmpeg lacks libass (the `subtitles` filter) or when
the heavy render deps (moviepy/numpy) aren't importable — i.e. it runs in
CI / Docker (Debian ffmpeg has libass) but no-ops on a dev box without it.
"""

import json
import os
import subprocess

import pytest

_FONTS = os.path.join(os.path.dirname(__file__), "..", "fonts")


def _ffmpeg_has_libass() -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return False
    return any(line.split()[1:2] == ["subtitles"] for line in out.splitlines() if line.strip())


try:  # heavy deps (moviepy/numpy) — only present in CI/Docker
    import pipeline  # noqa: F401
    from render_spec import RenderSpec
    _PIPELINE_OK = True
except Exception:
    _PIPELINE_OK = False


pytestmark = pytest.mark.skipif(
    not (_PIPELINE_OK and _ffmpeg_has_libass()),
    reason="needs ffmpeg+libass and render deps (runs in CI/Docker)",
)


def _font():
    for name in ("Anton-Regular.ttf", "Oswald-Bold.ttf", "Montserrat-Bold.ttf"):
        p = os.path.join(_FONTS, name)
        if os.path.isfile(p):
            return p
    pytest.skip("no test font present")


def test_libass_render_is_playable(tmp_path):
    job_dir = str(tmp_path)
    bg = os.path.join(job_dir, "bg.mp4")
    mp3 = os.path.join(job_dir, "a.mp3")
    # Synthetic 4s background video + audio.
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=640x360:rate=24:duration=4",
                    "-pix_fmt", "yuv420p", bg], check=True, timeout=60)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=4", mp3], check=True, timeout=60)

    segments = [
        {"start": 0.5, "end": 1.8, "text": "hola mundo"},
        {"start": 2.0, "end": 3.5, "text": "segunda linea"},
    ]
    out = pipeline._render_lyrics_ass(
        bg, mp3, segments, job_dir, 4.0,
        spec=RenderSpec.youtube_default(), font_path=_font(),
        artist="Test Artist", song_title="Test Song",
    )
    assert os.path.exists(out) and os.path.getsize(out) > 4096

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type:format=duration", "-of", "json", out],
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    data = json.loads(probe.stdout)
    types = {s.get("codec_type") for s in data.get("streams", [])}
    assert "video" in types and "audio" in types
    dur = float(data["format"]["duration"])
    assert abs(dur - 4.0) < 2.0

    # faststart: moov must precede mdat near the front.
    with open(out, "rb") as f:
        head = f.read(2_000_000)
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    assert moov != -1 and (mdat == -1 or moov < mdat), "render lacks faststart"
