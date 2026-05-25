"""Golden-set loader. Parses YAML, filters by mode/only/skip, merges defaults
into per-song `expected` dicts. Pure + testable."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def load_golden_set(path: str | Path) -> dict:
    """Load the YAML golden set. Returns the raw dict with keys
    `schema_version`, `defaults`, `songs`."""
    import yaml  # PyYAML — already in backend deps
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    if "songs" not in data or not isinstance(data["songs"], list):
        raise ValueError(f"{path} missing `songs:` list")
    return data


def _merge_expected(song_expected: dict, default_expected: dict) -> dict:
    """Shallow merge: song-level keys override defaults. The
    `forbidden_timing_sources` list is *extended* (not replaced) so
    per-song extras add to the global deny-list."""
    merged: dict[str, Any] = dict(default_expected or {})
    merged.update(song_expected or {})
    deny_global = (default_expected or {}).get("forbidden_timing_sources") or []
    deny_song = (song_expected or {}).get("forbidden_timing_sources_extra") or []
    if deny_global or deny_song:
        merged["forbidden_timing_sources"] = list(deny_global) + [
            x for x in deny_song if x not in deny_global
        ]
    return merged


def resolve_expected(song: dict, defaults: dict) -> dict:
    """Compute the effective `expected` dict for a song, merging defaults."""
    return _merge_expected(song.get("expected") or {}, (defaults or {}).get("expected") or {})


def filter_songs(
    data: dict,
    mode: str | None = None,
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> list[dict]:
    """Apply --mode/--only/--skip filters to the songs list.

    Filtering order:
      1. --only (allow-list, exact id match) — if set, mode is ignored.
      2. --mode quick/full — keep songs whose `class:` contains the mode.
      3. --skip — drop matching ids.
    """
    songs: list[dict] = data.get("songs") or []
    if only:
        wanted = set(only)
        songs = [s for s in songs if s.get("id") in wanted]
    elif mode:
        songs = [s for s in songs if mode in (s.get("class") or [])]
    if skip:
        drop = set(skip)
        songs = [s for s in songs if s.get("id") not in drop]
    return songs
