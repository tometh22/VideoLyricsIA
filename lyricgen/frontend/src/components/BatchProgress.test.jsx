/**
 * Smoke tests for the SingleGeneratingHero "honest progress" rewrite.
 *
 * Regression for the 2026-05-26 operator complaint:
 *   - Step text used to cycle every 3.2s through ALL pipeline phases
 *     ("Transcribiendo lyrics" appeared even after the user had finished
 *     editing them — looked broken/confused).
 *   - "~8 min restantes" was hardcoded and never updated.
 *
 * The rewrite makes the component purely declarative: it reads
 * `current_step`, `step_text_es`, and `eta_s` from the SSE payload via
 * props. No setInterval. The text changes only when the worker
 * transitions; the bar advances only when progress advances; the ETA
 * comes from the backend's `step_eta.compute_eta_s` and refreshes per
 * SSE event.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";

// Stub the i18n hook to return the key as the translated string, so
// assertions can match either the literal fallback string or the key.
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback || _key }),
}));

// Stub mediaUrl — it touches `import.meta.env` which isn't reliable in
// the test runner and we don't render any media in these tests.
vi.mock("../mediaUrl", () => ({
  getDownloadUrl: vi.fn(),
  useMediaUrl: vi.fn(),
}));

import BatchProgress from "./BatchProgress";

function renderBatch(job) {
  return render(<BatchProgress jobs={[job]} />);
}

describe("SingleGeneratingHero (honest progress)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the backend-provided step_text_es directly (no cycling)", () => {
    renderBatch({
      job_id: "test-1",
      filename: "638 - Viejas Locas.wav",
      status: "processing",
      current_step: "background",
      step_text_es: "Generando el fondo cinematográfico",
      progress: 22,
      eta_s: 180,
    });

    expect(screen.getByText("Generando el fondo cinematográfico")).toBeTruthy();
  });

  it("does NOT cycle through different steps over time", () => {
    renderBatch({
      job_id: "test-2",
      filename: "638.wav",
      status: "processing",
      current_step: "background",
      step_text_es: "Generando el fondo cinematográfico",
      progress: 22,
      eta_s: 180,
    });

    // Before fix: setInterval(3200) rotated to a different label.
    // After fix: text is stable as long as the SSE payload doesn't change.
    act(() => {
      vi.advanceTimersByTime(15000);
    });
    expect(screen.queryByText("Aislando la voz del audio")).toBeNull();
    expect(screen.queryByText("Buscando la letra")).toBeNull();
    expect(screen.getByText("Generando el fondo cinematográfico")).toBeTruthy();
  });

  it("falls back to i18n step name when step_text_es is missing", () => {
    renderBatch({
      job_id: "test-3",
      filename: "x.wav",
      status: "processing",
      current_step: "short",
      // step_text_es absent — older worker version
      progress: 75,
      eta_s: 60,
    });
    // The i18n mock returns the key when no fallback is passed; the
    // production-time real translator returns the dictionary value.
    // Either way we exercise the per-step fallback branch.
    expect(screen.getByText("hero.step_short")).toBeTruthy();
  });

  it("renders a generic phrase when step is unknown", () => {
    renderBatch({
      job_id: "test-4",
      filename: "x.wav",
      status: "processing",
      current_step: "ghost_step_does_not_exist",
      progress: 50,
      eta_s: 30,
    });
    expect(screen.getByText("hero.step_default")).toBeTruthy();
  });

  it("formats eta_s as seconds when < 90s", () => {
    renderBatch({
      job_id: "test-5",
      filename: "x.wav",
      status: "processing",
      current_step: "upload",
      step_text_es: "Guardando en tu galería",
      progress: 96,
      eta_s: 45,
    });
    expect(screen.getByText(/~45 hero.eta_seconds/)).toBeTruthy();
  });

  it("formats eta_s as minutes when ≥ 90s with round-half-up", () => {
    renderBatch({
      job_id: "test-6",
      filename: "x.wav",
      status: "processing",
      current_step: "background",
      step_text_es: "Generando el fondo cinematográfico",
      progress: 22,
      eta_s: 180,  // 180s → (180+30)/60 = 3 min
    });
    expect(screen.getByText(/~3 hero.eta_minutes/)).toBeTruthy();
  });

  it("shows 'Casi listo' when eta_s is 0", () => {
    renderBatch({
      job_id: "test-7",
      filename: "x.wav",
      status: "processing",
      current_step: "upload",
      step_text_es: "Guardando en tu galería",
      progress: 99,
      eta_s: 0,
    });
    expect(screen.getByText("hero.eta_almost_done")).toBeTruthy();
  });
});

// Regression for the 2026-07-27 freeze: a silent /generate 4xx set the
// single-song job to status="error" and the hero kept rendering the
// "Construyendo tu video" spinner forever. The single-song view must be a
// TOTAL function over status — every terminal-but-not-successful state, plus
// any stalled/unknown state, renders a dead-end card with an escape hatch.
describe("single-song view is total over status (no frozen spinner)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("renders an error card (not the spinner) for a job in error, showing the message", () => {
    renderBatch({
      job_id: null,
      filename: "audio.mp3",
      status: "error",
      error: "La sesión expiró antes de generar. Re-subí el audio para regenerar.",
    });
    expect(screen.getByText(/La sesión expiró/)).toBeTruthy();
    expect(screen.getByText("hero.error_title")).toBeTruthy();
    // The generating spinner ("Construyendo tu video" label) must NOT render.
    expect(screen.queryByText("hero.label")).toBeNull();
  });

  it.each(["validation_failed", "rejected"])(
    "renders the error card for terminal-non-success status %s",
    (status) => {
      renderBatch({ job_id: "e1", filename: "x.wav", status, error: "Contenido bloqueado" });
      expect(screen.getByText("Contenido bloqueado")).toBeTruthy();
      expect(screen.queryByText("hero.label")).toBeNull();
    },
  );

  it("renders an escape card immediately for a ghost bg_preview_done job", () => {
    // This is the a52795cd8c98 shape: a terminal ghost that leaked into the
    // jobs array. It must never show the infinite spinner.
    renderBatch({ job_id: "g1", filename: "x.wav", status: "bg_preview_done", current_step: "cached", progress: 28 });
    expect(screen.getByText("hero.stalled_title")).toBeTruthy();
    expect(screen.queryByText("hero.label")).toBeNull();
  });

  it("swaps the spinner for the stalled card when the worker stops advancing", () => {
    renderBatch({
      job_id: "w1",
      filename: "x.wav",
      status: "processing",
      current_step: "background",
      step_text_es: "Generando el fondo cinematográfico",
      progress: 22,
      eta_s: 180,
    });
    // Before the watchdog trips, the spinner is shown.
    expect(screen.getByText("Generando el fondo cinematográfico")).toBeTruthy();
    act(() => { vi.advanceTimersByTime(46_000); });
    expect(screen.getByText("hero.stalled_title")).toBeTruthy();
    expect(screen.queryByText("Generando el fondo cinematográfico")).toBeNull();
  });

  it("never freezes on an unknown non-terminal status (escapes via watchdog)", () => {
    renderBatch({ job_id: "u1", filename: "x.wav", status: "quantum_flux", current_step: "background", progress: 30 });
    // Unknown status is treated as generating initially...
    expect(screen.getByText("hero.label")).toBeTruthy();
    // ...but the watchdog guarantees an escape.
    act(() => { vi.advanceTimersByTime(46_000); });
    expect(screen.getByText("hero.stalled_title")).toBeTruthy();
  });
});
