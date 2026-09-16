"""AI Provider abstraction — allows swapping video/image/text providers via env var.

Default providers: Google Veo 3.1 (video), Imagen 4 (image), Gemini 2.5 Flash (text).
Configure via env vars: VIDEO_PROVIDER, IMAGE_PROVIDER, TEXT_PROVIDER.
"""

import json
import os
from abc import ABC, abstractmethod


class VideoProvider(ABC):
    """Abstract base for video generation providers."""
    name: str
    provider: str

    @abstractmethod
    def generate_video(self, prompt: str, output_path: str, job_id: str = None,
                       aspect_ratio: str = "16:9") -> str:
        """Generate a video and return the file path."""

    @abstractmethod
    def get_model_version(self) -> str:
        pass


class ImageProvider(ABC):
    """Abstract base for image generation providers."""
    name: str
    provider: str

    @abstractmethod
    def generate_image(self, prompt: str, output_path: str, job_id: str = None,
                       aspect_ratio: str = "16:9") -> str:
        """Generate an image and return the file path."""

    @abstractmethod
    def get_model_version(self) -> str:
        pass


class TextProvider(ABC):
    """Abstract base for text generation providers."""
    name: str
    provider: str

    @abstractmethod
    def generate_text(self, system_prompt: str, user_prompt: str,
                      job_id: str = None, **kwargs) -> str:
        """Generate text and return the response."""

    @abstractmethod
    def get_model_version(self) -> str:
        pass


# ---------------------------------------------------------------------------
# Google Vertex AI implementations (default)
# ---------------------------------------------------------------------------

class VeoVideoProvider(VideoProvider):
    """Google Veo 3.1 via Vertex AI."""
    name = "veo-3.1-lite-generate-001"
    provider = "google_vertex"

    def generate_video(self, prompt, output_path, job_id=None, aspect_ratio="16:9"):
        from pipeline import _generate_veo_video
        return _generate_veo_video(prompt, output_path, job_id=job_id)

    def get_model_version(self):
        return self.name


class FireflyVideoProvider(VideoProvider):
    """Adobe Firefly Generate Video via OAuth server-to-server.

    The client is initialized lazily so workers that serve other tenants never
    require Adobe credentials.  Missing entitlement or secrets therefore fail
    only the Firefly-pinned job and cannot trigger a provider substitution.
    """

    name = "video1_standard"
    provider = "adobe_firefly"
    _DIMENSIONS = {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
    }

    def __init__(self, client=None, rate=None):
        self._client = client
        self._rate = rate

    def _get_client(self):
        if self._client is None:
            from firefly_client import FireflyClient

            self._client = FireflyClient()
        return self._client

    def _get_rate(self):
        if self._rate is None:
            from firefly_pricing import load_firefly_video_rate

            self._rate = load_firefly_video_rate()
        return self._rate

    def generate_video(
        self, prompt, output_path, job_id=None, aspect_ratio="16:9"
    ):
        try:
            width, height = self._DIMENSIONS[aspect_ratio]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported Firefly aspect ratio: {aspect_ratio}"
            ) from exc

        # This is a hard pre-submit gate.  Adobe's public app-credit plans do
        # not define API Operations pricing, so a missing private rate card
        # must stop the call instead of recording a made-up default cost.
        rate = self._get_rate()
        recorder = None
        if job_id:
            from provenance import record_ai_call

            recorder = record_ai_call(
                job_id=job_id,
                step="video_bg",
                tool_name=self.name,
                tool_provider=self.provider,
                prompt=prompt,
                input_data_types=["generated_prompt"],
                tool_version=self.name,
            )
        try:
            result = self._get_client().generate_video(
                prompt,
                output_path,
                width=width,
                height=height,
            )
        except Exception as exc:
            if recorder:
                recorder.finish(
                    response_summary=(
                        f"error: {exc.__class__.__name__}"
                    )
                )
            raise

        if recorder:
            recorder.finish(
                response_summary=json.dumps(
                    {
                        "provider_job_id": result.provider_job_id,
                        "model_version": result.model_version,
                        "width": result.width,
                        "height": result.height,
                        "seed": result.seed,
                        "output_sha256": result.output_sha256,
                        "output_bytes": result.output_bytes,
                        **rate.provenance_fields(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                output_artifact=output_path,
            )
        return output_path

    def get_model_version(self):
        return self.name


class ImagenImageProvider(ImageProvider):
    """Google still-image generation via Vertex AI.

    Named "Imagen" for backwards compatibility with the IMAGE_PROVIDER env
    var and the registry key, but the model underneath is resolved at call
    time: Vertex stopped serving the Imagen publisher-model family to our
    project on 2026-07-16 (404 in every region), so the effective model is
    now `gemini-2.5-flash-image`. See `pipeline._resolve_still_image_model`.
    """
    provider = "google_vertex"

    @property
    def name(self):
        from pipeline import _resolve_still_image_model
        return _resolve_still_image_model()

    def generate_image(self, prompt, output_path, job_id=None, aspect_ratio="16:9"):
        from pipeline import _generate_imagen_image
        return _generate_imagen_image(prompt, output_path, job_id=job_id)

    def get_model_version(self):
        return self.name


class GeminiTextProvider(TextProvider):
    """Google Gemini 2.5 Flash via Vertex AI."""
    name = "gemini-2.5-flash"
    provider = "google_vertex"

    def generate_text(self, system_prompt, user_prompt, job_id=None, **kwargs):
        from pipeline import _get_genai_client
        from google import genai

        client = _get_genai_client()
        response = client.models.generate_content(
            model=self.name,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=kwargs.get("temperature", 0.7),
                max_output_tokens=kwargs.get("max_output_tokens", 500),
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return response.text.strip()

    def get_model_version(self):
        return self.name


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_VIDEO_PROVIDERS = {
    "veo": VeoVideoProvider,
    "firefly": FireflyVideoProvider,
}

_IMAGE_PROVIDERS = {
    "imagen": ImagenImageProvider,
}

_TEXT_PROVIDERS = {
    "gemini": GeminiTextProvider,
}


def get_video_provider(name: str | None = None) -> VideoProvider:
    """Get an explicitly selected video provider (default: legacy env/Veo)."""
    name = (name or os.environ.get("VIDEO_PROVIDER", "veo")).lower()
    cls = _VIDEO_PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"Unknown video provider: {name}. Available: {list(_VIDEO_PROVIDERS.keys())}")
    return cls()


def get_image_provider() -> ImageProvider:
    """Get the configured image provider (default: imagen)."""
    name = os.environ.get("IMAGE_PROVIDER", "imagen").lower()
    cls = _IMAGE_PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"Unknown image provider: {name}. Available: {list(_IMAGE_PROVIDERS.keys())}")
    return cls()


def get_text_provider() -> TextProvider:
    """Get the configured text provider (default: gemini)."""
    name = os.environ.get("TEXT_PROVIDER", "gemini").lower()
    cls = _TEXT_PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"Unknown text provider: {name}. Available: {list(_TEXT_PROVIDERS.keys())}")
    return cls()
