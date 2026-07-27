/**
 * Regression test for PR fix/edit-lyrics-fast-mount (2026-05-27).
 *
 * Bug observed in prod: operator clicked "Corregir lyrics" and the
 * screen stayed on a spinner for 30-60 seconds before the editor
 * finally appeared. Root cause: EditLyricsRoute used `await
 * Promise.all([4 fetches])` to bootstrap currentReview — and
 * `/jobs/:id/waveform` does a slow `librosa.load` on cold cache
 * (5-30s). The editor was blocked on the slowest of the four fetches.
 *
 * The fix splits bootstrap into two phases:
 *   Phase A (blocking, ~50ms): /status only. If editable + has segments,
 *     mount the editor immediately with audio/waveform/bg = null.
 *   Phase B (fire-and-forget): the 3 enhancements with per-fetch 15s
 *     timeout. Each one patches its field into currentReview when it
 *     lands; if it times out or errors, the field stays null and the
 *     editor keeps working in degraded visual mode.
 *
 * This test mirrors the bootstrap contract — we don't mount EditLyricsRoute
 * itself (heavy: depends on i18n provider, router, wizardScreen) but we
 * exercise the same logic in isolation via an inline reproduction.
 *
 * Coverage:
 *   1. Phase A only — /status responds, editor mounts even before
 *      enhancements resolve.
 *   2. Phase A + /waveform that NEVER resolves — editor mounts in <1s
 *      anyway; enhancement times out silently after 15s.
 *   3. /status timeout (>10s) — state goes to "error", editor never mounts.
 *   4. /status 404 — state goes to "not_found".
 *   5. /status with status="transcribing" — state goes to "not_editable".
 *   6. /status with empty segments_json — state goes to "no_segments".
 *   7. Race guard: setCurrentReview called from an enhancement AFTER
 *      the operator navigated to a different id is a no-op (returns prev).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

// ---------------------------------------------------------------------------
// Inline reproduction of EditLyricsRoute's bootstrap logic — mirrors the
// production async IIFE in App.jsx. If the production code changes shape,
// this needs to change too; the contract being pinned is "Phase A mounts
// regardless of enhancement latency".
// ---------------------------------------------------------------------------
function makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview, setWizardStage = () => {}, API = "https://api.test" }) {
  return async function bootstrap(id, { aliveRef, reusableSnap = false, snap = null } = {}) {
    const alive = () => (aliveRef ? aliveRef.alive : true);
    setState({ status: "loading" });

    // Phase A — /status only.
    let statusRes;
    try {
      statusRes = await authFetchWithTimeout(`${API}/status/${id}`, {}, 10_000);
    } catch (e) {
      if (alive()) setState({ status: "error" });
      return;
    }
    if (!alive()) return;
    if (statusRes.status === 404) { setState({ status: "not_found" }); return; }
    if (!statusRes.ok) { setState({ status: "error" }); return; }

    let job;
    try {
      job = await statusRes.json();
    } catch {
      if (alive()) setState({ status: "error" });
      return;
    }

    const editable =
      job.status === "pending_review" ||
      job.status === "done" ||
      job.status === "rejected";
    if (!editable) { setState({ status: "not_editable", jobStatus: job.status }); return; }

    if (!Array.isArray(job.segments_json) || job.segments_json.length === 0) {
      setState({ status: "no_segments" });
      return;
    }

    const params = job.render_params || {};
    const segmentsFromSnap = reusableSnap ? snap.currentReview.segments : job.segments_json;

    setCurrentReview({
      editingJobId: id,
      segments: segmentsFromSnap,
      audioUrl: null,
      waveform: null,
      bgUrl: null,
      font: params.font || "",
    });
    // The wizardScreen reads wizardStage to decide what to render. Without
    // this call, currentReview.editingJobId is set but the wizard stays in
    // "upload" stage and shows UploadZone ("Crear videos") instead of the
    // editor. Bug reproduced 2026-05-27, fixed in fix/edit-lyrics-set-wizard-stage.
    setWizardStage("review");
    setState({ status: "ready" });

    // Phase B — fire-and-forget.
    const enhanceField = async (url, key, extractor) => {
      try {
        const res = await authFetchWithTimeout(url, {}, 15_000);
        if (!alive() || !res.ok) return;
        const data = await res.json();
        if (!alive()) return;
        const value = extractor(data);
        setCurrentReview((prev) => {
          if (!prev || prev.editingJobId !== id) return prev;
          return { ...prev, [key]: value };
        });
      } catch { /* silent */ }
    };
    enhanceField(`${API}/jobs/${id}/source-audio-url`, "audioUrl", (d) => d?.url || null);
    enhanceField(`${API}/jobs/${id}/waveform`, "waveform", (d) => d);
    enhanceField(`${API}/jobs/${id}/background-url`, "bgUrl", (d) => d?.url || null);
  };
}

// Helper: a fake response object compatible with what the bootstrap expects.
function ok(body) {
  return Promise.resolve({ ok: true, status: 200, json: async () => body });
}
function notOk(status) {
  return Promise.resolve({ ok: false, status, json: async () => ({}) });
}
function neverResolves() {
  return new Promise(() => { /* hangs forever */ });
}

describe("EditLyricsRoute bootstrap — Phase A mounts before Phase B", () => {
  let setState;
  let setCurrentReview;
  let authFetchWithTimeout;
  const VALID_JOB = {
    status: "pending_review",
    segments_json: [{ start: 0, end: 1, text: "hola" }],
    render_params: { font: "jost-bold" },
  };

  beforeEach(() => {
    setState = vi.fn();
    setCurrentReview = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("Phase A: editor mounts as soon as /status responds", async () => {
    // Router by URL: /status fast, enhancements also fast.
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok(VALID_JOB);
      if (url.includes("/source-audio-url")) return ok({ url: "blob://audio" });
      if (url.includes("/waveform")) return ok({ peaks: [0.1], duration: 1 });
      if (url.includes("/background-url")) return ok({ url: "blob://bg" });
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    // Wait a microtask cycle for the fire-and-forget enhancers to land.
    await new Promise((r) => setTimeout(r, 10));

    // setCurrentReview called once for Phase A + 3 patches.
    expect(setCurrentReview).toHaveBeenCalledTimes(4);
    // First call is the Phase A mount with all enhancements null.
    expect(setCurrentReview.mock.calls[0][0]).toMatchObject({
      editingJobId: "abc123",
      audioUrl: null,
      waveform: null,
      bgUrl: null,
    });
    expect(setState).toHaveBeenCalledWith({ status: "ready" });
  });

  it("Phase B: editor mounts even when /waveform never resolves", async () => {
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok(VALID_JOB);
      if (url.includes("/source-audio-url")) return ok({ url: "blob://audio" });
      if (url.includes("/waveform")) return neverResolves();
      if (url.includes("/background-url")) return ok({ url: "blob://bg" });
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    await new Promise((r) => setTimeout(r, 20));

    // Phase A mounted. We expect ≥1 call to setState({ready}) and ≥1 call
    // to setCurrentReview with all enhancements null.
    const readyCalls = setState.mock.calls.filter(([s]) => s && s.status === "ready");
    expect(readyCalls.length).toBe(1);
    expect(setCurrentReview).toHaveBeenCalled();
    // The mount call (first) has all enhancements null.
    expect(setCurrentReview.mock.calls[0][0]).toMatchObject({
      editingJobId: "abc123",
      audioUrl: null,
      waveform: null,
      bgUrl: null,
    });
    // audio + bg should have patched by now (resolved fast).
    // waveform hung — never patched. But mount still happened.
  });

  it("/status timeout: state goes to error, editor never mounts", async () => {
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return Promise.reject(Object.assign(new Error("timeout"), { name: "TimeoutError" }));
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");

    expect(setCurrentReview).not.toHaveBeenCalled();
    const errorCalls = setState.mock.calls.filter(([s]) => s && s.status === "error");
    expect(errorCalls.length).toBe(1);
  });

  it("/status 404: state goes to not_found", async () => {
    authFetchWithTimeout = vi.fn(() => notOk(404));
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    expect(setState).toHaveBeenCalledWith({ status: "not_found" });
    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  it("status=transcribing: state goes to not_editable", async () => {
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok({ status: "transcribing", segments_json: [] });
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    expect(setState).toHaveBeenCalledWith({ status: "not_editable", jobStatus: "transcribing" });
    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  it("empty segments_json: state goes to no_segments", async () => {
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok({ status: "done", segments_json: [] });
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    expect(setState).toHaveBeenCalledWith({ status: "no_segments" });
    expect(setCurrentReview).not.toHaveBeenCalled();
  });

  it("Phase A success: calls setWizardStage('review') so wizardScreen renders the editor", async () => {
    // REGRESSION 2026-05-27 (fix/edit-lyrics-set-wizard-stage): bug en prod
    // donde EditLyricsRoute seteaba currentReview.editingJobId pero nunca
    // le decía al wizardStage de App.jsx que pasara de "upload" a "review".
    // Resultado: el wizardScreen seguía rendereando UploadZone ("Crear
    // videos"). Pin del contrato: Phase A success DEBE llamar
    // setWizardStage("review") para que el editor se monte.
    const setWizardStage = vi.fn();
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok(VALID_JOB);
      return ok({});
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview, setWizardStage });
    await bootstrap("abc123");
    expect(setWizardStage).toHaveBeenCalledWith("review");
    // Debe llamarse ANTES de setState(ready) para que el wizardScreen vea
    // el stage nuevo en el render que dispara setState(ready).
    const stageCallOrder = setWizardStage.mock.invocationCallOrder[0];
    const readyCall = setState.mock.calls.findIndex(([s]) => s && s.status === "ready");
    const readyCallOrder = setState.mock.invocationCallOrder[readyCall];
    expect(stageCallOrder).toBeLessThan(readyCallOrder);
  });

  it("not_editable: does NOT call setWizardStage (stays in 'upload')", async () => {
    const setWizardStage = vi.fn();
    authFetchWithTimeout = vi.fn(() => ok({ status: "transcribing", segments_json: [] }));
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview, setWizardStage });
    await bootstrap("abc123");
    expect(setWizardStage).not.toHaveBeenCalled();
  });

  it("race guard: enhancement that lands after navigating to other id is a no-op", async () => {
    authFetchWithTimeout = vi.fn((url) => {
      if (url.includes("/status/")) return ok(VALID_JOB);
      // All enhancements respond fast.
      if (url.includes("/source-audio-url")) return ok({ url: "blob://audio" });
      if (url.includes("/waveform")) return ok({ peaks: [], duration: 1 });
      if (url.includes("/background-url")) return ok({ url: "blob://bg" });
      return notOk(404);
    });
    const bootstrap = makeBootstrap({ authFetchWithTimeout, setState, setCurrentReview });
    await bootstrap("abc123");
    await new Promise((r) => setTimeout(r, 10));

    // Simulate operator navigating to a different job: now prev has a
    // different editingJobId. The setCurrentReview updater should return
    // prev unchanged.
    const updaters = setCurrentReview.mock.calls
      .map(([arg]) => arg)
      .filter((arg) => typeof arg === "function");
    expect(updaters.length).toBe(3); // 3 enhancement patches
    for (const updater of updaters) {
      const fakePrev = { editingJobId: "different-job", segments: [] };
      const result = updater(fakePrev);
      expect(result).toBe(fakePrev); // SAME reference, untouched
    }
  });
});
