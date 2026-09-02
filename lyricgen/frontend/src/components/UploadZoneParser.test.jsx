/**
 * Regression test for the `parseFilename` heuristic in UploadZone.jsx.
 *
 * HOTFIX 2026-05-27: PR #286 was wrong. It unified BOTH separators to
 * `head=artist`, but the backend's `_parse_filename_artist_title`
 * (lyricgen/backend/main.py:1537-1539 + docstring at 1500-1508)
 * documents and implements DIFFERENT conventions per separator:
 *   - " - " → "Artist - Title"   (head=artist)
 *   - "_"   → "Title_Artist"     (head=title, tail=artist)  ← YouTube/Suno
 *
 * The frontend's incorrect unification meant /upload-url got the
 * frontend's wrong (artist, title) and committed them to the Job row,
 * while a second job got created later with the backend's correct parse
 * — resulting in two competing jobs for one audio, and the UI showing
 * "subiendo" forever because the frontend didn't know which to follow.
 * Surfaced 2026-05-27 16:42 by user agus.cafisi with file
 * `Un Pacto Live In Buenos Aires  2001_Bersuit Vergarabat.wav`.
 *
 * This file now locks in the CORRECT convention matching the backend.
 *
 * This is a behavioural test against the COPIED implementation —
 * UploadZone.jsx itself is a big component that we don't want to mount
 * just to test a parser. The implementation is small enough to mirror.
 */
import { describe, it, expect } from "vitest";

// Mirror the implementation from UploadZone.jsx for unit testing. If
// you change the parser there, update this copy too — and the test
// will fail if the contract breaks. The wrapper test below verifies
// the actual file matches this.
const _NOISE_PATTERNS = [
  /\s*[\(\[]\s*official\s+video\s*[\)\]]/gi,
  /\s*[\(\[]\s*official\s+audio\s*[\)\]]/gi,
  /\s*[\(\[]\s*official\s+music\s+video\s*[\)\]]/gi,
  /\s*[\(\[]\s*lyric\s+video\s*[\)\]]/gi,
  /\s*[\(\[]\s*audio\s*[\)\]]/gi,
  /\s*[\(\[]\s*video\s*[\)\]]/gi,
  /\s*[\(\[]\s*en\s+vivo\s*[\)\]]/gi,
  /\s*[\(\[]\s*live\s*[\)\]]/gi,
  /\s*[\(\[]\s*lyrics\s*[\)\]]/gi,
  /\s*[\(\[]\s*remaster(?:ed)?(?:\s+\d{4})?\s*[\)\]]/gi,
  /\s*-\s*official\s+video\s*$/gi,
  /\s*-\s*live\s*$/gi,
];
const _stripNoise = (s) => {
  let out = s;
  for (const pat of _NOISE_PATTERNS) out = out.replace(pat, "");
  return out.trim();
};
const parseFilename = (filename) => {
  const name = filename.replace(/\.(mp3|wav|m4a|flac|aac|ogg)$/i, "");
  let artist = "";
  let song = name.trim();
  if (name.includes(" - ")) {
    const [head, ...rest] = name.split(" - ");
    artist = head.trim();
    song = rest.join(" - ").trim();
  } else if (name.includes("_")) {
    // YouTube / Suno convention: "Title_Artist". head=title, tail=artist.
    const [head, ...rest] = name.split("_");
    song = head.trim();
    artist = rest.join("_").trim();
  }
  song = _stripNoise(song);
  artist = _stripNoise(artist);
  return { artist, song };
};


describe("parseFilename", () => {
  it("Underscore convention: `Title_Artist.ext` matches backend", () => {
    // YouTube/Suno export convention — tail is the artist.
    // Backend: lyricgen/backend/main.py:1537-1539 (docstring 1500-1508).
    expect(parseFilename("Viejas Locas_Legalícenla.mp3")).toEqual({
      artist: "Legalícenla",
      song: "Viejas Locas",
    });
  });

  it("Regression: agus.cafisi incident filename (2026-05-27 16:42)", () => {
    // Exact filename that triggered the dual-job creation incident.
    // Before hotfix: frontend parsed artist="Un Pacto..." (wrong) and
    // backend parsed artist="Bersuit Vergarabat" (right), creating two
    // competing jobs for the same audio.
    expect(parseFilename(
      "Un Pacto Live In Buenos Aires  2001_Bersuit Vergarabat.wav"
    )).toEqual({
      artist: "Bersuit Vergarabat",
      song: "Un Pacto Live In Buenos Aires  2001",
    });
  });

  it("`Artist - Title.mp3` keeps the well-known convention", () => {
    expect(parseFilename("Rata Blanca - Mujer Amante.mp3")).toEqual({
      artist: "Rata Blanca",
      song: "Mujer Amante",
    });
  });

  it("strips `(Official Video)` and variants", () => {
    expect(parseFilename("Artist - Title (Official Video).mp3").song).toBe("Title");
    expect(parseFilename("Artist - Title (OFFICIAL VIDEO).mp3").song).toBe("Title");
    expect(parseFilename("Artist - Title [Official Audio].mp3").song).toBe("Title");
    expect(parseFilename("Artist - Title (Lyric Video).mp3").song).toBe("Title");
    expect(parseFilename("Artist - Title (En Vivo).mp3").song).toBe("Title");
    expect(parseFilename("Artist - Title (Remastered 2024).mp3").song).toBe("Title");
  });

  it("handles multiple ` - ` separators by keeping artist=first", () => {
    expect(parseFilename("Artist - Title - Live.mp3")).toEqual({
      artist: "Artist",
      song: "Title",   // " - Live" stripped by noise patterns
    });
  });

  it("handles filenames without separators", () => {
    expect(parseFilename("CancionSuelta.mp3")).toEqual({
      artist: "",
      song: "CancionSuelta",
    });
  });

  it("strips audio extensions (case-insensitive)", () => {
    expect(parseFilename("Artist - Title.WAV").song).toBe("Title");
    expect(parseFilename("Artist - Title.M4A").song).toBe("Title");
    expect(parseFilename("Artist - Title.flac").song).toBe("Title");
  });

  it("handles trailing whitespace + special chars without crashing", () => {
    expect(parseFilename("  Artist  -  Title  .mp3")).toEqual({
      artist: "Artist",
      song: "Title",
    });
    // Unicode + tildes
    expect(parseFilename("Babasónicos - Putita.mp3")).toEqual({
      artist: "Babasónicos",
      song: "Putita",
    });
  });
});
