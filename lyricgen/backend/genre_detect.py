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


_PHRASES = {
    # Compound answers: the genre that decides the look comes LAST in English
    # ("pop rock" is rock, "rap metal" is metal) — except the few below.
    "hip hop": "hiphop", "k pop": "pop", "j pop": "pop", "latin pop": "latin",
    "pop latino": "latin", "rock nacional": "rock", "nu metal": "metal",
    "heavy metal": "metal", "hard rock": "rock",
}
_ALIAS = {
    "rap": "hiphop", "trap": "hiphop", "drill": "hiphop", "latino": "latin",
    "cumbia": "latin", "salsa": "latin", "bachata": "latin", "cuarteto": "latin",
    "merengue": "latin", "dembow": "reggaeton", "regueton": "reggaeton",
    "balada": "ballad", "bolero": "ballad", "electronica": "electronic",
    "edm": "electronic", "house": "electronic", "techno": "electronic",
    "folclore": "folk", "folklore": "folk", "tango": "folk", "zamba": "folk",
    "chacarera": "folk", "metalcore": "metal", "thrash": "metal",
    "hardcore": "metal", "grunge": "rock", "synthpop": "pop",
}


def parse_answer(text: str) -> str:
    """Genre of the model's answer ("" if none). The model is told to answer
    one word; when it doesn't, the LAST genre word of the first phrase wins
    ("pop rock" → rock, "folk-rock" → rock, "pop punk" → punk)."""
    import unicodedata
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    # First clause only: "rock, it's pop" / "rock. maybe pop" → rock.
    raw = re.split(r"[,.;:\n(/]| or | o ", raw, maxsplit=1)[0]
    raw = " ".join(re.findall(r"[a-z]+", raw))
    for phrase, g in _PHRASES.items():
        if re.search(rf"\b{phrase}\b", raw):
            return g
    found = ""
    for tok in raw.split():
        tok = _ALIAS.get(tok, tok)
        if tok in R.GENRES:
            found = tok
    return found


def detect(artist: str, title: str, lyrics_text: str = "", *, job_id: str | None = None,
           provider=None, cached_only: bool = False) -> tuple[str, str]:
    """(genre, source) where source is 'cache' | 'ai' | 'none'.

    ``cached_only`` never calls the model (high-rate preview endpoints read
    the cache; the paid call goes through the rate-limited detect endpoint
    or the render)."""
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
    if cached_only:
        return "", "none"
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
