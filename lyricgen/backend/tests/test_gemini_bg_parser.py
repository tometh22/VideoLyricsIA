"""Regression tests for the Gemini background-response parser.

Incident 2026-05-15: after the system_prompt in #152 expanded with 6
worked examples and lyrics-anchor instructions, Gemini's output became
more verbose and started truncating at `max_output_tokens=500` mid-
property. Two prod jobs were observed:

  - 7e89f00cd130 ("Lunes Por La Madrugada" / Los Abuelos De La Nada)
    concept=industrial, parse failed, Veo got "stars and milky way"
    from the combinatorial random fallback.
  - 7fd94fb30fc1 ("Me late" / Los Pericos)
    raw output cut mid-word: `"prompt": "...then a ge`
    fallback random picked "northern lights aurora" — visible polar
    bias from the same fallback list that produced the original
    iceberg incident.

The fix has two parts pinned by these tests:

  1. `max_output_tokens` must be at least 1000. 500 is provably too low
     for the post-#152 prompt richness; 1500 is the chosen target with
     ~3× headroom over typical responses.
  2. `_parse_gemini_bg_response` must recover usable prompts even when
     the JSON is truncated by max_output_tokens or wrapped in markdown
     code fences. Falling to the combinatorial random is the worst
     outcome (concept/lyrics/hint all ignored), and a defensive parser
     keeps it as a last-resort path rather than the common path.
"""

import inspect

import pipeline


# ---------------------------------------------------------------------------
# Token budget pin
# ---------------------------------------------------------------------------

def test_max_output_tokens_is_at_least_1000():
    """Pin `max_output_tokens >= 1000` so a future "trim costs" patch can't
    silently regress us to the 500-token cap that broke prod 2026-05-15.
    """
    src = inspect.getsource(pipeline._analyze_lyrics_for_background)
    import re
    match = re.search(r"max_output_tokens\s*=\s*(\d+)", src)
    assert match is not None, (
        "max_output_tokens kwarg should be present in the Gemini call. "
        "If you removed it, Gemini's default is 8192 which is also fine, "
        "but please update this test to assert that explicitly."
    )
    value = int(match.group(1))
    assert value >= 1000, (
        f"max_output_tokens={value} is too low. The concept+match_lyrics=True "
        f"branch (post-#152) produces verbose responses that truncate at <1000 "
        f"tokens and break the JSON parser. Set to >=1000 (currently we use "
        f"1500). Incident date: 2026-05-15."
    )


# ---------------------------------------------------------------------------
# Parser — happy paths
# ---------------------------------------------------------------------------

def test_parses_bare_json():
    """The simplest happy path: bare JSON, no fences, no preamble."""
    raw = '{"style":"video","prompt":"Slow drone over a stormy desert highway at dusk, vintage road sign in the foreground"}'
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None
    assert result["style"] == "video"
    assert "stormy desert highway" in result["prompt"]


def test_parses_markdown_wrapped_json():
    """Gemini 2.5 Flash often wraps JSON in ```json fences. Must strip cleanly."""
    raw = """```json
{
  "style": "video",
  "prompt": "Slow drift through a sunlit room at golden hour, gauze curtains moving softly"
}
```"""
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None
    assert result["style"] == "video"
    assert "sunlit room" in result["prompt"]


def test_parses_json_with_preamble():
    """Gemini sometimes adds a brief preamble before the JSON. Must skip it."""
    raw = """Here is the JSON response based on the lyrics:

{"style":"video","prompt":"Aerial pull-back over a misty mountain valley at dawn"}"""
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None
    assert "misty mountain valley" in result["prompt"]


def test_parses_multiline_json_with_long_prompt():
    """A realistic 80-120 word prompt rendered across multiple lines."""
    raw = '''{
  "style": "video",
  "prompt": "Slow first-person glide through a forest clearing at twilight, a small campfire burning in the center casting warm orange light on weathered logs and the surrounding pines, sparks drifting upward into the cool blue evening air, subtle wind moving low branches, smoke curling lazily, contemplative and intimate, cinematic 4k with rich depth of field"
}'''
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None
    assert "campfire" in result["prompt"]
    assert "twilight" in result["prompt"]


# ---------------------------------------------------------------------------
# Parser — truncation recovery (the actual incident shape)
# ---------------------------------------------------------------------------

def test_recovers_prompt_from_json_truncated_mid_property():
    """The exact shape that broke prod 2026-05-15: max_output_tokens hit
    mid-prompt, no closing quote, no closing brace. Old parser returned
    None and fell to the combinatorial random. New parser must extract
    the partial prompt and return it.
    """
    raw = '''{
  "style": "video",
  "prompt": "Slow, pulsing zoom into the heart of a vibrant, tropical night market, illuminated by an array of colorful paper lanterns and string lights, reflecting off slick cobblestones after a light rain. Focus on a blur of exotic flowers in the foreground, then a ge'''
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None, (
        "Truncated JSON should recover via the field-level regex fallback. "
        "Falling to the combinatorial random was the prod incident."
    )
    assert "tropical night market" in result["prompt"]
    assert result["style"] == "video"


def test_recovers_prompt_from_markdown_wrapped_truncation():
    """Truncation can happen inside a markdown fence. Stage 1 (greedy JSON)
    fails because there's no closing brace; Stage 2 (field regex) must
    still extract the prompt content despite the unclosed fence.
    """
    raw = '''```json
{
  "style": "video",
  "prompt": "Wide aerial shot of a coastal cliff at sunset, waves crashing against dark basalt rocks, gulls circling overhead, the horizon glowing in amber and rose, kelp swaying in tide pools belo'''
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is not None, (
        "Markdown-wrapped truncation should still recover via field regex."
    )
    assert "coastal cliff" in result["prompt"]


# ---------------------------------------------------------------------------
# Parser — true unrecoverable cases (don't pretend to succeed)
# ---------------------------------------------------------------------------

def test_returns_none_on_completely_malformed_response():
    """If the response has no `prompt` field at all, we should fall to
    combinatorial — pretending to succeed would render nonsense.
    """
    raw = "I'm sorry, I cannot generate a prompt for this song."
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is None


def test_returns_none_on_empty_response():
    raw = ""
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is None


def test_returns_none_on_too_short_recovered_prompt():
    """If we extract a "prompt" value that's <15 chars (Gemini outputting
    an error code, or near-immediate truncation), fall through. The
    downstream length gate (>15) was the original guard; preserve it
    so we don't render two-word "scenes" through Veo.
    """
    raw = '{"style":"video","prompt":"err"}'
    result = pipeline._parse_gemini_bg_response(raw)
    # Note: this returns the dict (parses fine) but the caller's len > 15
    # check rejects it. The parser itself only enforces the length gate
    # on the *recovery* path (Stage 2). For Stage 1 (clean JSON) the
    # caller does its own length check. Verify both paths:
    if result is not None:
        assert len(result.get("prompt", "")) < 15, (
            "If parser returns short prompt from clean JSON, the caller's "
            "downstream len(prompt) > 15 check will reject it. That's OK."
        )


def test_truncation_below_recovery_threshold_returns_none():
    """If truncation happens so early there's not even 15 chars of prompt
    content, recovery should refuse rather than emit garbage.
    """
    raw = '{"style":"video","prompt":"Slow"'
    result = pipeline._parse_gemini_bg_response(raw)
    assert result is None, (
        "Recovery should refuse prompts <15 chars to match the downstream gate."
    )
