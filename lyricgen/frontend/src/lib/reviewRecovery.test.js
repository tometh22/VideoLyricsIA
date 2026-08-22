import { describe, expect, it } from "vitest";
import { isReusableEditSnapshot, resolveLegacyDraft } from "./reviewRecovery";

const remote = [{ start: 1, end: 2, text: "Servidor" }];
const local = [{ start: 1, end: 2, text: "Local" }];

describe("review recovery concurrency", () => {
  it("reuses a wizard snapshot only at the exact server revision", () => {
    const snapshot = {
      currentReview: {
        editingJobId: "job-1",
        segmentsRevision: 7,
        segments: local,
      },
    };
    expect(isReusableEditSnapshot({ snapshot, jobId: "job-1", serverRevision: 7 })).toBe(true);
    expect(isReusableEditSnapshot({ snapshot, jobId: "job-1", serverRevision: 8 })).toBe(false);
    expect(isReusableEditSnapshot({ snapshot, jobId: "job-2", serverRevision: 7 })).toBe(false);
  });

  it("restores a draft based on the current revision as-is", () => {
    expect(resolveLegacyDraft({
      draft: { base_revision: 7, segments: local },
      currentSegments: remote,
      currentRevision: 7,
    })).toEqual({ action: "restore", segments: local });
  });

  it("discards an already-saved stale draft without creating a conflict", () => {
    expect(resolveLegacyDraft({
      draft: { base_revision: 7, segments: remote },
      currentSegments: remote,
      currentRevision: 8,
    })).toEqual({ action: "discard", segments: null });
  });

  it("ignores an empty or missing draft", () => {
    expect(resolveLegacyDraft({
      draft: null, currentSegments: remote, currentRevision: 8,
    })).toEqual({ action: "none", segments: null });
    expect(resolveLegacyDraft({
      draft: { segments: [] }, currentSegments: remote, currentRevision: 8,
    })).toEqual({ action: "none", segments: null });
  });

  // El caso real: la misma persona con dos pestañas abiertas. NUNCA se le
  // pregunta nada y NUNCA se le tira el trabajo.
  describe("stale draft (varias pestañas del mismo usuario)", () => {
    it("never returns a conflict the operator has to arbitrate", () => {
      const result = resolveLegacyDraft({
        draft: { base_revision: 7, segments: local },
        currentSegments: remote,
        currentRevision: 9,
      });
      expect(result.action).toBe("restore");
      expect(result.action).not.toBe("conflict");
    });

    it("three-way merges when the draft knows its base: la otra pestaña no se pisa", () => {
      const base = [
        { start: 1, end: 2, text: "Uno" },
        { start: 2, end: 3, text: "Dos" },
      ];
      const draft = [
        { start: 1, end: 2, text: "Uno editado acá" },
        { start: 2, end: 3, text: "Dos" },
      ];
      const server = [
        { start: 1, end: 2, text: "Uno" },
        { start: 2, end: 3, text: "Dos editado en la otra pestaña" },
      ];
      const result = resolveLegacyDraft({
        draft: { base_revision: 7, base_segments: base, segments: draft },
        currentSegments: server,
        currentRevision: 9,
      });
      expect(result.action).toBe("restore");
      expect(result.rebased).toBe(true);
      expect(result.segments.map((s) => s.text)).toEqual([
        "Uno editado acá",                 // lo tipeado acá sobrevive
        "Dos editado en la otra pestaña",  // y lo de la otra pestaña también
      ]);
    });

    it("keeps the local line when both tabs touched it", () => {
      const base = [{ start: 1, end: 2, text: "Original" }];
      const result = resolveLegacyDraft({
        draft: { base_revision: 7, base_segments: base, segments: local },
        currentSegments: remote,
        currentRevision: 9,
      });
      expect(result.segments.map((s) => s.text)).toEqual(["Local"]);
    });

    it("restores the draft when there is no base to merge against", () => {
      const result = resolveLegacyDraft({
        draft: { base_revision: 7, segments: local },
        currentSegments: remote,
        currentRevision: 9,
      });
      expect(result).toEqual({ action: "restore", segments: local, rebased: true });
    });
  });
});
