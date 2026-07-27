/**
 * Tests for mediaUrl's `version` cache-buster.
 *
 * Regression: UMG job eaff5c7baf50 ("no me lo está actualizando") — edits
 * overwrite the SAME R2 key, so a mounted <video src> that never changes
 * keeps showing the pre-edit render. The operator burned her 3 edits
 * re-requesting changes that had already succeeded server-side.
 *
 * What this test pins:
 *   - getPreviewUrl / getDownloadUrl append `&v=<version>` when a version
 *     is given, so the URL STRING changes when the render changes.
 *   - Omitting version keeps the legacy URL shape (no `&v=`) — backward
 *     compatible for callers that don't care about re-renders.
 *   - The media TOKEN is reused across versions (single /media-token
 *     request): the buster must NOT multiply token traffic.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";

describe("mediaUrl version cache-buster", () => {
  let mediaUrl;

  beforeEach(async () => {
    vi.resetModules();
    window.localStorage.setItem("genly_token", "test-token");
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ token: "tok-abc" }),
      })
    );
    mediaUrl = await import("./mediaUrl.js");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("appends &v= when a version is provided (preview + download)", async () => {
    const p = await mediaUrl.getPreviewUrl("job1", "video", "2-pending_review");
    expect(p).toContain("/preview/job1/video?token=tok-abc");
    expect(p).toContain("&v=2-pending_review");

    const d = await mediaUrl.getDownloadUrl("job1", "video", "2-pending_review");
    expect(d).toContain("/download/job1/video?token=tok-abc");
    expect(d).toContain("&v=2-pending_review");
  });

  it("omits &v= when no version is given (legacy callers unchanged)", async () => {
    const p = await mediaUrl.getPreviewUrl("job1", "video");
    expect(p).toContain("/preview/job1/video?token=tok-abc");
    expect(p).not.toContain("&v=");
  });

  it("reuses the cached token across versions — one /media-token request", async () => {
    const v1 = await mediaUrl.getPreviewUrl("job1", "video", "1-editing");
    const v2 = await mediaUrl.getPreviewUrl("job1", "video", "1-pending_review");
    expect(v1).not.toEqual(v2); // URL changed → player reloads
    // ...but the token itself was fetched exactly once.
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
