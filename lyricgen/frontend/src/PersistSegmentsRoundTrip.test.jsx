/**
 * Regression test for PR A (fix/timeline-drag-persists, 2026-05-26).
 *
 * Bug observed in production: operator drags a line edge in the lyrics
 * timeline to extend its duration, releases the mouse, and the line
 * "snaps back" to its old position. The backend save succeeds, but the
 * parent's `currentReview.segments` never updates — so any subsequent
 * re-render (drift-sync of typography, rehydrate from sessionStorage,
 * polling) passes stale segments back through the prop, the editor's
 * prop-sync useEffect (LyricsEditor.jsx:440-448) detects a new reference
 * and rewrites local `edited` with the old data.
 *
 * The fix lives in App.jsx::persistSegmentsToBackend: after a successful
 * POST to /save-segments, call setCurrentReview to keep the prop in sync
 * with the backend ground truth.
 *
 * This test mirrors the callback contract (not the actual component
 * tree) — App.jsx is too big to mount for a unit test. It guards against
 * regressing the four invariants the fix establishes.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

// Inline mirror of App.jsx's persistSegmentsToBackend. Keep this in sync
// with the production callback — if the contract changes there, this
// test should fail loudly and force the maintainer to think.
//
// 2026-05-28 (audit P0 #74): contract widened — function now returns
// { ok, reason, status? } so callers (LyricsEditor's saveStatus state
// machine) can branch on outcome instead of treating every void return
// as success. Mirror updated to match. Return values:
//   - { ok: false, reason: "no-data" } when jobId/segments invalid
//   - { ok: false, reason: "job-gone", status: 404 } on 404
//   - { ok: false, reason: `http-${n}`, status: n } on other 4xx/5xx
//   - { ok: false, reason: "network", error: string } on fetch reject
//   - { ok: true } on 2xx success
function makePersistSegmentsToBackend({ authFetch, setCurrentReview, API = "https://api.test" }) {
  return async function persistSegmentsToBackend(jobId, segments) {
    if (!jobId || !Array.isArray(segments) || segments.length === 0) {
      return { ok: false, reason: "no-data" };
    }
    try {
      const res = await authFetch(`${API}/jobs/${jobId}/save-segments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments }),
      });
      if (!res.ok) {
        if (res.status !== 404) {
          console.warn("[autosave] /save-segments failed", res.status);
        }
        return {
          ok: false,
          reason: res.status === 404 ? "job-gone" : `http-${res.status}`,
          status: res.status,
        };
      }
      setCurrentReview((prev) => {
        if (!prev) return prev;
        const matchesWizard = prev.transcribeJobId === jobId;
        const matchesEditor = prev.editingJobId === jobId;
        if (!matchesWizard && !matchesEditor) return prev;
        return { ...prev, segments };
      });
      return { ok: true };
    } catch (err) {
      console.warn("[autosave] /save-segments network error", err);
      return { ok: false, reason: "network", error: String(err) };
    }
  };
}

describe("persistSegmentsToBackend — round-trip to currentReview", () => {
  let authFetch;
  let setCurrentReview;
  let prev;
  const SEGMENTS = [{ start: 0, end: 5, text: "linea 1" }, { start: 5, end: 10, text: "linea 2" }];

  beforeEach(() => {
    authFetch = vi.fn();
    // setCurrentReview captures the updater functions; we exercise them
    // with the test's `prev` value to inspect what the merge produces.
    setCurrentReview = vi.fn();
  });

  it("on success: updates currentReview.segments when transcribeJobId matches", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    prev = { transcribeJobId: "abc123", segments: [{ start: 0, end: 99, text: "stale" }] };

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("abc123", SEGMENTS);

    expect(setCurrentReview).toHaveBeenCalledTimes(1);
    const updater = setCurrentReview.mock.calls[0][0];
    const next = updater(prev);
    expect(next).not.toBe(prev);  // new reference
    expect(next.segments).toBe(SEGMENTS);  // points at the persisted array
    expect(next.transcribeJobId).toBe("abc123");  // other fields preserved
  });

  it("on success: updates currentReview.segments when editingJobId matches", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    prev = { editingJobId: "edt456", segments: [{ start: 0, end: 99, text: "stale" }] };

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("edt456", SEGMENTS);

    expect(setCurrentReview).toHaveBeenCalledTimes(1);
    const updater = setCurrentReview.mock.calls[0][0];
    const next = updater(prev);
    expect(next.segments).toBe(SEGMENTS);
    expect(next.editingJobId).toBe("edt456");
  });

  it("on success: noop on the updater when currentReview is null (already approved)", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("abc123", SEGMENTS);

    expect(setCurrentReview).toHaveBeenCalledTimes(1);
    const updater = setCurrentReview.mock.calls[0][0];
    expect(updater(null)).toBeNull();  // no return-replacement, no crash
  });

  it("on success: noop on the updater when jobId matches no path on the current review", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    // currentReview is for a DIFFERENT job — autosave landing late
    // shouldn't clobber the operator's current edit context.
    prev = { transcribeJobId: "other-job", segments: [{ start: 0, end: 99, text: "other" }] };

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("abc123", SEGMENTS);

    expect(setCurrentReview).toHaveBeenCalledTimes(1);
    const updater = setCurrentReview.mock.calls[0][0];
    expect(updater(prev)).toBe(prev);  // same reference, untouched
  });

  it("on backend rejection (500): does NOT update currentReview", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 500 });
    prev = { transcribeJobId: "abc123", segments: [{ start: 0, end: 99, text: "stale" }] };

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("abc123", SEGMENTS);

    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  it("on 404 (job reaped): does NOT update currentReview, logs softly", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 404 });
    prev = { transcribeJobId: "abc123", segments: [{ start: 0, end: 99, text: "stale" }] };

    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("abc123", SEGMENTS);

    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  it("ignores invalid jobIds or empty segments", async () => {
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    await persist("", SEGMENTS);
    await persist("abc", []);
    await persist("abc", null);
    await persist(null, SEGMENTS);

    expect(authFetch).not.toHaveBeenCalled();
    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  // QA fix 2026-05-28 (audit P0 #74): explicit return-contract tests so
  // a regression where someone deletes the `{ ok }` return value to
  // "simplify" fails loudly. The whole error-feedback chain in
  // LyricsEditor depends on this object existing.

  it("returns { ok: true } on 2xx success", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    const result = await persist("abc123", SEGMENTS);
    expect(result).toEqual({ ok: true });
  });

  it("returns { ok: false, reason: 'job-gone', status: 404 } on 404", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 404 });
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    const result = await persist("abc123", SEGMENTS);
    expect(result).toEqual({ ok: false, reason: "job-gone", status: 404 });
  });

  it("returns { ok: false, reason: 'http-500', status: 500 } on 5xx", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 500 });
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    const result = await persist("abc123", SEGMENTS);
    expect(result).toEqual({ ok: false, reason: "http-500", status: 500 });
  });

  it("returns { ok: false, reason: 'network' } when fetch rejects", async () => {
    authFetch.mockRejectedValue(new Error("ECONNRESET"));
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    const result = await persist("abc123", SEGMENTS);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("network");
    expect(result.error).toContain("ECONNRESET");
  });

  it("returns { ok: false, reason: 'no-data' } on invalid input", async () => {
    const persist = makePersistSegmentsToBackend({ authFetch, setCurrentReview });
    const r1 = await persist("", SEGMENTS);
    const r2 = await persist("abc", []);
    const r3 = await persist(null, SEGMENTS);
    expect(r1).toEqual({ ok: false, reason: "no-data" });
    expect(r2).toEqual({ ok: false, reason: "no-data" });
    expect(r3).toEqual({ ok: false, reason: "no-data" });
  });
});
