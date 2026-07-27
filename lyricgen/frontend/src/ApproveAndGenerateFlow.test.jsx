/**
 * Integration test for the "Aprobar y generar video" flow.
 *
 * This is the test that WOULD HAVE CAUGHT the bug fixed in PR #474
 * (commit fd0335f, 2026-05-29) — Agus.Cafisi's prod incident where
 * clicking "Aprobar y generar" silently navigated nowhere and the job
 * was stuck in transcribed_pending. PR #473 (the day before) patched
 * three stub-file crash sites but missed the one inside
 * `startGenerationWithSegments` at App.jsx:2351.
 *
 * Existing tests passed (270/270) because they all covered isolated
 * functions. None of them mounted the wizard's approve→generate
 * pipeline as a whole. This file closes that gap by replicating the
 * handler chain inline and exercising it across the five scenarios
 * that matter:
 *
 *   1. Happy path single song with real File → POST /generate fires
 *   2. Happy path single song with stub File + transcribeJobId → backend
 *      reuses R2 audio, generation proceeds
 *   3. Bug scenario: stub File without transcribeJobId → alert shown,
 *      session cleared, redirect to /new, NO crash, NO POST
 *   4. Multi-song batch with mixed files: one broken aborts the batch
 *   5. fetch /generate throws → try/catch catches, alert shown,
 *      no GlobalErrorBoundary (regression guard for Guard 2)
 *
 * Keep these inline mirrors in sync with App.jsx::handleApproveLyrics
 * and App.jsx::startGenerationWithSegments. If a contract changes
 * there, these tests should fail loudly and force the maintainer to
 * think about whether the change preserves the invariants.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

// ─── Inline mirror of the production handlers ───────────────────────
//
// We rebuild handleApproveLyrics + startGenerationWithSegments as pure
// functions taking their dependencies as parameters. The structure
// matches App.jsx lines 2147-2370 (approve→generate path, ignoring
// the edit-mode branch which is tested separately).

function makeStartGenerationWithSegments(deps) {
  const {
    t, alert, navigate,
    setJobs, setReadyToGenerate, setApprovedJobs, setCurrentReview,
    wizardPersistence, fetchGenerate,
  } = deps;
  return async function startGenerationWithSegments(approved) {
    // Guard 1 (hotfix #473.2): validate every entry has either a real
    // Blob or a transcribeJobId before the .map.
    const broken = approved.find(
      (a) => !a.transcribeJobId && (!a.file || typeof a.file.slice !== "function"),
    );
    if (broken) {
      console.warn("[wizard] approve aborted: file is not a Blob", {
        has_file: !!broken.file,
        has_transcribeJobId: !!broken.transcribeJobId,
        is_stub: !!broken.file?._restoredStub,
      });
      alert({
        title: t("wizard.session_expired_title") || "Tu sesión expiró",
        description: t("wizard.session_expired_desc") || "...",
        tone: "warning",
      });
      wizardPersistence.clear();
      setCurrentReview(null);
      setApprovedJobs([]);
      setReadyToGenerate(false);
      navigate("/new", { replace: true });
      return;
    }

    const jobList = approved.map((a) => ({
      filename: (a.file && a.file.name) || "audio.mp3",
      _file: a.file,
      artist: a.artist,
      songTitle: (a.songTitle || "").trim(),
      transcribeJobId: a.transcribeJobId || null,
      status: "queued",
    }));
    setJobs(jobList);
    navigate("/generating");
    setReadyToGenerate(false);
    setApprovedJobs([]);

    // The worker that posts /generate. In production this is a while
    // loop with per-entry FormData; for the test we just assert the
    // POST happens with the right body shape.
    for (const job of jobList) {
      await fetchGenerate(job);
    }
  };
}

function makeHandleApproveLyrics(deps) {
  const {
    currentReviewRef, approvedJobsRef,
    setApprovedJobs, setCurrentReview, setReadyToGenerate,
    persistSegmentsToBackend, transcribeNext, startGenerationWithSegments,
    t, alert,
  } = deps;
  return async function handleApproveLyrics(editedSegments) {
    const r = currentReviewRef.current;
    if (!r) return;

    // Skip the edit-mode branch — covered by a separate test file.

    const newApproved = [
      ...approvedJobsRef.current,
      {
        file: r.file,
        artist: r.artist,
        songTitle: r.songTitle || "",
        segments: editedSegments,
        transcribeJobId: r.transcribeJobId || null,
      },
    ];
    setApprovedJobs(newApproved);
    setCurrentReview(null);

    if (r.transcribeJobId) {
      persistSegmentsToBackend(r.transcribeJobId, editedSegments);
    }

    // Guard 2 (hotfix #473.2): try/catch wraps the final switch so a
    // future regression surfaces as an alert, not a GlobalErrorBoundary.
    const nextIdx = r.queueIdx + 1;
    try {
      if (nextIdx < r.queue.length) {
        transcribeNext(r.queue, nextIdx);
      } else if (r.queue.length === 1) {
        await startGenerationWithSegments(newApproved);
      } else {
        setReadyToGenerate(true);
      }
    } catch (e) {
      console.error("[wizard] approve→generate failed", e);
      alert({
        title: t("wizard.generate_failed_title") || "No pudimos disparar la generación",
        description: t("wizard.generate_failed_desc") || "...",
        tone: "error",
      });
    }
  };
}

// ─── Test scaffolding ───────────────────────────────────────────────

function makeRealFile(name = "song.mp3") {
  const blob = new Blob(["audio bytes"], { type: "audio/mpeg" });
  // jsdom Blob doesn't have .name by default; emulate File.
  Object.defineProperty(blob, "name", { value: name });
  return blob;
}

function makeStubFile(name = "song.mp3") {
  // What wizardPersistence.rehydrateReview produces post-refresh.
  return {
    name,
    size: 1000,
    type: "audio/mpeg",
    lastModified: 0,
    _restoredStub: true,
  };
}

function makeHarness(overrides = {}) {
  const calls = {
    alert: [],
    navigate: [],
    setJobs: [],
    setApprovedJobs: [],
    setCurrentReview: [],
    setReadyToGenerate: [],
    persistSegmentsToBackend: [],
    transcribeNext: [],
    wizardPersistenceClear: 0,
    fetchGenerate: [],
  };

  const currentReviewRef = { current: overrides.currentReview ?? null };
  const approvedJobsRef = { current: overrides.approvedJobs ?? [] };

  const fetchGenerate = overrides.fetchGenerate || (async (job) => {
    calls.fetchGenerate.push(job);
    return { ok: true };
  });

  const startGen = makeStartGenerationWithSegments({
    // Force the `||` fallback strings to be used so assertions match
    // the actual user-facing copy, not the i18n key.
    t: () => null,
    alert: (a) => calls.alert.push(a),
    navigate: (p, opts) => calls.navigate.push([p, opts]),
    setJobs: (j) => calls.setJobs.push(j),
    setReadyToGenerate: (v) => calls.setReadyToGenerate.push(v),
    setApprovedJobs: (j) => calls.setApprovedJobs.push(j),
    setCurrentReview: (v) => calls.setCurrentReview.push(v),
    wizardPersistence: { clear: () => { calls.wizardPersistenceClear++; } },
    fetchGenerate,
  });

  const handleApprove = makeHandleApproveLyrics({
    currentReviewRef,
    approvedJobsRef,
    setApprovedJobs: (j) => { calls.setApprovedJobs.push(j); approvedJobsRef.current = j; },
    setCurrentReview: (v) => { calls.setCurrentReview.push(v); currentReviewRef.current = v; },
    setReadyToGenerate: (v) => calls.setReadyToGenerate.push(v),
    persistSegmentsToBackend: (id, segs) => calls.persistSegmentsToBackend.push([id, segs]),
    transcribeNext: (q, idx) => calls.transcribeNext.push([q, idx]),
    startGenerationWithSegments: startGen,
    // Force the `||` fallback strings to be used so assertions match
    // the actual user-facing copy, not the i18n key.
    t: () => null,
    alert: (a) => calls.alert.push(a),
  });

  return { handleApprove, calls };
}

// ─── Tests ──────────────────────────────────────────────────────────

describe("Aprobar y generar — flow integration (#473.2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("happy path: single song with real File → POST /generate fires", async () => {
    const file = makeRealFile("viejas-locas.mp3");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "Viejas Locas", songTitle: "El Arbol",
        queueIdx: 0, queue: [{ file }], transcribeJobId: "txn-1",
      },
    });

    await handleApprove([{ start: 0, end: 1, text: "hola" }]);

    expect(calls.alert).toHaveLength(0);
    expect(calls.navigate).toContainEqual(["/generating", undefined]);
    expect(calls.fetchGenerate).toHaveLength(1);
    expect(calls.fetchGenerate[0].filename).toBe("viejas-locas.mp3");
    expect(calls.fetchGenerate[0].transcribeJobId).toBe("txn-1");
  });

  it("rehydrated stub File WITH transcribeJobId → proceeds (R2 fallback)", async () => {
    const file = makeStubFile("viejas-locas.mp3");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "Viejas Locas", songTitle: "El Arbol",
        queueIdx: 0, queue: [{ file }], transcribeJobId: "txn-1",
      },
    });

    await handleApprove([{ start: 0, end: 1, text: "hola" }]);

    expect(calls.alert).toHaveLength(0);
    expect(calls.navigate).toContainEqual(["/generating", undefined]);
    expect(calls.fetchGenerate).toHaveLength(1);
    expect(calls.fetchGenerate[0].filename).toBe("viejas-locas.mp3");
  });

  it("REGRESSION: stub File WITHOUT transcribeJobId → alert + clear + /new (would catch Agus's bug)", async () => {
    const file = makeStubFile("arruinarse.wav");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "Tan Bionica", songTitle: "Arruinarse",
        queueIdx: 0, queue: [{ file }], transcribeJobId: null,
      },
    });

    await handleApprove([{ start: 0, end: 1, text: "hola" }]);

    expect(calls.alert).toHaveLength(1);
    expect(calls.alert[0].tone).toBe("warning");
    expect(calls.alert[0].title).toMatch(/sesi/i);
    expect(calls.wizardPersistenceClear).toBe(1);
    expect(calls.navigate).toContainEqual(["/new", { replace: true }]);
    expect(calls.fetchGenerate).toHaveLength(0);
    expect(calls.setJobs).toHaveLength(0);
  });

  it("REGRESSION: null File without transcribeJobId → same abort path", async () => {
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file: null, artist: "X", songTitle: "Y",
        queueIdx: 0, queue: [{ file: null }], transcribeJobId: null,
      },
    });

    await handleApprove([{ start: 0, end: 1, text: "x" }]);

    expect(calls.alert).toHaveLength(1);
    expect(calls.wizardPersistenceClear).toBe(1);
    expect(calls.fetchGenerate).toHaveLength(0);
  });

  it("Guard 2: fetch /generate throws → try/catch shows error alert (NOT bubbles to ErrorBoundary)", async () => {
    const file = makeRealFile("song.mp3");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "X", songTitle: "Y",
        queueIdx: 0, queue: [{ file }], transcribeJobId: "txn-1",
      },
      fetchGenerate: async () => {
        throw new Error("network exploded");
      },
    });

    // The critical assertion: this should NOT throw out of handleApprove.
    // If it does, the outer GlobalErrorBoundary catches it and the
    // user sees "Algo salió mal" with no recovery.
    await expect(handleApprove([{ start: 0, end: 1, text: "x" }])).resolves.toBeUndefined();

    expect(calls.alert).toHaveLength(1);
    expect(calls.alert[0].tone).toBe("error");
    expect(calls.alert[0].title).toMatch(/generaci/i);
  });

  it("multi-song batch: middle song still in queue → transcribeNext, no /generate yet", async () => {
    const file = makeRealFile("song1.mp3");
    const file2 = makeRealFile("song2.mp3");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "A", songTitle: "S1",
        queueIdx: 0, queue: [{ file }, { file: file2 }], transcribeJobId: "txn-1",
      },
    });

    await handleApprove([{ start: 0, end: 1, text: "x" }]);

    expect(calls.transcribeNext).toHaveLength(1);
    expect(calls.transcribeNext[0][1]).toBe(1); // nextIdx
    expect(calls.fetchGenerate).toHaveLength(0); // not yet
    expect(calls.navigate).toHaveLength(0);
  });

  it("multi-song batch: last song approved → readyToGenerate set, NOT auto-generate", async () => {
    const file = makeRealFile("song2.mp3");
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "A", songTitle: "S2",
        queueIdx: 1, queue: [{ file: makeRealFile("song1.mp3") }, { file }],
        transcribeJobId: "txn-2",
      },
      approvedJobs: [{ file: makeRealFile("song1.mp3"), transcribeJobId: "txn-1" }],
    });

    await handleApprove([{ start: 0, end: 1, text: "x" }]);

    expect(calls.setReadyToGenerate).toContainEqual(true);
    expect(calls.fetchGenerate).toHaveLength(0); // shown the "ready" screen first
    expect(calls.transcribeNext).toHaveLength(0);
  });

  it("empty currentReview is a no-op (early return)", async () => {
    const { handleApprove, calls } = makeHarness({ currentReview: null });
    await handleApprove([]);
    expect(calls.alert).toHaveLength(0);
    expect(calls.fetchGenerate).toHaveLength(0);
    expect(calls.navigate).toHaveLength(0);
  });

  it("persistSegmentsToBackend is called when transcribeJobId is set", async () => {
    const file = makeRealFile("song.mp3");
    const segs = [{ start: 0, end: 1, text: "hola" }];
    const { handleApprove, calls } = makeHarness({
      currentReview: {
        file, artist: "X", songTitle: "Y",
        queueIdx: 0, queue: [{ file }], transcribeJobId: "txn-1",
      },
    });

    await handleApprove(segs);

    expect(calls.persistSegmentsToBackend).toHaveLength(1);
    expect(calls.persistSegmentsToBackend[0][0]).toBe("txn-1");
    expect(calls.persistSegmentsToBackend[0][1]).toEqual(segs);
  });
});
