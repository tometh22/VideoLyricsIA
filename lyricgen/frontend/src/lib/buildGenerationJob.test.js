import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildGenerationJob, buildDirectGenerationJob } from "./buildGenerationJob";
import { normalizeLyricLook, lyricColorForSubmit } from "./lyricLooks";
import { appendLookVariantFields } from "./lyricLookVariants";

describe("buildGenerationJob", () => {
  it("keeps the approved editor selectors and completed background preview", () => {
    const job = buildGenerationJob({
      file: { name: "mi-nina.mp3" },
      artist: "Los Ángeles Negros",
      songTitle: "Mi Niña",
      segments: [{ start: 0, end: 1, text: "una línea" }],
      segmentsRevision: 4,
      editorRevision: 4,
      editorVersionId: "version-4",
      transcribeJobId: "job-123",
      operatorMetrics: { active_edit_ms: 1200 },
      bgCacheKey: "preview-123",
    });

    expect(job).toMatchObject({
      filename: "mi-nina.mp3",
      segmentsRevision: 4,
      editorRevision: 4,
      editorVersionId: "version-4",
      transcribeJobId: "job-123",
      operatorMetrics: { active_edit_ms: 1200 },
      bgCacheKey: "preview-123",
    });
  });

  it("uses safe defaults when an optional selector is absent", () => {
    const job = buildGenerationJob({
      file: { name: "song.mp3" },
      artist: "Artist",
      songTitle: "Song",
      segments: [],
    });
    expect(job).toMatchObject({
      segmentsRevision: 0,
      editorRevision: null,
      editorVersionId: null,
      bgCacheKey: null,
      status: "queued",
      lyricLook: "",
    });
  });

  it("keeps the lyric look (normalized) next to the selectors it overrides", () => {
    const job = buildGenerationJob({
      file: { name: "song.mp3" },
      segments: [],
      lyricLook: "Cine",
      font: "anton",
      lyricsAnimation: "karaoke",
      lineTransition: "wipe",
    });
    // El look no borra lo elegido a mano: volver a "Sin look" lo recupera.
    expect(job).toMatchObject({ lyricLook: "cine", font: "anton", lyricsAnimation: "karaoke", lineTransition: "wipe" });
    expect(buildGenerationJob({ file: { name: "a.mp3" }, segments: [], lyricLook: "vaporwave" }).lyricLook).toBe("");
  });
});

// Incidente 11-nov-2026: "Generar sin revisar" mandaba lyric_look="" aunque
// el operador hubiera elegido un look en el wizard → letra blanca plana.
describe("buildDirectGenerationJob (Generar sin revisar)", () => {
  // Una entrada de files[*] tal como la deja UploadZone (batchDefaults
  // faneados a cada archivo).
  const fileEntry = {
    file: { name: "rosa-de-fogo.wav" },
    artist: "  Diego Faria ",
    songTitle: "Rosa de Fogo",
    language: "pt",
    genre: "", font: "", concept: "", movementStyle: "estatico", effect: "",
    backgroundHint: "", bgVerbatim: true,
    textCase: "title", frameFormat: "full", fontScale: "1.1",
    lyricsAnimation: "none", lineTransition: "none", textContrast: "strong",
    lyricLook: "cine", lyricGenre: "", lyricVariant: "",
    lyricFontMain: "", lyricFontKey: "", lyricAltColor: "", lyricAccentColor: "",
    lyricColor: "#FFFFFF", lyricSungColor: "#FFFFFF",
    titleTemplate: "centered", titleSize: "1.2", titleArtistFont: "", titleSongFont: "",
    titleSongBreak: "",
  };

  it("carries the look and every visual pick the reviewed path carries", () => {
    const job = buildDirectGenerationJob(fileEntry);
    expect(job).toMatchObject({
      filename: "rosa-de-fogo.wav",
      _file: fileEntry.file,
      artist: "Diego Faria",
      songTitle: "Rosa de Fogo",
      lyricLook: "cine",
      textCase: "title",
      fontScale: "1.1",
      textContrast: "strong",
      movementStyle: "estatico",
      titleTemplate: "centered",
      titleSize: "1.2",
      transcribeJobId: null,
      status: "queued",
    });
  });

  it("produces the same /generate look fields as the reviewed path", () => {
    const direct = buildDirectGenerationJob(fileEntry);
    const reviewed = buildGenerationJob({ ...fileEntry, artist: "Diego Faria", segments: [] });
    const lookFields = (j) => {
      const fd = new FormData();
      fd.append("lyric_look", normalizeLyricLook(j.lyricLook));
      fd.append("lyric_color", lyricColorForSubmit(j.lyricLook, j.lyricColor));
      appendLookVariantFields(fd, j);
      return Object.fromEntries(fd.entries());
    };
    // Variante automática → no viaja: el backend renderiza el diseño original.
    expect(lookFields(direct)).toEqual({ lyric_look: "cine", lyric_color: "" });
    expect(lookFields(direct)).toEqual(lookFields(reviewed));
    const pinned = buildDirectGenerationJob({ ...fileEntry, lyricGenre: "rock", lyricVariant: 3 });
    expect(lookFields(pinned)).toMatchObject({ lyric_genre: "rock", lyric_variant: "3" });
  });

  it("is what App.jsx handleGenerateDirect queues", () => {
    const src = readFileSync(resolve(process.cwd(), "src/App.jsx"), "utf8");
    const start = src.indexOf("const handleGenerateDirect = ");
    const body = src.slice(start, src.indexOf("processQueueDirect(jobList)", start));
    expect(start).toBeGreaterThan(-1);
    expect(body).toMatch(/files\.map\(\(f\) => buildDirectGenerationJob\(f\)\)/);
  });
});
