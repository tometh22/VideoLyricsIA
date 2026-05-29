"""ASS (Advanced SubStation Alpha) subtitle generation for the fast
lyric-render path.

WHY THIS EXISTS
---------------
The legacy text path (pipeline._make_text_clip + CompositeVideoClip)
renders every frame in Python via moviepy: a 3-min video at 24fps is
~4,300 frames, each compositing the background plus N text layers
(rasterized through ImageMagick). That Python frame loop is the
dominant cost of a re-render (~8-15 min on staging).

libass renders the exact same overlay (white fill + black outline +
shadow + fades + per-line sizing) inside a single ffmpeg pass, in C.
A 3-min video drops to ~30-60s. This module turns the timed lyric
segments into an ASS file; the ffmpeg `subtitles` filter burns it onto
the (already ffmpeg-looped) background.

PARITY CONTRACT
---------------
The *styling decisions* (fontsize tiers by text length, fade durations,
the perceptual fade offset, contrast outline/shadow widths) stay in
pipeline.py as the single source of truth. This module is a pure
formatter: callers pass already-computed values per line and we emit
valid ASS. That keeps the look identical to the moviepy path and makes
this module trivially unit-testable (no moviepy / ImageMagick import).

ASS reference notes:
  - Colours are &HAABBGGRR (alpha first, then blue/green/red). Alpha 00
    is fully opaque, FF fully transparent. White=&H00FFFFFF,
    black=&H00000000.
  - Alignment uses the numpad layout: 5 = middle-center (matches the
    legacy lyrics, which are vertically+horizontally centered;
    base_y = (height - text_h)//2 in _make_text_clip).
  - Timestamps are H:MM:SS.cs (centiseconds, two digits).
  - \\fad(in_ms,out_ms) is the per-line fade; omit for hard cuts.
  - \\fs<n> overrides the per-line font size (legacy sizes lines by
    character count, so size varies line to line).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AssLine:
    """One timed lyric line, with its already-computed style values.

    start_s / end_s already include any perceptual fade offset the
    caller wants (we do not re-derive it here — parity logic lives in
    pipeline). text is the final display string (case transform already
    applied).

    Layout overrides (None/0 → centered default):
      pos: (x, y) as 0..1 fractions of the frame for the line's anchor
           (the anchor corner is set by `alignment`; default \\an5 = center).
      rot: degrees CLOCKWISE (preview/CSS convention). ASS \\frz is
           counter-clockwise-positive, so build_ass emits -rot.

    Per-line style overrides (used by the title card, which mixes fonts,
    weights and alignment with the centered lyric lines):
      font_name: libass family to switch to via \\fn (must be in fontsdir).
      bold: True/False to force weight via \\b1/\\b0 (None → inherit style).
      alignment: numpad \\an override (e.g. 4 = left-middle for the badge).
      primary_alpha: fill transparency 0..255 (0 = opaque) → \\1a, mirrors
           the moviepy title card's per-line base opacity.

    Animation overrides (the lyric-animation templates — all rendered by
    libass inside the single ffmpeg pass, never moviepy):
      animation: "none"|"karaoke"|"word_reveal"|"pop"|"glow". The line-level
           templates (pop/glow) need no extra data; the word-level ones
           (karaoke/word_reveal) consume `words`.
      words: per-word timing for this line as
           [{"word": str, "start": float, "end": float}, ...] in ABSOLUTE
           seconds (already case-transformed). None → the word-level
           templates degrade to their line-level fallback (see build_ass)."""
    text: str
    start_s: float
    end_s: float
    fontsize: int
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    pos: tuple | None = None
    rot: float = 0.0
    font_name: str | None = None
    bold: bool | None = None
    alignment: int | None = None
    primary_alpha: int = 0
    animation: str = "none"
    words: list | None = None
    # Line-to-line MOTION transition (orthogonal to `animation`, composes with
    # it): "none"|"slide_up"|"slide_side"|"wipe"|"dissolve_blur". Uses
    # position / clip / blur (\\move, \\clip, \\blur) so it never collides with
    # the animation's scale/colour/per-word tags.
    transition: str = "none"


# --- Style tiers (single source of truth, mirrored by pipeline) ---------
#
# These mirror the legacy sizing/fade logic in pipeline._make_text_clip
# exactly. Keeping them here (pure, testable) lets BOTH the moviepy path
# and the ASS path compute identical values, so swapping engines never
# shifts the look. pipeline._make_text_clip is refactored to call these.

_FADE_DURATIONS_S = {"fade": 0.15, "fade_slow": 0.30}


def lyric_fontsize(text_len: int, scale: float, font_scale: float = 1.0) -> int:
    """Font size for a lyric line, by character count (legacy tiers).

    Mirrors _make_text_clip: >80 chars → 55, >50 → 70, else 85 (each
    multiplied by spec.text_scale), then the user's font_scale (clamped
    0.6-1.5), floored at 18px."""
    font_scale = max(0.6, min(1.5, float(font_scale or 1.0)))
    if text_len > 80:
        base = 55
    elif text_len > 50:
        base = 70
    else:
        base = 85
    base_fontsize = int(round(base * scale))
    return max(18, int(round(base_fontsize * font_scale)))


def fade_seconds(lyric_transition: str, seg_duration: float) -> float:
    """Fade in/out duration in seconds, capped at 1/3 of the segment so
    short lines don't fully dissolve. Cuts → 0."""
    fade = _FADE_DURATIONS_S.get(lyric_transition, 0.0)
    return min(fade, max(0.0, seg_duration) / 3)


def perceptual_start(seg_start: float, fade_dur: float) -> float:
    """Shift the visual onset earlier by half the fade so the perceived
    'appear' moment (~50% opacity) lands on the operator's anchored
    timestamp. Mirrors _make_text_clip's fade_perceptual_offset."""
    return max(0.0, seg_start - fade_dur / 2.0)


def moviepy_line_placement(line_pos, clip_w, clip_h, frame_w, frame_h, dx=0, dy=0):
    """Top-left (x, y) so a clip of size (clip_w, clip_h) is CENTERED on the
    per-line position, for the moviepy render path.

    line_pos is (x, y) as 0..1 fractions of the frame (the line's center),
    matching build_ass's `\\pos` mapping (px = pos.x * width). None → frame
    center, i.e. the legacy centered behavior. dx/dy add a screen-space
    offset (the drop-shadow displacement). Pure (no moviepy) so it's unit-
    testable; the rotation itself stays in pipeline (moviepy clip.rotate)."""
    cx = (line_pos[0] if line_pos else 0.5) * frame_w
    cy = (line_pos[1] if line_pos else 0.5) * frame_h
    return (cx - clip_w / 2 + dx, cy - clip_h / 2 + dy)


def font_family(font_path: str) -> tuple[str, bool]:
    """Resolve a .ttf path to its libass-matchable (family_name, is_bold).

    libass matches fonts by family NAME + weight, not by file path — so
    the moviepy path (which hands ImageMagick the file directly) and the
    ASS path must agree on the family name or libass silently falls back
    to a default font (visible parity break).

    We read the font's own name table via Pillow (already a dependency).
    is_bold is derived from the file's actual style/family string (not
    forced) so libass requests the weight the file really is: e.g.
    Oswald-Bold → ("Oswald", True); Anton-Regular → ("Anton", False),
    which avoids libass synthesising fake-bold on a display face that is
    already heavy by design.

    Pair this with a single-font fontsdir (see single_font_dir) so libass
    has exactly one candidate and can't mis-match across the pool.
    """
    from PIL import ImageFont
    f = ImageFont.truetype(font_path, size=16)
    family, style = f.getname()
    blob = f"{family} {style}".lower()
    is_bold = any(w in blob for w in ("bold", "black", "heavy"))
    return family, is_bold


def single_font_dir(font_path: str) -> str:
    """Copy one font into a fresh temp dir and return it, so the ffmpeg
    `subtitles` filter can point `fontsdir` at a directory with exactly
    one font — removing any family-matching ambiguity in libass.

    Caller owns cleanup of the returned dir."""
    import os
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="ass_fonts_")
    shutil.copy2(font_path, os.path.join(d, os.path.basename(font_path)))
    return d


def multi_font_dir(font_paths: list[str]) -> str:
    """Like single_font_dir but for several fonts (the lyric font + the
    title-card artist font). libass discovers all of them by family name,
    so a Dialogue line can \\fn-switch between them. Duplicate basenames
    are de-duped (same file copied once). Caller owns cleanup."""
    import os
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="ass_fonts_")
    seen: set[str] = set()
    for p in font_paths:
        if not p:
            continue
        name = os.path.basename(p)
        if name in seen or not os.path.exists(p):
            continue
        shutil.copy2(p, os.path.join(d, name))
        seen.add(name)
    return d


def _opacity_to_alpha(opacity: float) -> int:
    """ASS \\1a is transparency: 0 opaque, 255 fully transparent — the
    inverse of moviepy's 0..1 opacity. round((1-op)*255), clamped."""
    return max(0, min(255, int(round((1.0 - float(opacity)) * 255))))


def _balanced_wrap(words: list[str], width100, max_lines: int) -> list[str]:
    """Greedily wrap `words` into up to `max_lines` lines, minimising the
    width of the widest line. `width100` measures a string's width at the
    reference size (100px). Only 2 lines are used in practice (the title
    card never needs more); higher max_lines just relaxes the cap."""
    if max_lines <= 1 or len(words) < 2:
        return [" ".join(words)]
    best_break, best_cost = 1, None
    for i in range(1, len(words)):
        left, right = " ".join(words[:i]), " ".join(words[i:])
        cost = max(width100(left), width100(right))
        if best_cost is None or cost < best_cost:
            best_cost, best_break = cost, i
    return [" ".join(words[:best_break]), " ".join(words[best_break:])]


def fit_title_text(
    text: str,
    font_path: str | None,
    base_size: int,
    max_width: float,
    min_size: int,
    *,
    max_lines: int = 2,
) -> tuple[list[str], int]:
    """Pick a font size and (if needed) wrap `text` so it fits `max_width`.

    "Shrink, then wrap": first shrink the size from base_size toward min_size
    to fit the whole text on ONE line; if it still doesn't fit at min_size,
    wrap greedily into up to max_lines balanced lines and pick the largest
    size in [min_size, base_size] whose widest wrapped line fits.

    Returns (lines, fontsize). With no font_path (parity callers / tests) or
    when PIL can't load the font, returns ([text], base_size) unchanged —
    preserving the legacy fixed-size behaviour so nothing regresses.

    Width is measured once at a 100px reference and scaled (text width grows
    ~linearly with font size), so we never re-rasterise per candidate size.
    """
    text = (text or "").strip()
    base_size = max(int(min_size), int(base_size))
    if not text:
        return [], base_size
    if not font_path or max_width <= 0:
        return [text], base_size
    try:
        from PIL import ImageFont
        ref = ImageFont.truetype(font_path, 100)
    except Exception:
        return [text], base_size

    def w100(s: str) -> float:
        try:
            return float(ref.getlength(s))
        except Exception:
            bbox = ref.getbbox(s)
            return float(bbox[2] - bbox[0])

    # 1) Largest size in [min_size, base_size] that fits on ONE line.
    one_w = w100(text)
    if one_w <= 0:
        return [text], base_size
    fit_size = int(max_width * 100.0 / one_w)
    if fit_size >= base_size:
        return [text], base_size
    if fit_size >= min_size:
        return [text], fit_size

    # 2) Doesn't fit even at min_size → wrap (if there's a space to break on).
    words = text.split()
    if len(words) < 2:
        return [text], min_size           # single long word: just clamp
    wrapped = _balanced_wrap(words, w100, max_lines)
    widest = max((w100(ln) for ln in wrapped), default=one_w)
    size = base_size
    if widest > 0:
        size = max(min_size, min(base_size, int(max_width * 100.0 / widest)))
    return wrapped, size


def title_card_lines(
    artist: str,
    song: str,
    first_lyric_start: float,
    *,
    width: int,
    height: int,
    text_scale: float,
    lyric_font_family: str,
    artist_font_family: str,
    lyric_font_path: str | None = None,
    artist_font_path: str | None = None,
) -> list[AssLine]:
    """Build the artist/song title-card overlay as ASS lines, mirroring the
    moviepy title card (pipeline.generate_lyric_video) so the look matches
    when the libass fast path renders it.

    Two layouts, picked by how much instrumental intro precedes the first
    sung line (same 0.8 s threshold as moviepy):
      - LONG intro: centred card, artist in ExtraBold over the song title,
        fades in/out, visible 0.3 s → min(first_lyric-0.2, 8.0 s).
      - SHORT intro: compact lower-left badge, visible 0.3 s → 6.3 s.

    Sizes/timings/fades mirror pipeline.py:6744-6842. Line heights are
    approximated as fontsize*1.2 per rendered line for vertical stacking
    (libass has no measure step); the moviepy card isn't pixel-locked either,
    so this stays visually faithful.

    Text is NFC-normalised first (the same as the lyric lines via
    _clean_display_text) so decomposed accents from macOS filenames — e.g.
    "Así" as 'i'+combining-acute — don't render as a detached floating mark.

    When font paths are supplied, long titles/artists are shrunk (and, only
    if they still don't fit, wrapped into 2 lines) to the card's safe width
    instead of overflowing the frame. With no paths (parity tests) the legacy
    fixed sizing is kept unchanged.
    """
    import unicodedata
    artist_u = unicodedata.normalize("NFC", (artist or "").strip()).upper()
    song_d = unicodedata.normalize("NFC", (song or "").strip())
    if not artist_u and not song_d:
        return []

    START_T = 0.3
    has_long_intro = first_lyric_start > START_T + 0.5

    if has_long_intro:
        # Hero title card: artist is the prominent line (bigger than the 85px
        # lyric tier), song title secondary. Bumped 2026-05 — the old 62/46
        # read smaller than the lyrics, which felt wrong for the intro moment.
        artist_size = max(30, int(round(100 * text_scale)))
        title_size = max(24, int(round(62 * text_scale)))
        title_end = min(first_lyric_start - 0.2, START_T + 8.0)
        clip_dur = max(0.1, title_end - START_T)
        fade_in = min(0.4, max(0.1, clip_dur * 0.25))
        fade_out = min(0.7, max(0.1, clip_dur * 0.35))
        alignment = 5            # centred
        op_artist, op_song = 0.97, 0.85
        card_w_frac = 0.80       # centred card: 80% of frame width (matches moviepy)
    else:
        artist_size = max(20, int(round(36 * text_scale)))
        title_size = max(16, int(round(28 * text_scale)))
        title_end = START_T + 6.0
        clip_dur = title_end - START_T
        fade_in, fade_out = 0.4, 0.8
        alignment = 4            # left-middle (lower-left badge)
        op_artist, op_song = 0.92, 0.80
        card_w_frac = 0.45       # lower-left badge: narrower (matches moviepy)

    # Shrink (then wrap) each line to the card's safe width so long titles /
    # artist names don't overflow the frame. min_size = 62% of the base tier,
    # floored at 16px. With no font path supplied this is a no-op (returns the
    # single line at base_size) so the parity callers/tests keep the old look.
    max_w = width * card_w_frac

    def _fit(text: str, base: int, font_path):
        return fit_title_text(
            text, font_path, base, max_w, max(16, int(round(base * 0.62))),
        )

    # Stack the present lines and compute each one's vertical CENTER as a
    # fraction of the frame. Approximate line height = 1.2 * fontsize PER
    # wrapped line, with the same 8 px gap moviepy uses between artist and
    # title. Each entry carries its (possibly wrapped) text, fitted size,
    # font, weight, opacity, and rendered line count.
    # Wrapped lines are joined with a real newline; build_ass's _ass_escape
    # turns "\n" into the ASS hard break "\N" (and escapes any braces). We
    # must NOT pre-insert a literal "\N" here — _ass_escape escapes the
    # backslash first, which would print a literal "\N" instead of breaking.
    stack: list[tuple[str, int, str, bool, float, int]] = []
    if artist_u:
        a_lines, a_size = _fit(artist_u, artist_size, artist_font_path)
        stack.append(("\n".join(a_lines), a_size,
                      artist_font_family, True, op_artist, len(a_lines)))
    if song_d:
        s_lines, s_size = _fit(song_d, title_size, lyric_font_path)
        stack.append(("\n".join(s_lines), s_size,
                      lyric_font_family, False, op_song, len(s_lines)))
    if not stack:
        return []

    line_hs = [1.2 * size * nlines for _, size, _, _, _, nlines in stack]
    gap = 8.0
    total_h = sum(line_hs) + gap * (len(stack) - 1)

    if alignment == 5:
        top = (height - total_h) / 2.0
    else:
        # bottom-anchored badge: 8% bottom safe-area margin
        bottom_margin = height * 0.08
        top = height - bottom_margin - total_h

    if alignment == 5:
        x_frac = 0.5
    else:
        x_frac = (width * 0.06) / width  # 6% left margin

    lines: list[AssLine] = []
    cursor = top
    fade_in_ms = int(round(fade_in * 1000))
    fade_out_ms = int(round(fade_out * 1000))
    for (txt, size, fam, bold, opacity, _nlines), lh in zip(stack, line_hs):
        center_y = cursor + lh / 2.0
        lines.append(AssLine(
            text=txt,
            start_s=START_T,
            end_s=title_end,
            fontsize=size,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
            pos=(x_frac, center_y / height),
            font_name=fam,
            bold=bold,
            alignment=alignment,
            primary_alpha=_opacity_to_alpha(opacity),
        ))
        cursor += lh + gap
    return lines


def _clean_display_text(text: str, case_fn) -> str:
    """Apply the case transform + the same sanitisation _make_text_clip
    does before handing text to the renderer (NFC normalise, drop chars
    that break the text engine). Returns "" for blank lines so the
    caller can skip them."""
    import unicodedata
    cased = case_fn(text) if case_fn else text
    out = unicodedata.normalize("NFC", cased)
    out = out.replace("@", "").replace("`", "'").replace("\x00", "")
    return out if out.strip() else ""


# Templates that paint/animate the line word-by-word; they need per-word
# timing (real if available, otherwise synthesized from the line window).
_WORD_LEVEL_ANIMATIONS = ("karaoke", "word_reveal")


def _strip_for_match(tok: str) -> str:
    """Lowercase + drop non-alphanumerics, for comparing a real word array
    against the (possibly edited) display string token-for-token."""
    import unicodedata
    out = unicodedata.normalize("NFC", tok).lower()
    return "".join(ch for ch in out if ch.isalnum())


def _word_timings(
    display: str,
    seg_start: float,
    seg_end: float,
    raw_words: list | None,
) -> list[dict]:
    """Per-word timing for a lyric line, in ABSOLUTE seconds, case-applied.

    Tries the real Whisper/aligner `raw_words` first, but ONLY if it
    reconstructs the display tokens (guards against stale `words` left over
    after the operator edited the line text in the visual editor). When the
    real data is missing or inconsistent — the common production case, since
    main.py strips `words` from segments_json — we SYNTHESIZE the timing by
    splitting the line window proportional to each token's length. This keeps
    karaoke / word_reveal working on every song with a smooth, believable
    sweep instead of falling back to a different template.

    Returns [] only for an empty/zero-length line (caller skips it)."""
    tokens = [t for t in (display or "").split() if t]
    if not tokens:
        return []
    seg_start = float(seg_start)
    seg_end = max(seg_start + 0.05, float(seg_end))

    # --- Try real word data, validated against the display tokens ----------
    if isinstance(raw_words, list) and len(raw_words) == len(tokens):
        cand = []
        ok = True
        for tok, rw in zip(tokens, raw_words):
            if not isinstance(rw, dict) or "start" not in rw or "end" not in rw:
                ok = False
                break
            if _strip_for_match(str(rw.get("word", ""))) != _strip_for_match(tok):
                ok = False
                break
            ws = max(seg_start, float(rw["start"]))
            we = min(seg_end, float(rw["end"]))
            if we <= ws:
                we = min(seg_end, ws + 0.05)
            cand.append({"word": tok, "start": ws, "end": we})
        if ok:
            # enforce monotonic non-overlap
            for i in range(1, len(cand)):
                if cand[i]["start"] < cand[i - 1]["end"]:
                    cand[i]["start"] = cand[i - 1]["end"]
                if cand[i]["end"] <= cand[i]["start"]:
                    cand[i]["end"] = min(seg_end, cand[i]["start"] + 0.05)
            return cand

    # --- Synthesize: split the window proportional to token length ---------
    weights = [len(t) + 1 for t in tokens]  # +1 so 1-char words still get time
    total_w = sum(weights)
    span = seg_end - seg_start
    out = []
    cursor = seg_start
    for tok, w in zip(tokens, weights):
        dur = span * (w / total_w)
        out.append({"word": tok, "start": cursor, "end": cursor + dur})
        cursor += dur
    out[-1]["end"] = seg_end  # absorb rounding into the last word
    return out


def segments_to_lines(
    segments: list[dict],
    *,
    text_scale: float,
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    animation: str = "none",
    transition: str = "none",
    case_fn=None,
) -> list[AssLine]:
    """Convert segments_json into ASS lines with parity to the moviepy
    path: same case/sanitise, same fontsize tiers (by cleaned-text
    length), same fade durations + perceptual onset offset.

    case_fn: optional callable applied to each line's raw text (pipeline
    passes _apply_case; tests pass None for identity)."""
    lines: list[AssLine] = []
    for seg in segments:
        raw = seg.get("text", "")
        display = _clean_display_text(raw, case_fn)
        if not display:
            continue
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        seg_dur = max(0.1, seg_end - seg_start)
        fontsize = lyric_fontsize(len(display), text_scale, font_scale)
        # Per-line layout overrides (operator-set in the preview).
        scale = seg.get("scale")
        if isinstance(scale, (int, float)) and scale > 0:
            fontsize = max(8, int(round(fontsize * scale)))
        pos = None
        _p = seg.get("pos")
        if isinstance(_p, dict) and "x" in _p and "y" in _p:
            pos = (float(_p["x"]), float(_p["y"]))
        rot = seg.get("rot")
        rot = float(rot) if isinstance(rot, (int, float)) else 0.0
        fade_dur = fade_seconds(lyric_transition, seg_dur)
        fade_ms = int(round(fade_dur * 1000))
        words = None
        if animation in _WORD_LEVEL_ANIMATIONS:
            # Real timing if it survives the token-match guard, else synthesize
            # from the line window so the sweep works on every song.
            words = _word_timings(display, seg_start, seg_end, seg.get("words"))
        lines.append(AssLine(
            text=display,
            start_s=perceptual_start(seg_start, fade_dur),
            end_s=seg_end,
            fontsize=fontsize,
            fade_in_ms=fade_ms,
            fade_out_ms=fade_ms,
            pos=pos,
            rot=rot,
            animation=animation,
            words=words,
            transition=transition,
        ))
    return lines


def _ass_time(seconds: float) -> str:
    """Format seconds as ASS H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    """Escape a display string for an ASS Dialogue Text field.

    Newlines become hard line breaks (\\N). Braces would open an
    override block, so they are neutralised. We keep it minimal —
    the caller already sanitised case / control chars."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
        .replace("\r", "\\N")
    )


def hex_to_ass(hex_color: str, *, fallback: str = "&H00FFFFFF") -> str:
    """Convert a #RRGGBB hex color to ASS &HAABBGGRR& format.

    ASS uses a big-endian uint32 wrapped in &H...& with byte order
    AA (alpha, 0=opaque) BB GG RR. The leading 0x00 alpha keeps the
    color fully opaque (the only mode the rest of the pipeline assumes).

    Malformed input (missing #, wrong length, non-hex chars) returns
    `fallback` so a stray operator value can't poison the ASS document
    or cause libass to crash mid-render. Default fallback is opaque
    white — matches the historical hardcoded PrimaryColour.

    Examples:
        hex_to_ass("#FF0000")  -> "&H000000FF"  (red)
        hex_to_ass("#19E0BC")  -> "&H00BCE019"  (karaoke green)
        hex_to_ass("")         -> "&H00FFFFFF"  (fallback)
        hex_to_ass("nope")     -> "&H00FFFFFF"  (fallback)
    """
    if not isinstance(hex_color, str):
        return fallback
    s = hex_color.strip()
    if not s.startswith("#") or len(s) != 7:
        return fallback
    try:
        r = int(s[1:3], 16)
        g = int(s[3:5], 16)
        b = int(s[5:7], 16)
    except ValueError:
        return fallback
    return f"&H00{b:02X}{g:02X}{r:02X}"


def build_ass(
    *,
    width: int,
    height: int,
    font_name: str,
    base_fontsize: int,
    outline: float,
    shadow: float,
    lines: list[AssLine],
    margin_v: int = 0,
    alignment: int = 5,
    bold: bool = True,
    # Lyric text colors 2026-05-25. Hex #RRGGBB; "" → blanco default.
    # primary_color: el color principal (palabra cantada en karaoke, texto
    # único en none/pop/glow/word_reveal). secondary_color: solo karaoke
    # = palabra no-cantada. Si vienen vacíos, mantenemos los defaults
    # históricos (blanco/rojo) para no romper jobs sin estos params.
    primary_color: str = "",
    secondary_color: str = "",
) -> str:
    """Build a complete ASS document for the lyric overlay.

    width/height  : PlayRes, must match the render frame size so libass
                    positions and scales correctly.
    font_name     : libass font family name (must be discoverable via the
                    ffmpeg `fontsdir` passed alongside the subtitles
                    filter).
    base_fontsize : style default; per-line AssLine.fontsize overrides it
                    inline via \\fs (legacy sizes lines by length).
    outline/shadow: border and drop-shadow widths in pixels (the legacy
                    stroke_width and shadow_offset).
    alignment     : numpad alignment; 5 = middle-center (legacy default).
    margin_v      : vertical margin in px (0 for centered \\an5).
    """
    # Style colors: black outline + black shadow constant; primary +
    # secondary se resuelven via hex_to_ass con fallback al default
    # histórico (blanco / rojo). El override per-line de karaoke
    # (overrides `\2c...\1c...` en _animated_word_payload) usa los mismos
    # colores cuando vienen seteados; si vienen "", mantiene el grey
    # hardcoded para no cambiar look de jobs sin colores.
    primary_ass = hex_to_ass(primary_color, fallback="&H00FFFFFF")
    secondary_ass = hex_to_ass(secondary_color, fallback="&H000000FF")
    style = (
        "Style: Lyric,{font},{fs},"
        "{primary},{secondary},&H00000000,&H80000000,"  # primary, secondary, outline, back(shadow, 50% alpha)
        "{bold},0,0,0,"      # bold (-1 true / 0 false), italic, underline, strikeout
        "100,100,0,0,"       # scaleX, scaleY, spacing, angle
        "1,{bord},{shad},"   # BorderStyle=1, Outline, Shadow
        "{align},20,20,{mv},1"  # alignment, marginL, marginR, marginV, encoding
    ).format(
        font=font_name,
        fs=base_fontsize,
        primary=primary_ass,
        secondary=secondary_ass,
        bold=-1 if bold else 0,
        bord=_fmt_num(outline),
        shad=_fmt_num(shadow),
        align=alignment,
        mv=margin_v,
    )

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {int(width)}",
            f"PlayResY: {int(height)}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            style,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text",
        ]
    )

    events = []
    for ln in lines:
        if not ln.text or not ln.text.strip():
            continue
        if ln.end_s <= ln.start_s:
            continue
        overrides = f"\\fs{int(ln.fontsize)}"
        if ln.font_name:
            overrides += f"\\fn{ln.font_name}"
        if ln.bold is not None:
            overrides += "\\b1" if ln.bold else "\\b0"
        # Resolve the line's anchor (px,py) + alignment. Slide transitions need
        # an explicit position to animate from, so compute the center default
        # even when no per-line \pos override was set.
        trans = ln.transition or "none"
        _dur_ms = max(1, int(round((ln.end_s - ln.start_s) * 1000)))
        _enter_ms = min(380, max(140, int(_dur_ms * 0.33)))
        _an = ln.alignment if ln.alignment else 5
        if ln.pos is not None:
            # The line is anchored by `alignment` (numpad) at \pos. Default
            # \an5 anchors by CENTER (matches the preview's
            # translate(-50%,-50%) + the frac→px mapping); the title card
            # overrides to e.g. \an4 for the left-aligned badge.
            _px = int(round(ln.pos[0] * width))
            _py = int(round(ln.pos[1] * height))
        else:
            _px, _py = width // 2, height // 2
        if trans in ("slide_up", "slide_side"):
            if trans == "slide_up":
                _off = max(24, int(round(height * 0.07)))
                _fx, _fy = _px, _py + _off          # enter from below, rise to place
            else:
                _off = max(40, int(round(width * 0.12)))
                _fx, _fy = _px - _off, _py           # enter from the left
            overrides += f"\\an{int(_an)}\\move({_fx},{_fy},{_px},{_py},0,{_enter_ms})"
        elif ln.pos is not None:
            overrides += f"\\an{int(_an)}\\pos({_px},{_py})"
        elif ln.alignment:
            overrides += f"\\an{int(ln.alignment)}"
        if ln.primary_alpha:
            overrides += f"\\1a&H{int(ln.primary_alpha):02X}&"
        if ln.rot:
            # CSS clockwise → ASS \frz is counter-clockwise-positive.
            overrides += f"\\frz{_fmt_num(-ln.rot)}"

        # --- Lyric-animation template (libass tags, single-pass, no moviepy) -
        anim = ln.animation or "none"
        # Word-level templates only animate per-word when we actually have
        # timing (segments_to_lines synthesizes it, so this is normally true).
        word_anim = anim in _WORD_LEVEL_ANIMATIONS and bool(ln.words)
        fade_in_ms, fade_out_ms = ln.fade_in_ms, ln.fade_out_ms
        if anim == "pop":
            # Scale-in with a small overshoot/settle. Capped at 116% and
            # settled by 210 ms so it never throws the reader off the line.
            overrides += ("\\fscx116\\fscy116\\t(0,120,\\fscx96\\fscy96)"
                          "\\t(120,210,\\fscx100\\fscy100)")
        elif anim == "glow":
            # One soft breathe of blur + outline, then hold. The black
            # OUTLINE never drops below the base width → legibility intact.
            _b = _fmt_num(outline)
            _b2 = _fmt_num(outline + 1.5)
            overrides += (f"\\bord{_b}\\blur2\\t(0,700,\\blur6\\bord{_b2})"
                          f"\\t(700,1400,\\blur2\\bord{_b})")
        elif anim == "karaoke" and word_anim:
            # Un-sung = SecondaryColour, sung = PrimaryColour; \kf sweeps
            # the fill between them. Outline stays black/full throughout.
            # Si el operador picó colores, usamos los suyos (override
            # inline). Sino, default histórico: un-sung grey + sung white.
            _karaoke_secondary = hex_to_ass(secondary_color, fallback="&H00808080")
            _karaoke_primary = hex_to_ass(primary_color, fallback="&H00FFFFFF")
            overrides += f"\\2c{_karaoke_secondary}&\\1c{_karaoke_primary}&"
        elif anim == "word_reveal" and word_anim:
            # The per-word reveal IS the entrance AND exit (words appear and
            # leave one by one), so drop BOTH line fades — they'd fight the
            # per-word \alpha.
            fade_in_ms = fade_out_ms = 0

        # --- Line-to-line MOTION transition (composes with the animation) ----
        if trans == "wipe":
            # Curtain reveal: clip width grows left→right over the enter window.
            overrides += (f"\\clip(0,0,0,{height})"
                          f"\\t(0,{_enter_ms},\\clip(0,0,{width},{height}))")
        elif trans == "dissolve_blur":
            # Enter out of focus and resolve, then blur back out near the end.
            _exit_ms = min(380, max(140, int(_dur_ms * 0.30)))
            overrides += (f"\\blur8\\t(0,{_enter_ms},\\blur0)"
                          f"\\t({_dur_ms - _exit_ms},{_dur_ms},\\blur8)")

        if fade_in_ms > 0 or fade_out_ms > 0:
            overrides += f"\\fad({int(fade_in_ms)},{int(fade_out_ms)})"

        if word_anim:
            inner = _animated_word_payload(anim, ln)
        else:
            inner = _ass_escape(ln.text)
        text = "{" + overrides + "}" + inner
        events.append(
            "Dialogue: 0,{start},{end},Lyric,,0,0,0,,{text}".format(
                start=_ass_time(ln.start_s),
                end=_ass_time(ln.end_s),
                text=text,
            )
        )

    return header + "\n" + "\n".join(events) + "\n"


def _animated_word_payload(animation: str, ln: AssLine) -> str:
    """Build the mid-line, per-word override payload for the word-level
    templates. Each word's display text is ASS-escaped FIRST, then wrapped in
    override braces that WE emit (so _ass_escape never sees — and never
    corrupts — our control braces). Words are space-separated; the spaces sit
    outside the override blocks and render literally.

    Timings are relative to the line's Dialogue start (ln.start_s), the same
    reference libass uses for \\k and \\t.
    """
    line_start = float(ln.start_s)
    parts: list[str] = []

    if animation == "word_reveal":
        # Each word fades IN at its onset, and (when the line is long enough)
        # fades OUT one by one near the end — "words appear and leave one at a
        # time". Both are per-word \alpha so the line carries no \fad.
        REVEAL_MS = 120
        words = ln.words or []
        n = len(words)
        line_dur = max(1, int(round((float(ln.end_s) - line_start) * 1000)))
        # Only stagger the exit if there's room after the last reveal.
        last_reveal_end = 0
        if words:
            last_reveal_end = max(0, int(round((float(words[-1]["start"]) - line_start) * 1000))) + REVEAL_MS
        exit_span = min(int(line_dur * 0.45), 160 * max(1, n))
        do_exit = line_dur - exit_span > last_reveal_end + 150
        exit_start = line_dur - exit_span
        exit_step = exit_span / max(1, n)
        for i, w in enumerate(words):
            t0 = max(0, int(round((float(w["start"]) - line_start) * 1000)))
            t1 = t0 + REVEAL_MS
            ov = "\\alpha&HFF&\\t(%d,%d,\\alpha&H00&)" % (t0, t1)
            if do_exit:
                ex0 = int(exit_start + i * exit_step)
                ex1 = min(line_dur, ex0 + REVEAL_MS)
                ov += "\\t(%d,%d,\\alpha&HFF&)" % (ex0, ex1)
            parts.append("{%s}%s" % (ov, _ass_escape(str(w.get("word", "")))))
        return " ".join(parts)

    # karaoke: \kf<cs> fill sweep. Each chunk's duration carries the fill from
    # the previous word's end to this word's end (cumulative, relative to the
    # line start), so the sweep is continuous and lands on each word's real
    # finish time. Centiseconds, floored at 1.
    prev_end = line_start
    for w in ln.words or []:
        cs = int(round((float(w["end"]) - prev_end) * 100))
        if cs < 1:
            cs = 1
        parts.append("{\\kf%d}%s" % (cs, _ass_escape(str(w.get("word", "")))))
        prev_end = float(w["end"])
    return " ".join(parts)


def _fmt_num(value: float) -> str:
    """ASS accepts ints or floats; emit a clean compact number."""
    f = float(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")
