"""Stable product labels for published UMG deliveries."""

from __future__ import annotations

PRODUCT_CATEGORIES = frozenset({
    "artists_art", "hits_lyrics", "hits_art", "rock_lyrics", "rock_art",
})


def product_category(kind: str, channel: str | None) -> str | None:
    channel = (channel or "").lower().strip()
    if kind == "art_track":
        if channel == "hitsdesiempre":
            return "hits_art"
        if channel == "cancionerorock":
            return "rock_art"
        # Existing artist Art Tracks did not carry a channel. Preserve them
        # in the artist product rather than hiding them from a category tab.
        return "artists_art"
    if kind == "lyric_video":
        if channel == "hitsdesiempre":
            return "hits_lyrics"
        if channel == "cancionerorock":
            return "rock_lyrics"
    return None
