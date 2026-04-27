"""AI Provider abstraction — allows swapping video/image/text providers via env var.

Default providers: Google Veo 3.1 (video), Imagen 4 (image), Gemini 2.5 Flash (text).
Configure via env vars: VIDEO_PROVIDER, IMAGE_PROVIDER, TEXT_PROVIDER.
"""

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
    name = "veo-3.1-generate-001"
    provider = "google_vertex"

    def generate_video(self, prompt, output_path, job_id=None, aspect_ratio="16:9"):
        from pipeline import _generate_veo_video
        return _generate_veo_video(prompt, output_path, job_id=job_id)

    def get_model_version(self):
        return self.name


class ImagenImageProvider(ImageProvider):
    """Google Imagen 4 via Vertex AI."""
    name = "imagen-4.0-generate-001"
    provider = "google_vertex"

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
}

_IMAGE_PROVIDERS = {
    "imagen": ImagenImageProvider,
}

_TEXT_PROVIDERS = {
    "gemini": GeminiTextProvider,
}


def get_video_provider() -> VideoProvider:
    """Get the configured video provider (default: veo)."""
    name = os.environ.get("VIDEO_PROVIDER", "veo").lower()
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
