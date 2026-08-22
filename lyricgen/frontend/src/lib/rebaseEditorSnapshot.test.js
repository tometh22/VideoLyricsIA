import { describe, expect, it, vi } from "vitest";
import { rebaseEditorSnapshot } from "./rebaseEditorSnapshot";

function reply(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

const base = [{ segment_id: "line-1", start: 0, end: 1, text: "base" }];
const local = [{ segment_id: "line-1", start: 0, end: 1, text: "local" }];

describe("rebaseEditorSnapshot", () => {
  it("preserves a remote-only line while keeping the local edit", async () => {
    const authFetch = vi.fn(async (path) => {
      if (path.endsWith("/editor/job")) {
        return reply({
          revision: 8,
          original_segments: base,
          segments: [
            { ...base[0], text: "base" },
            { segment_id: "line-2", start: 1, end: 2, text: "remote" },
          ],
        });
      }
      if (path.endsWith("/versions?limit=50")) return reply({ versions: [] });
      return reply({}, 404);
    });

    const result = await rebaseEditorSnapshot({
      authFetch,
      api: "",
      jobId: "job",
      localSegments: local,
      baseRevision: 7,
    });

    expect(result).toMatchObject({ ok: true, hadLineConflicts: false });
    expect(result.segments).toEqual(expect.arrayContaining([
      expect.objectContaining({ text: "local" }),
      expect.objectContaining({ text: "remote" }),
    ]));
  });

  it("uses the immutable editor version before the history-list fallback", async () => {
    const authFetch = vi.fn(async (path) => {
      if (path.endsWith("/editor/job")) {
        return reply({ revision: 8, segments: [{ ...base[0], text: "remote" }] });
      }
      if (path.endsWith("/versions/v7")) return reply({ segments: base });
      return reply({ versions: [] });
    });

    const result = await rebaseEditorSnapshot({
      authFetch,
      api: "",
      jobId: "job",
      localSegments: local,
      baseRevision: 7,
      editorVersionId: "v7",
    });

    expect(result).toMatchObject({ ok: true, hadLineConflicts: true });
    expect(authFetch).toHaveBeenCalledWith("/editor/job/versions/v7");
    expect(authFetch).not.toHaveBeenCalledWith("/editor/job/versions?limit=50");
  });

  it("does not synthesize an overwrite when there is no merge base", async () => {
    const authFetch = vi.fn().mockResolvedValue(reply({
      revision: 8,
      segments: [{ ...base[0], text: "remote" }],
    }));
    await expect(rebaseEditorSnapshot({
      authFetch,
      api: "",
      jobId: "job",
      localSegments: local,
      baseRevision: 7,
    })).resolves.toMatchObject({ ok: false, reason: "merge-base-unavailable" });
  });
});
