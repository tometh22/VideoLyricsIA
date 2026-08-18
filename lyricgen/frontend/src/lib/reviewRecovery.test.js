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

  it("restores a legacy draft only when its base revision is current", () => {
    expect(resolveLegacyDraft({
      draft: { base_revision: 7, segments: local },
      currentSegments: remote,
      currentRevision: 7,
    })).toEqual({ action: "restore", segments: local });
    expect(resolveLegacyDraft({
      draft: { base_revision: 7, segments: local },
      currentSegments: remote,
      currentRevision: 8,
    })).toEqual({ action: "conflict", segments: null });
  });

  it("discards an already-saved stale draft without creating a conflict", () => {
    expect(resolveLegacyDraft({
      draft: { base_revision: 7, segments: remote },
      currentSegments: remote,
      currentRevision: 8,
    })).toEqual({ action: "discard", segments: null });
  });
});
