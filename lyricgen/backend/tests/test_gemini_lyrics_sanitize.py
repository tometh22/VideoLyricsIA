"""Unit tests for _sanitize_gemini_lyrics — strips § (U+00A7) and ¶
(U+00B6) markers from Gemini-grounded lyrics scrapes.

Why this matters: lyrics scraped from sites like Letras.com / AZLyrics
sometimes include `§` as an estrofa separator in the HTML structure.
Without this sanitizer the char leaks into (a) lyrics_cache.lyrics
and (b) Whisper's `prompt` parameter, which biases the transcription
to emit § in jobs.segments_json. Real incident: Rata Blanca / Mujer
Amante, 2026-05-12 — operator reported `§ CON TU AMOR SENSUAL...`
rendered as text on the video.

Strictly conservative: only strips § and ¶. Anything that legitimately
appears in lyrics (accented chars, em-dashes, Spanish quotes, etc.)
must survive untouched — this is the regression guard for the
"don't break the 99% timestamp accuracy" constraint.
"""
from pipeline import _sanitize_gemini_lyrics


# ─── Core: strips the offending chars ───────────────────────────────

def test_strips_section_sign():
    assert _sanitize_gemini_lyrics("§ Siento el calor §") == " Siento el calor "


def test_strips_pilcrow():
    assert _sanitize_gemini_lyrics("¶ Con tu amor ¶") == " Con tu amor "


def test_strips_both_section_and_pilcrow():
    assert _sanitize_gemini_lyrics("§ línea uno ¶ línea dos §") == " línea uno  línea dos "


def test_strips_repeated_section_sign():
    """Some scrapes have multiple § consecutive between estrofas."""
    assert _sanitize_gemini_lyrics("§§§ texto §§") == " texto "


def test_strips_mid_line_section_sign():
    """The Mujer Amante real case: § appears at start AND end of a line."""
    raw = "§ CON TU AMOR SENSUAL CUÁNTO ME DAS §"
    assert _sanitize_gemini_lyrics(raw) == " CON TU AMOR SENSUAL CUÁNTO ME DAS "


# ─── Preservation: lyrics-legitimate chars survive ──────────────────

def test_preserves_spanish_diacritics():
    """Tildes, eñes, acentos — must survive untouched."""
    raw = "Cuánto me das, haz que mi sueño sea una verdad"
    assert _sanitize_gemini_lyrics(raw) == raw


def test_preserves_spanish_punctuation():
    """¡! ¿? ñ y acentos — todos deben sobrevivir."""
    raw = "¡Sí! ¿Por qué no, mi corazón? Año tras año."
    assert _sanitize_gemini_lyrics(raw) == raw


def test_preserves_em_dash_and_quotes():
    """Em-dashes y comillas tipográficas son válidos en lyrics."""
    raw = "Ella dijo «adiós» — y se fue"
    assert _sanitize_gemini_lyrics(raw) == raw


def test_preserves_multiline_structure():
    """Newlines + structure must be byte-identical when no § present."""
    raw = "línea uno\nlínea dos\n\nlínea tres"
    assert _sanitize_gemini_lyrics(raw) == raw


def test_preserves_empty_lines_separating_stanzas():
    """Stanzas separated by blank lines is the lrclib/Gemini convention."""
    raw = "verso 1 línea 1\nverso 1 línea 2\n\nverso 2 línea 1"
    assert _sanitize_gemini_lyrics(raw) == raw


# ─── Edge cases ─────────────────────────────────────────────────────

def test_empty_input_returns_empty():
    assert _sanitize_gemini_lyrics("") == ""


def test_none_input_returns_none():
    """Defensive: caller may pass None for missing/failed Gemini response."""
    assert _sanitize_gemini_lyrics(None) is None


def test_only_section_signs_returns_whitespace():
    """An input that is JUST section signs collapses to whitespace.
    Downstream validation (line count >= 8) will reject this — sanitizer
    just removes the chars, doesn't second-guess validation."""
    assert _sanitize_gemini_lyrics("§ § §") == "  "


def test_no_section_signs_returns_unchanged():
    """Most Gemini responses don't have §. Verify zero-cost passthrough."""
    raw = "Verse one with no markers\nVerse two also clean"
    result = _sanitize_gemini_lyrics(raw)
    assert result == raw
    # Identity check would be nice but Python's small-string interning
    # makes it non-deterministic. Equality is sufficient.


# ─── Regression guard: real Mujer Amante case ───────────────────────

def test_real_world_mujer_amante_pattern():
    """Reproduce the exact pattern observed in production for Rata
    Blanca / Mujer Amante on 2026-05-12. After sanitize, the line must
    be readable Spanish lyrics with no § leftover anywhere."""
    raw = (
        "Siento el calor de toda tu piel\n"
        "En mi cuerpo otra vez\n"
        "§ Con tu amor sensual cuánto me das §\n"
        "§ Haz que mi sueño sea una verdad §\n"
        "§ §\n"
        "Dame tu alma hoy"
    )
    cleaned = _sanitize_gemini_lyrics(raw)
    assert "§" not in cleaned
    # All lyric content survives
    assert "Siento el calor de toda tu piel" in cleaned
    assert "Con tu amor sensual cuánto me das" in cleaned
    assert "Haz que mi sueño sea una verdad" in cleaned
    assert "Dame tu alma hoy" in cleaned
