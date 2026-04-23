"""Render specifications and UMG delivery profile validation.

A RenderSpec centralizes all encoding/compositing parameters so the pipeline
can emit either the YouTube MP4 (H.264, 1080p, 24 fps, yuv420p) or a UMG
master (.mov ProRes, multi-resolution, native fps, BT.709) from the same
composition code.
"""

from dataclasses import dataclass


# UMG-accepted frame sizes with display aspect ratio.
UMG_FRAME_SIZES = {
    "DCI-4K": {"width": 4096, "height": 2160, "dar": (256, 135)},
    "UHD-4K": {"width": 3840, "height": 2160, "dar": (16, 9)},
    "DCI-2K": {"width": 2048, "height": 1080, "dar": (256, 135)},
    "HD":     {"width": 1920, "height": 1080, "dar": (16, 9)},
}

UMG_FPS = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)

# prores_ks profiles: 3=422 HQ, 4=4444, 5=4444 XQ
UMG_PRORES_PROFILES = {
    3: {"label": "ProRes 422 HQ", "pix_fmt": "yuv422p10le"},
    4: {"label": "ProRes 4444",   "pix_fmt": "yuv444p10le"},
    5: {"label": "ProRes 4444 XQ","pix_fmt": "yuv444p10le"},
}

# Map ffmpeg rational framerates for fractional fps.
FPS_RATIONAL = {
    23.976: "24000/1001",
    29.97:  "30000/1001",
    59.94:  "60000/1001",
}


@dataclass
class RenderSpec:
    profile: str              # "youtube" | "umg"
    width: int
    height: int
    fps: float
    dar: tuple[int, int]
    codec: str                # "libx264" | "prores_ks"
    prores_profile: int | None
    pix_fmt: str              # "yuv420p" | "yuv422p10le" | "yuv444p10le"
    audio_codec: str          # "aac" | "pcm_s24le"
    color_primaries: str      # "bt709"
    container: str            # "mp4" | "mov"

    @property
    def fps_str(self) -> str:
        """ffmpeg-compatible framerate string (rational for fractional fps)."""
        return FPS_RATIONAL.get(self.fps, str(self.fps))

    @property
    def text_scale(self) -> float:
        """Scale factor for font sizes and text widths vs the 1080p baseline."""
        return self.height / 1080.0

    @staticmethod
    def youtube_default() -> "RenderSpec":
        return RenderSpec(
            profile="youtube",
            width=1920, height=1080,
            fps=24.0,
            dar=(16, 9),
            codec="libx264",
            prores_profile=None,
            pix_fmt="yuv420p",
            audio_codec="aac",
            color_primaries="bt709",
            container="mp4",
        )

    @staticmethod
    def youtube_short() -> "RenderSpec":
        return RenderSpec(
            profile="youtube",
            width=1080, height=1920,
            fps=24.0,
            dar=(9, 16),
            codec="libx264",
            prores_profile=None,
            pix_fmt="yuv420p",
            audio_codec="aac",
            color_primaries="bt709",
            container="mp4",
        )

    @staticmethod
    def umg(frame_size: str, fps: float, prores_profile: int) -> "RenderSpec":
        if frame_size not in UMG_FRAME_SIZES:
            raise ValueError(
                f"Invalid UMG frame_size '{frame_size}'. "
                f"Allowed: {list(UMG_FRAME_SIZES)}"
            )
        if fps not in UMG_FPS:
            raise ValueError(
                f"Invalid UMG fps {fps}. Allowed: {list(UMG_FPS)}"
            )
        if prores_profile not in UMG_PRORES_PROFILES:
            raise ValueError(
                f"Invalid UMG prores_profile {prores_profile}. "
                f"Allowed: {list(UMG_PRORES_PROFILES)}"
            )

        dims = UMG_FRAME_SIZES[frame_size]
        prof = UMG_PRORES_PROFILES[prores_profile]
        return RenderSpec(
            profile="umg",
            width=dims["width"],
            height=dims["height"],
            fps=fps,
            dar=dims["dar"],
            codec="prores_ks",
            prores_profile=prores_profile,
            pix_fmt=prof["pix_fmt"],
            audio_codec="pcm_s24le",
            color_primaries="bt709",
            container="mov",
        )


def validate_umg_config(frame_size: str, fps: float, prores_profile: int) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    errors = []
    if frame_size not in UMG_FRAME_SIZES:
        errors.append(
            f"frame_size must be one of {list(UMG_FRAME_SIZES)}, got '{frame_size}'"
        )
    if fps not in UMG_FPS:
        errors.append(f"fps must be one of {list(UMG_FPS)}, got {fps}")
    if prores_profile not in UMG_PRORES_PROFILES:
        errors.append(
            f"prores_profile must be one of {list(UMG_PRORES_PROFILES)}, "
            f"got {prores_profile}"
        )
    return errors


def umg_catalog() -> dict:
    """Catalog of accepted UMG specs, for frontend dropdowns."""
    return {
        "frame_sizes": [
            {
                "key": key,
                "width": v["width"],
                "height": v["height"],
                "dar": f"{v['dar'][0]}:{v['dar'][1]}",
            }
            for key, v in UMG_FRAME_SIZES.items()
        ],
        "fps": list(UMG_FPS),
        "prores_profiles": [
            {"key": k, "label": v["label"], "pix_fmt": v["pix_fmt"]}
            for k, v in UMG_PRORES_PROFILES.items()
        ],
    }
