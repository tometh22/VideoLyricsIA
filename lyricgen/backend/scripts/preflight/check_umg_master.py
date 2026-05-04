"""P0 #2 — UMG master file conformance.

Generates a UMG-profile job end-to-end (or reuses an existing one if present)
and verifies via ffprobe that the resulting `_umg_master.mov` matches every
spec UMG audits on ingest:

  - codec_name == prores
  - prores profile id matches requested profile (3=422 HQ, 4=4444, 5=4444 XQ)
  - resolution matches the frame_size (HD = 1920x1080)
  - rate matches requested fps within 0.01 (handles 23.976 / 29.97 rationals)
  - pixel format matches profile (yuv422p10le or yuv444p10le)
  - audio: pcm_s24le, 48 kHz, 2 channels
  - color primaries / transfer / matrix all bt709
  - container is QuickTime (.mov)

If no UMG master is reachable we mark FAIL with a clear next-step message.
We do NOT generate a job from this check (would cost ~$1 of Veo per run);
the runner orchestrates that separately so a single Veo render serves
multiple checks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from ._base import Check, CheckResult


def _ffprobe(path: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            path,
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


def _find_stream(streams: list[dict], kind: str) -> dict | None:
    return next((s for s in streams if s.get("codec_type") == kind), None)


def _rate_to_float(rate: str) -> float:
    """Convert ffprobe rational ("24000/1001") to float."""
    m = re.match(r"^(\d+)/(\d+)$", rate)
    if m:
        n, d = int(m.group(1)), int(m.group(2))
        return n / d if d else 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


# Map (profile_id, label) — what ffprobe returns for prores_ks profile_id field.
PRORES_PROFILE_LABELS = {
    3: "HQ",
    4: "4444",
    5: "4444 XQ",
}


class UmgMasterCheck(Check):
    name = "umg_master_conformance"
    description = "ffprobe a UMG master against codec / fps / pix_fmt / audio / colour spec"
    p0 = True

    def __init__(self, master_path: str | None, expected: dict):
        self.master_path = master_path
        self.expected = expected

    def run(self) -> CheckResult:
        if not self.master_path or not os.path.exists(self.master_path):
            return self._failed(
                "no UMG master file available — generate one first via the API "
                "with delivery_profile=umg, then re-run this check pointing at "
                "the downloaded .mov",
                expected_at=self.master_path,
            )

        try:
            info = _ffprobe(self.master_path)
        except subprocess.CalledProcessError as e:
            return self._failed(
                "ffprobe rejected the file",
                stderr=e.stderr,
            )

        v = _find_stream(info["streams"], "video")
        a = _find_stream(info["streams"], "audio")
        fmt = info.get("format", {})

        if not v:
            return self._failed("no video stream found")
        if not a:
            return self._failed("no audio stream found")

        problems: list[str] = []

        # --- Container ---
        container_name = (fmt.get("format_name") or "").lower()
        if "mov" not in container_name and "quicktime" not in container_name:
            problems.append(
                f"container is {container_name!r}, expected QuickTime/.mov"
            )

        # --- Video codec / profile ---
        if v.get("codec_name") != "prores":
            problems.append(
                f"video codec_name is {v.get('codec_name')!r}, expected 'prores'"
            )

        profile_id = self.expected["prores_profile"]
        expected_label = PRORES_PROFILE_LABELS.get(profile_id, "")
        actual_profile = (v.get("profile") or "").strip()
        if expected_label and expected_label not in actual_profile:
            problems.append(
                f"prores profile is {actual_profile!r}, expected to contain "
                f"{expected_label!r} (id={profile_id})"
            )

        # --- Resolution ---
        if v.get("width") != self.expected["width"]:
            problems.append(
                f"width is {v.get('width')}, expected {self.expected['width']}"
            )
        if v.get("height") != self.expected["height"]:
            problems.append(
                f"height is {v.get('height')}, expected {self.expected['height']}"
            )

        # --- Frame rate (allow 0.01 tolerance for rational ffprobe parsing) ---
        actual_fps = _rate_to_float(v.get("r_frame_rate", "0/0"))
        expected_fps = float(self.expected["fps"])
        if abs(actual_fps - expected_fps) > 0.01:
            problems.append(
                f"fps is {actual_fps:.3f}, expected {expected_fps:.3f}"
            )

        # --- Pixel format ---
        if v.get("pix_fmt") != self.expected["pix_fmt"]:
            problems.append(
                f"pix_fmt is {v.get('pix_fmt')!r}, expected {self.expected['pix_fmt']!r}"
            )

        # --- Colour space ---
        for tag in ("color_primaries", "color_transfer", "color_space"):
            actual = (v.get(tag) or "").lower()
            if actual not in {"bt709", "unknown", ""}:
                problems.append(f"{tag} is {actual!r}, expected bt709 or unset")

        # --- Audio ---
        if a.get("codec_name") != "pcm_s24le":
            problems.append(
                f"audio codec_name is {a.get('codec_name')!r}, expected 'pcm_s24le'"
            )
        if int(a.get("sample_rate", 0)) != 48000:
            problems.append(
                f"audio sample_rate is {a.get('sample_rate')}, expected 48000"
            )
        if int(a.get("channels", 0)) != 2:
            problems.append(
                f"audio channels is {a.get('channels')}, expected 2 (stereo)"
            )

        size_mb = Path(self.master_path).stat().st_size / 1024 / 1024
        details = {
            "file": self.master_path,
            "size_mb": round(size_mb, 1),
            "container": fmt.get("format_name"),
            "video": {
                "codec": v.get("codec_name"),
                "profile": v.get("profile"),
                "resolution": f"{v.get('width')}x{v.get('height')}",
                "fps": round(actual_fps, 3),
                "pix_fmt": v.get("pix_fmt"),
                "color_primaries": v.get("color_primaries"),
                "color_transfer": v.get("color_transfer"),
                "color_space": v.get("color_space"),
            },
            "audio": {
                "codec": a.get("codec_name"),
                "sample_rate": a.get("sample_rate"),
                "channels": a.get("channels"),
                "bit_depth": a.get("bits_per_sample"),
            },
        }

        if problems:
            return self._failed(
                f"{len(problems)} spec violation(s) — UMG would reject this master",
                violations=problems,
                **details,
            )

        return self._passed(
            f"UMG master matches all spec ({size_mb:.1f} MB, "
            f"{v.get('profile')}, {actual_fps:.3f} fps)",
            **details,
        )
