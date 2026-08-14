import { describe, expect, it } from "vitest";
import { buildGenerationJob } from "./buildGenerationJob";

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
    });
  });
});
