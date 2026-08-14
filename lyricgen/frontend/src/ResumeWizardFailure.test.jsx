/**
 * Regression test for hotfix 2026-05-31 — `resumeWizard()` silent failure.
 *
 * Bug observed in production (UMG, Agus.Cafisi, 01:28 ART): operator
 * clicks "Continuar wizard" from the resume banner → wizard navigates
 * to /new with the empty upload state ("Arrastrá archivos MP3 o WAV")
 * → operator loses context, no alert explains why.
 *
 * Root cause: `App.jsx::resumeWizard()` had `try { … } finally { … }`
 * with NO `catch`. If any of the rehydrate calls threw (snapshot from
 * an older bundle version, JSON shape mismatch, queue entry without
 * the expected fields, etc.), the exception bubbled out of the
 * useCallback. `setResumableWizard(null)` had already run by then —
 * banner gone — but `navigate("/new")` never fired (it's the last
 * line of the try block). The operator was left wherever they were
 * with state half-cleared.
 *
 * Why this is a 2026-05-31 hotfix specifically: PR #493 (Service
 * Worker) merged the same day. The SW caches `/assets/*` and on a
 * deploy can serve a stale chunk just long enough for the page to
 * boot with the OLD code but a NEW sessionStorage snapshot shape (or
 * vice versa). Without a catch, that shape mismatch is silent.
 *
 * This test pins the SEMANTIC contract of the catch path:
 *   1. Detect the shape that throws (rehydrate raises)
 *   2. Clear persistence
 *   3. Reset wizard state
 *   4. Show alert with i18n keys (wizard.resume_failed_*)
 *   5. Redirect to /new with replace=true
 *
 * Mirror inline so the test doesn't drag the entire App component in.
 * Keep this in sync with App.jsx::resumeWizard.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Inline mirror of the resume function's safety wrapper.
// Mirrors the SEMANTICS of the catch block, not the literal JSX.
function safeResume({
  snap,
  rehydrate,
  setters,
  navigate,
  alert,
  t,
  clearPersistence,
  captureException,
}) {
  if (!snap) {
    setters.setResumableWizard(null);
    return { outcome: "no_snapshot" };
  }
  try {
    // Pretend to run all the setters in order. `rehydrate` is a single
    // injectable function so the test can make it throw deterministically.
    const review = rehydrate(snap.currentReview);
    setters.setCurrentReview(review);
    setters.setWizardStage(snap.wizardStage || "review");
    setters.setResumableWizard(null);
    navigate("/new");
    return { outcome: "ok" };
  } catch (err) {
    captureException(err, {
      tags: { feature: "wizard-resume" },
      extra: { snapKeys: Object.keys(snap || {}) },
    });
    clearPersistence();
    setters.setCurrentReview(null);
    setters.setApprovedJobs([]);
    setters.setReviewQueue([]);
    setters.setFiles([]);
    setters.setResumableWizard(null);
    setters.setWizardStage("upload");
    alert({
      title: t("wizard.resume_failed_title") || "No pudimos retomar tu sesión",
      description: t("wizard.resume_failed_desc") ||
        "El estado guardado no es compatible con esta versión. Empezamos limpio.",
      tone: "warning",
    });
    navigate("/new", { replace: true });
    return { outcome: "recovered", err };
  }
}

describe("resumeWizard failure recovery (2026-05-31 hotfix)", () => {
  let mocks;
  beforeEach(() => {
    mocks = {
      setters: {
        setCurrentReview: vi.fn(),
        setApprovedJobs: vi.fn(),
        setReviewQueue: vi.fn(),
        setFiles: vi.fn(),
        setResumableWizard: vi.fn(),
        setWizardStage: vi.fn(),
      },
      navigate: vi.fn(),
      alert: vi.fn(),
      clearPersistence: vi.fn(),
      captureException: vi.fn(),
      t: vi.fn((key) => key), // returns key, falsy fallback hits the OR
    };
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("no snapshot → only banner dismissal, no nav/alert", () => {
    const out = safeResume({
      snap: null,
      rehydrate: () => ({}),
      ...mocks,
    });
    expect(out.outcome).toBe("no_snapshot");
    expect(mocks.setters.setResumableWizard).toHaveBeenCalledWith(null);
    expect(mocks.navigate).not.toHaveBeenCalled();
    expect(mocks.alert).not.toHaveBeenCalled();
    expect(mocks.clearPersistence).not.toHaveBeenCalled();
  });

  it("rehydrate succeeds → happy path navigates /new, no alert", () => {
    const snap = { currentReview: { foo: 1 }, wizardStage: "review" };
    const out = safeResume({
      snap,
      rehydrate: (r) => r,
      ...mocks,
    });
    expect(out.outcome).toBe("ok");
    expect(mocks.setters.setCurrentReview).toHaveBeenCalledWith({ foo: 1 });
    expect(mocks.setters.setWizardStage).toHaveBeenCalledWith("review");
    expect(mocks.navigate).toHaveBeenCalledWith("/new");
    expect(mocks.alert).not.toHaveBeenCalled();
    expect(mocks.clearPersistence).not.toHaveBeenCalled();
  });

  it("rehydrate throws → clears persistence + alerts + redirects clean", () => {
    const snap = { currentReview: { incompatible: "shape" } };
    const boom = new Error("Cannot read properties of undefined");
    const out = safeResume({
      snap,
      rehydrate: () => { throw boom; },
      ...mocks,
    });
    expect(out.outcome).toBe("recovered");
    expect(out.err).toBe(boom);

    // Persistence + state must be fully cleared so the next session
    // doesn't trip on the same incompatible snapshot again.
    expect(mocks.clearPersistence).toHaveBeenCalled();
    expect(mocks.setters.setCurrentReview).toHaveBeenCalledWith(null);
    expect(mocks.setters.setApprovedJobs).toHaveBeenCalledWith([]);
    expect(mocks.setters.setReviewQueue).toHaveBeenCalledWith([]);
    expect(mocks.setters.setFiles).toHaveBeenCalledWith([]);
    expect(mocks.setters.setResumableWizard).toHaveBeenCalledWith(null);
    expect(mocks.setters.setWizardStage).toHaveBeenCalledWith("upload");

    // The user MUST see an explanation — silent recovery is the bug.
    expect(mocks.alert).toHaveBeenCalledTimes(1);
    const alertArg = mocks.alert.mock.calls[0][0];
    expect(alertArg.tone).toBe("warning");
    expect(alertArg.title).toBeTruthy();
    expect(alertArg.description).toBeTruthy();

    // Sentry breadcrumb for triage. snapKeys lets us spot which shape
    // was incompatible without leaking PII.
    expect(mocks.captureException).toHaveBeenCalledWith(boom, expect.objectContaining({
      tags: expect.objectContaining({ feature: "wizard-resume" }),
      extra: expect.objectContaining({ snapKeys: expect.any(Array) }),
    }));

    // Redirect with replace=true so the broken state doesn't end up
    // in the history (back button would just retry the crash).
    expect(mocks.navigate).toHaveBeenLastCalledWith("/new", { replace: true });
  });

  it("rehydrate throws null-ish error → still recovers (no crash on err.message)", () => {
    const snap = { currentReview: {} };
    // Some thrown values aren't Errors (e.g. throw "string", throw undefined)
    const out = safeResume({
      snap,
      rehydrate: () => { throw null; },
      ...mocks,
    });
    expect(out.outcome).toBe("recovered");
    expect(mocks.clearPersistence).toHaveBeenCalled();
    expect(mocks.alert).toHaveBeenCalled();
  });
});
