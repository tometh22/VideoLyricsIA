// Pretty-print a song filename for display in the lyrics editor header.
//
// MP3/WAV uploads land with the operator's raw filesystem name —
// underscores, dashes, ALL CAPS, ALL Title Case, mixed separators.
// In the wizard step 6 review screen this name shows as "el árbol de
// la vida" verbatim, and that's the first touchpoint where the
// operator should feel the system understands their material.
//
// We do not try to restore accents (that needs a dictionary lookup we
// don't ship). We just clean up the structural noise:
//   * strip the audio extension
//   * collapse `_` separators to em-dashes (where they look intentional)
//     or to spaces (where they replace an actual space)
//   * normalize ` - ` to ` — ` (em-dash) so the artist/title separator
//     reads as a proper title separator
//   * title-case with Spanish/Portuguese-aware lowercase exceptions
//
// Returns the cleaned string. If parsing breaks for any reason, returns
// the input unchanged so the operator never sees an empty/garbled
// header.

const AUDIO_EXT_RE = /\.(mp3|wav|m4a|flac|ogg|opus|aac|wma)$/i;

// Words that stay lowercase mid-title in Spanish/Portuguese. NOT
// lowercased at the start of a part — that's handled separately.
const LOWERCASE_MID = new Set([
  // Spanish
  "de", "del", "la", "las", "el", "los", "y", "o", "a", "en", "con",
  "por", "sin", "para", "un", "una", "unos", "unas",
  // Portuguese
  "do", "da", "dos", "das", "e", "ao", "à",
  // English (the most common ones; full list lives elsewhere if needed)
  "the", "of", "and", "to", "in", "on", "at", "for",
]);

function titleCasePart(part) {
  // Split on whitespace only — keep apostrophes / dashes inside words
  // intact (e.g. "Don't" stays "Don't", "Rock-and-Roll" stays so).
  const words = part.trim().split(/\s+/);
  return words
    .map((w, i) => {
      if (!w) return w;
      const lower = w.toLowerCase();
      // First word of a part always capitalizes
      if (i === 0) return lower.charAt(0).toUpperCase() + lower.slice(1);
      // Stop-words stay lowercase mid-title
      if (LOWERCASE_MID.has(lower)) return lower;
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(" ");
}

/**
 * @param {string} input — raw filename or song identifier
 * @returns {string} cleaned, title-cased name. Empty input → "".
 */
export function prettifySongTitle(input) {
  if (input == null) return "";
  let s = String(input).trim();
  if (!s) return "";

  try {
    // 1. Strip audio extension
    s = s.replace(AUDIO_EXT_RE, "");

    // 2. ` _ ` (space-underscore-space) is the most common "section
    //    separator" pattern observed in operator uploads — convert to
    //    em-dash so it reads as a deliberate division.
    s = s.replace(/\s+_\s+/g, " — ");

    // 3. Remaining `_` are stand-ins for spaces.
    s = s.replace(/_/g, " ");

    // 4. ` - ` (space-dash-space) is the conventional artist/title
    //    separator. Promote it to em-dash for visual consistency with
    //    step 2 — same separator throughout the string.
    s = s.replace(/\s+-\s+/g, " — ");

    // 5. Collapse repeated whitespace.
    s = s.replace(/\s{2,}/g, " ").trim();

    // 6. Title-case each em-dash-separated segment independently.
    const parts = s.split(" — ").map(titleCasePart);
    const out = parts.join(" — ");

    // Guard against the cleanup producing an empty string (e.g. input
    // was only separators). Fall back to the input unchanged.
    return out || input;
  } catch {
    // Any unexpected exception → return input verbatim. Better a raw
    // filename than a crash in the header.
    return input;
  }
}

export default prettifySongTitle;
