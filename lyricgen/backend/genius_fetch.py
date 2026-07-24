"""Genius.com lyrics fallback when lrclib is missing or returns incomplete text.

WHY
---
lrclib.net is community-curated. Coverage is great for popular Latin / pop /
reggaeton catalogue, but it has two failure modes that bite "Rotor-grade"
ambitions:

1. **Track missing entirely** — niche releases, b-sides, recent indie drops.
2. **Track present but lyrics incomplete** — community uploader skipped the
   intro chorus, omitted bridge, or transcribed only the first verse. This
   is the "Legalícenla" failure mode (2026-05-25 incident): lrclib brought
   text that started at the verse, missing the intro chorus that the audio
   clearly contains; forced_align honoured the source so the first segment
   landed at ~45 s.

Genius.com has dramatically better coverage for mainstream catalogue
(Universal / Sony / Warner releases) because their model is editor-curated
not crowd-curated. They don't ship synced timestamps — we only use them for
the *text*, then forced_align pins the timing against the audio as usual.

CONTRACT
--------
- Behind `GENIUS_API_TOKEN` env var. Without it, `is_enabled()` returns
  False and `fetch_genius_plain` returns None silently. Genly stays on
  lrclib-only behaviour.
- `fetch_genius_plain(artist, song, db=None)` returns a plain-lyrics string
  or None. Never raises. Lyrics come HTML-scraped from the Genius song page
  (the official API only returns metadata + the page URL, not the lyrics
  text — that's behind the page).
- Optional `db` reuses the existing `LyricsCache` table with a `genius:`
  prefix so we never re-fetch the same song twice.

The HTML scraping is intentionally minimal: extract `[data-lyrics-container]`
divs, strip tags, decode entities, remove `[Verse 1]`/`[Chorus]` section
markers. Genius's page structure has been stable for years and degrades
gracefully — if the scrape fails, we return None and the caller falls
through to the next source (Gemini, then bare Whisper).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re

logger = logging.getLogger("genly.genius")

_TOKEN = os.environ.get("GENIUS_API_TOKEN", "").strip()
_API_BASE = "https://api.genius.com"
_SEARCH_TIMEOUT_S = 15.0
_FETCH_TIMEOUT_S = 20.0
_UA = "GenLyAI/1.0 (+https://app.genly.pro)"


def is_enabled() -> bool:
    """True iff GENIUS_API_TOKEN is set. Keeps the dep optional — staging /
    prod can toggle it independently."""
    return bool(_TOKEN)


def _cache_key(artist: str, song: str) -> str:
    """`genius:<hash>` keyspace in LyricsCache, separate from `lrclib:` so
    the two sources never collide on cache hits."""
    import hashlib
    h = hashlib.sha256(f"{(artist or '').strip().lower()}|{(song or '').strip().lower()}".encode("utf-8"))
    return f"genius:{h.hexdigest()[:16]}"


def _normalise_artist(text: str) -> str:
    """Lowercase + drop common Latin accents so 'Soda Stéreo' matches
    'Soda Stereo' in Genius's search results."""
    import unicodedata
    norm = unicodedata.normalize("NFKD", text or "").lower()
    return "".join(c for c in norm if not unicodedata.combining(c))


def _search_song_url(artist: str, song: str) -> str | None:
    """Hit Genius's official /search and pick the top hit whose primary
    artist normalised-matches the query. Returns the song's HTML URL on
    Genius (used by `_scrape_lyrics_from_url`), or None if no match."""
    import requests as _req
    try:
        r = _req.get(
            f"{_API_BASE}/search",
            params={"q": f"{artist} {song}"},
            headers={"Authorization": f"Bearer {_TOKEN}", "User-Agent": _UA},
            timeout=_SEARCH_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("[GENIUS] search request failed: %s", e)
        return None
    if r.status_code != 200:
        logger.warning("[GENIUS] /search %s for %r - %r", r.status_code, artist, song)
        return None
    try:
        hits = r.json().get("response", {}).get("hits") or []
    except Exception as e:
        logger.warning("[GENIUS] search JSON parse failed: %s", e)
        return None
    if not hits:
        return None
    target_artist = _normalise_artist(artist)
    for h in hits[:5]:                              # top-5 only — anything below is noise
        result = h.get("result") or {}
        primary = _normalise_artist((result.get("primary_artist") or {}).get("name", ""))
        if not primary:
            continue
        # Bidirectional substring match catches "Viejas Locas" vs "Las Viejas Locas"
        # and similar formatting variance.
        if target_artist in primary or primary in target_artist:
            url = result.get("url")
            if url:
                return url
    # INCIDENT 2026-05-25 (post-PR #309 smoke test): the original code had
    # a "top hit fallback" here — if no artist matched, take hits[0].
    # That produced disastrously wrong results:
    #   - "Amanda Pujó - Ser Anti" → Jotapê (Brazilian trap, ñada que ver)
    #   - "AIRBAG - El Plan Perfecto Vol.2" → unrelated reggaeton
    # Mixing wrong lyrics into forced_align is WORSE than no lyrics at all
    # because the resulting video looks confident-but-incorrect (timestamps
    # for text that doesn't exist in the audio). Strict artist match only —
    # better return None and fall through to Gemini.
    logger.info("[GENIUS] no artist match in top-5 hits for %r — refusing top-hit fallback", artist)
    return None


def _scrape_lyrics_from_url(url: str) -> str | None:
    """Pull the song page and extract lyrics from `[data-lyrics-container]`
    divs. Returns plain text with line breaks, or None on failure."""
    import requests as _req
    try:
        r = _req.get(url, headers={"User-Agent": _UA}, timeout=_FETCH_TIMEOUT_S)
    except Exception as e:
        logger.warning("[GENIUS] page fetch failed: %s", e)
        return None
    if r.status_code != 200:
        logger.warning("[GENIUS] page %s for %s", r.status_code, url)
        return None
    raw = r.text or ""
    # Grab every `[data-lyrics-container]` div. Genius wraps the lyrics in
    # potentially-multiple containers (one per stanza). We split the page
    # on the container's opening tag and grab the body up to the next
    # `<div ` or end of file. Non-greedy `.*?` doesn't work cleanly here
    # because real Genius pages have nested divs/spans/anchors inside.
    containers: list[str] = []
    for match in re.finditer(
        r'<div[^>]+data-lyrics-container[^>]*>',
        raw, re.IGNORECASE,
    ):
        start = match.end()
        # Walk depth-aware to find the matching close. Genius pages are
        # well-formed enough that this simple scan works: count <div opens
        # and </div> closes until depth returns to zero.
        depth = 1
        i = start
        while i < len(raw) and depth > 0:
            next_open = raw.find("<div", i)
            next_close = raw.find("</div>", i)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                i = next_open + 4
            else:
                depth -= 1
                if depth == 0:
                    containers.append(raw[start:next_close])
                i = next_close + 6
    if not containers:
        # Fallback: try the older single-container layout.
        containers = re.findall(r'<div class="lyrics">(.*?)</div>', raw, re.DOTALL)
    if not containers:
        logger.info("[GENIUS] no lyrics container found at %s", url)
        return None
    body = "\n".join(containers)
    # <br> → newline first (preserve the song's line structure).
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    # Strip remaining tags.
    body = re.sub(r"<[^>]+>", "", body)
    # Decode HTML entities (&#x27;, &amp;, etc.).
    body = _html.unescape(body)
    # Section markers ([Verse 1], [Chorus: feat. X], etc.) — drop entire line.
    body = re.sub(r"\[[^\]]+\]", "", body)
    # Genius sometimes injects "<song name> Lyrics" header — strip first 80
    # chars that look like a title preamble.
    body = re.sub(r"^[^\n]{0,80}Lyrics\s*\n", "", body, count=1)
    # Collapse 3+ newlines to 2 (paragraph break) and strip per-line whitespace.
    lines = [ln.strip() for ln in body.splitlines()]
    cleaned: list[str] = []
    for ln in lines:
        if not ln:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(ln)
    text = "\n".join(cleaned).strip()
    # Sanity floor: a real lyrics page has at least 4 non-empty lines and 60 chars.
    non_empty = [ln for ln in cleaned if ln]
    if len(non_empty) < 4 or len(text) < 60:
        logger.info("[GENIUS] page text too thin (%d lines / %d chars) — skip", len(non_empty), len(text))
        return None
    # Sanity ceiling (2026-05-25 smoke test finding): a real song has ~30-80
    # lines and < 5000 chars of unique text. Wildly larger means the scraper
    # picked up multiple lyric containers from the same page (genius shows
    # related/popular tracks below the main one on some layouts), or the
    # match was a "compilation" / "every song by artist X" aggregator page.
    # Either way the result is unsafe — mixing several songs' text would
    # corrupt forced_align. Reject and let Gemini try.
    if len(non_empty) > 200 or len(text) > 5000:
        logger.warning("[GENIUS] page text suspiciously large (%d lines / %d chars) — likely over-scrape, skip",
                       len(non_empty), len(text))
        return None
    return text


def fetch_genius_plain(artist: str, song: str, db=None) -> str | None:
    """Look up a song on Genius. Returns plain lyrics or None.

    `db` (optional): SQLAlchemy session. When provided, hits the same
    LyricsCache table used by lrclib with a `genius:` prefix so future
    calls don't re-fetch the same song.

    Best-effort — never raises.
    """
    if not is_enabled():
        return None
    if not artist or not song:
        return None

    cache_key = _cache_key(artist, song)
    if db is not None:
        try:
            from database import LyricsCache
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row and row.lyrics:
                logger.info("[GENIUS] cache hit %s (%d chars)", cache_key, len(row.lyrics))
                return row.lyrics
        except Exception as e:
            logger.warning("[GENIUS] cache read failed: %s", e)

    url = _search_song_url(artist, song)
    if not url:
        logger.info("[GENIUS] no match for %r - %r", artist, song)
        return None
    text = _scrape_lyrics_from_url(url)
    if not text:
        return None

    if db is not None:
        try:
            from database import LyricsCache
            row = LyricsCache(cache_key=cache_key, lyrics=text)
            db.merge(row)
            db.commit()
            logger.info("[GENIUS] cached %s (%d chars) from %s", cache_key, len(text), url)
        except Exception as e:
            logger.warning("[GENIUS] cache write failed: %s", e)
    else:
        logger.info("[GENIUS] fetched %r - %r → %d chars from %s",
                    artist, song, len(text), url)
    return text
