"""Manual smoke test for the Gemini-grounded lyrics fetcher.

NOT a pytest. Hits real Gemini and real network. Run before each prod push:

    cd lyricgen/backend
    source venv/bin/activate
    python scripts/manual_lyrics_smoke.py

Three songs cover the spread:
- ES popular (Bad Bunny / Tití Me Preguntó)
- EN popular (Taylor Swift / Cruel Summer)
- AR regional (El Plan de la Mariposa / El Riesgo) — the song that triggered
  the launch-day investigation; if Gemini-search can find this one, the
  feature is shippable.

Prints lyrics char count, line count, source URLs, and validation verdict
for each. Eyeball pass/fail before pushing.

Requires GOOGLE_APPLICATION_CREDENTIALS pointing to a Vertex service account
JSON, plus VERTEX_PROJECT / VERTEX_LOCATION env vars (same setup as the worker).
"""

import os
import sys
import time

# Make backend modules importable when run from the backend dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SONGS = [
    ("Bad Bunny", "Titi Me Pregunto"),
    ("Taylor Swift", "Cruel Summer"),
    ("El Plan de la Mariposa", "El Riesgo"),
]


def _print_section(title):
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def main():
    from credentials_bootstrap import bootstrap_vertex_credentials
    bootstrap_vertex_credentials()

    from database import SessionLocal, init_db, LyricsCache
    init_db()  # ensures lyrics_cache table exists locally

    from pipeline import _fetch_lyrics_via_gemini_search, _lyrics_cache_key

    db = SessionLocal()
    try:
        for artist, song in SONGS:
            _print_section(f"{artist} — {song}")

            # Bypass cache for the smoke run: we want fresh Gemini behavior.
            # Delete any pre-existing row so this exercises the full path.
            key = _lyrics_cache_key(artist, song)
            db.query(LyricsCache).filter(LyricsCache.cache_key == key).delete()
            db.commit()

            t0 = time.time()
            result = _fetch_lyrics_via_gemini_search(artist, song, db=db)
            elapsed = time.time() - t0

            if result is None:
                print(f"VERDICT: ✗ FAILED (returned None)  elapsed={elapsed:.1f}s")
                continue

            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == key
            ).first()

            lines = [ln for ln in result.splitlines() if ln.strip()]
            print(f"VERDICT: ✓ OK  elapsed={elapsed:.1f}s")
            print(f"  chars      : {len(result)}")
            print(f"  lines      : {len(lines)}")
            print(f"  distinct   : {len(set(lines))}")
            if row and row.source_urls:
                print(f"  sources    : {len(row.source_urls)}")
                for u in row.source_urls[:5]:
                    print(f"    - {u}")
            print(f"\n  --- first 8 lines ---")
            for ln in lines[:8]:
                print(f"  {ln}")
            print(f"  --- ... {max(0, len(lines) - 8)} more lines ---")
    finally:
        db.close()


if __name__ == "__main__":
    main()
