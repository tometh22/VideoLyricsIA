"""Filename parsing and resumable manifest support for Universal batches."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


TECHNICAL_CODE_RE = re.compile(r"(?:_)?(AR(?:F|UM)\d+)$", re.IGNORECASE)
VERSION_RE = re.compile(r"\s*\((live|en vivo)\)\s*$", re.IGNORECASE)


@dataclass
class AudioManifestEntry:
    source_path: str
    filename: str
    title: str
    artist: str
    lookup_title: str
    version: str
    technical_code: str
    fuzzy_lookup: bool
    size_bytes: int
    sha256: str
    duration_seconds: float | None
    status: str = "pending"
    job_id: str | None = None
    search_result: dict[str, Any] | None = None
    scoreboard: dict[str, Any] | None = None
    render_profile: dict[str, Any] | None = None
    background_id: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def parse_audio_filename(filename: str) -> dict[str, Any]:
    """Map ``Title_ArtistARF123.wav`` to explicit display/search metadata."""
    name = _nfc(Path(filename).name)
    stem = Path(name).stem
    code_match = TECHNICAL_CODE_RE.search(stem)
    if not code_match:
        raise ValueError(f"technical ARF/ARUM code missing: {filename}")
    technical_code = code_match.group(1).upper()
    remainder = stem[:code_match.start()].rstrip(" _")
    if "_" not in remainder:
        raise ValueError(f"title/artist separator missing: {filename}")
    title, artist = (part.strip() for part in remainder.rsplit("_", 1))
    if not title or not artist:
        raise ValueError(f"empty title or artist: {filename}")
    version_match = VERSION_RE.search(title)
    version = version_match.group(1) if version_match else ""
    lookup_title = VERSION_RE.sub("", title).strip()
    # Preserve display text exactly; only the search hint is normalized.
    lookup_title = " ".join(lookup_title.split())
    return {
        "filename": name,
        "title": title,
        "artist": artist,
        "lookup_title": lookup_title,
        "version": version.lower(),
        "technical_code": technical_code,
        # Instant-Taneas is intentionally not corrected.  The flag lets the
        # lyrics searcher try a fuzzy variant while display keeps the source.
        "fuzzy_lookup": "-" in lookup_title or "  " in title,
    }


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_seconds(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except (TypeError, ValueError):
        return None


def build_manifest(folder: str | Path) -> list[AudioManifestEntry]:
    root = Path(folder).expanduser().resolve()
    paths = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".wav")
    entries: list[AudioManifestEntry] = []
    seen_codes: set[str] = set()
    seen_hashes: set[str] = set()
    for path in paths:
        parsed = parse_audio_filename(path.name)
        checksum = _sha256(path)
        if parsed["technical_code"] in seen_codes:
            raise ValueError(f"duplicate technical code: {parsed['technical_code']}")
        if checksum in seen_hashes:
            raise ValueError(f"duplicate audio checksum: {path.name}")
        seen_codes.add(parsed["technical_code"])
        seen_hashes.add(checksum)
        entries.append(AudioManifestEntry(
            source_path=str(path),
            filename=parsed["filename"],
            title=parsed["title"],
            artist=parsed["artist"],
            lookup_title=parsed["lookup_title"],
            version=parsed["version"],
            technical_code=parsed["technical_code"],
            fuzzy_lookup=parsed["fuzzy_lookup"],
            size_bytes=path.stat().st_size,
            sha256=checksum,
            duration_seconds=_duration_seconds(path),
        ))
    return entries


def write_manifest(path: str | Path, entries: list[AudioManifestEntry], *, expected_count: int = 30) -> None:
    """Atomically write a manifest, retaining a machine-readable count check."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "expected_count": expected_count,
        "actual_count": len(entries),
        "count_ok": len(entries) == expected_count,
        "entries": [entry.to_dict() for entry in entries],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(target)


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text())
