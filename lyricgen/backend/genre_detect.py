"""Song genre for lyric looks: one cheap text-model call, cached.

None of the jobs carry a genre (the wizard field is almost never filled), so
the look's genre skin needs one. We ask the text provider to classify the song
from artist + title + a slice of the lyrics into lyric_recipes.GENRES. The
answer is cached per (artist, title) in memory and in R2, so a catalogue only
pays once per song (~USD 0.0001 with Gemini Flash).

Never raises: any failure returns "" and the look renders as designed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import threading

import lyric_recipes as R

logger = logging.getLogger("genly.genre_detect")

_MEM: dict[str, str] = {}
_LOCK = threading.Lock()

_SYSTEM = (
    "You classify songs into exactly one genre for choosing a lyric-video visual "
    "style. Answer with ONE word from this list and nothing else: "
    + ", ".join(R.GENRES) + ". Use what you know about the artist first; the lyrics "
    "are only a hint. Hard rock, heavy metal, thrash and hardcore are 'metal'; "
    "classic/alternative/pop-rock bands are 'rock'; cumbia, salsa, bachata and "
    "cuarteto are 'latin'; trap and rap are 'hiphop'; folklore, zamba, chacarera "
    "and tango are 'folk'; romantic ballads and bolero are 'ballad'."
)


def _key(artist: str, title: str) -> str:
    norm = lambda s: re.sub(r"\s+", " ", str(s or "").strip().lower())
    return hashlib.sha1(f"{norm(artist)}|{norm(title)}".encode("utf-8")).hexdigest()[:16]


def _r2_key(k: str) -> str:
    return f"lyric_genre/{k}.json"


def parse_answer(text: str) -> str:
    """First known genre word in the model's answer ("" if none)."""
    import unicodedata
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    whole = {"hip-hop": "hiphop", "hip hop": "hiphop", "k-pop": "pop", "j-pop": "pop",
             "pop-punk": "punk", "folk-rock": "rock", "pop-rock": "rock",
             "nu-metal": "metal", "heavy metal": "metal", "hard rock": "rock"}
    for phrase, g in whole.items():
        if phrase in raw:
            return g
    alias = {"rap": "hiphop", "trap": "hiphop", "latino": "latin", "balada": "ballad",
             "electronica": "electronic", "folclore": "folk", "reggaeton": "reggaeton",
             "regueton": "reggaeton", "edm": "electronic", "house": "electronic",
             "techno": "electronic", "bolero": "ballad", "tango": "folk"}
    for tok in re.findall(r"[a-z]+", raw):
        tok = alias.get(tok, tok)
        if tok in R.GENRES:
            return tok
    return ""


def detect(artist: str, title: str, lyrics_text: str = "", *, job_id: str | None = None,
           provider=None) -> tuple[str, str]:
    """(genre, source) where source is 'cache' | 'ai' | 'none'."""
    if not (artist or title):
        return "", "none"
    k = _key(artist, title)
    with _LOCK:
        if k in _MEM:
            return _MEM[k], "cache"
    try:
        import storage
        if storage.is_enabled() and storage.object_exists(_r2_key(k)):
            with tempfile.NamedTemporaryFile(suffix=".json") as tf:
                if storage.download_object(_r2_key(k), tf.name):
                    with open(tf.name) as fh:
                        g = R.normalize_genre(json.load(fh).get("genre"))
                    if g:
                        with _LOCK:
                            _MEM[k] = g
                        return g, "cache"
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.info("[GENRE] cache read failed: %s", exc)
    try:
        if provider is None:
            from ai_providers import get_text_provider
            provider = get_text_provider()
        prompt = (f"Artist: {artist}\nSong: {title}\nLyrics excerpt:\n"
                  f"{str(lyrics_text or '')[:600]}")
        genre = parse_answer(provider.generate_text(_SYSTEM, prompt, job_id=job_id,
                                                    temperature=0.0, max_output_tokens=8))
    except Exception as exc:
        logger.warning("[GENRE] detection failed for %r / %r: %s", artist, title, exc)
        return "", "none"
    if not genre:
        # Remember "unknown" for this process so renders/previews of the same
        # song don't pay for the same unanswerable question again.
        with _LOCK:
            _MEM[k] = ""
        return "", "none"
    with _LOCK:
        _MEM[k] = genre
    try:
        import storage
        if storage.is_enabled():
            storage.put_object_bytes(_r2_key(k), json.dumps({"genre": genre}).encode(),
                                     "application/json")
    except Exception as exc:  # pragma: no cover
        logger.info("[GENRE] cache write failed: %s", exc)
    return genre, "ai"
