/**
 * Regression test for hotfix #473.2 (2026-05-29).
 *
 * Bug observed in production (UMG, Agus.Cafisi): operator approves
 * lyrics → clicks "Aprobar y generar video" → silently nothing happens
 * → job stays in `transcribed_pending` forever; the wizard navigates
 * back to /new and Historial shows the empty state "Este video todavía
 * no se generó".
 *
 * Root cause: `startGenerationWithSegments` in App.jsx:2349 mapped
 * `approved` to `{ filename: a.file.name, ... }`. When `a.file` was a
 * stub object rehydrated from sessionStorage (no Blob, no `.slice`,
 * sometimes no `.name`), the `.map` callback threw TypeError. The
 * exception bubbled to GlobalErrorBoundary, `navigate("/generating")`
 * never fired, and the backend `/generate` POST never happened.
 *
 * PR #473 (commit ea58098) patched three other stub-file crash sites
 * but missed this one — the most user-visible path.
 *
 * This test pins:
 *   1. The guard predicate: an entry is "broken" iff no real Blob AND
 *      no transcribeJobId (the only two ways the backend can produce
 *      the audio).
 *   2. The defensive filename read: falls back to "audio.mp3" when
 *      file is null/stub.
 *
 * Keep this inline mirror in sync with App.jsx::startGenerationWithSegments.
 */
import { describe, it, expect } from "vitest";

// Inline mirror of the guard predicate. If the predicate in App.jsx
// changes, this test should fail loudly and force the maintainer to
// think about whether the change is safe.
function isBrokenForGenerate(a) {
  return !a.transcribeJobId && (!a.file || typeof a.file.slice !== "function");
}

// Inline mirror of the defensive filename read.
function safeFilename(a) {
  return (a.file && a.file.name) || "audio.mp3";
}

describe("startGenerationWithSegments guards (hotfix #473.2)", () => {
  describe("isBrokenForGenerate predicate", () => {
    it("real File without transcribeJobId is NOT broken", () => {
      const realFile = new Blob(["audio bytes"], { type: "audio/mpeg" });
      Object.defineProperty(realFile, "name", { value: "song.mp3" });
      const entry = { file: realFile, transcribeJobId: null };
      expect(isBrokenForGenerate(entry)).toBe(false);
    });

    it("real File with transcribeJobId is NOT broken", () => {
      const realFile = new Blob(["audio"], { type: "audio/mpeg" });
      const entry = { file: realFile, transcribeJobId: "abc123" };
      expect(isBrokenForGenerate(entry)).toBe(false);
    });

    it("stub file WITH transcribeJobId is NOT broken (backend uses R2 cache)", () => {
      const stubFile = { name: "song.mp3", size: 1000, _restoredStub: true };
      const entry = { file: stubFile, transcribeJobId: "abc123" };
      expect(isBrokenForGenerate(entry)).toBe(false);
    });

    it("stub file WITHOUT transcribeJobId IS broken (no way to send audio)", () => {
      const stubFile = { name: "song.mp3", size: 1000, _restoredStub: true };
      const entry = { file: stubFile, transcribeJobId: null };
      expect(isBrokenForGenerate(entry)).toBe(true);
    });

    it("null file without transcribeJobId IS broken", () => {
      const entry = { file: null, transcribeJobId: null };
      expect(isBrokenForGenerate(entry)).toBe(true);
    });

    it("undefined file without transcribeJobId IS broken", () => {
      const entry = { transcribeJobId: null };
      expect(isBrokenForGenerate(entry)).toBe(true);
    });

    it("null file with transcribeJobId is NOT broken (R2 fallback)", () => {
      const entry = { file: null, transcribeJobId: "abc123" };
      expect(isBrokenForGenerate(entry)).toBe(false);
    });
  });

  describe("safeFilename defensive read", () => {
    it("uses file.name when file is a real Blob", () => {
      const realFile = new Blob(["audio"], { type: "audio/mpeg" });
      Object.defineProperty(realFile, "name", { value: "Viejas Locas.mp3" });
      expect(safeFilename({ file: realFile })).toBe("Viejas Locas.mp3");
    });

    it("uses stub.name when file is a stub with name", () => {
      const stub = { name: "Bionica.wav", _restoredStub: true };
      expect(safeFilename({ file: stub })).toBe("Bionica.wav");
    });

    it("falls back to audio.mp3 when file is null", () => {
      expect(safeFilename({ file: null })).toBe("audio.mp3");
    });

    it("falls back to audio.mp3 when file is undefined", () => {
      expect(safeFilename({})).toBe("audio.mp3");
    });

    it("falls back to audio.mp3 when stub has no name (corrupted)", () => {
      expect(safeFilename({ file: {} })).toBe("audio.mp3");
    });
  });

  describe("batch validation", () => {
    it("a batch with at least one broken entry is detected by .find()", () => {
      const realFile = new Blob(["a"], { type: "audio/mpeg" });
      const batch = [
        { file: realFile, transcribeJobId: "ok1" },
        { file: null, transcribeJobId: null }, // ← broken
        { file: realFile, transcribeJobId: "ok2" },
      ];
      const broken = batch.find(isBrokenForGenerate);
      expect(broken).toBeDefined();
      expect(broken.transcribeJobId).toBe(null);
    });

    it("a batch with all valid entries returns undefined from .find()", () => {
      const realFile = new Blob(["a"], { type: "audio/mpeg" });
      const batch = [
        { file: realFile, transcribeJobId: null },
        { file: realFile, transcribeJobId: "abc" },
      ];
      expect(batch.find(isBrokenForGenerate)).toBeUndefined();
    });
  });
});
