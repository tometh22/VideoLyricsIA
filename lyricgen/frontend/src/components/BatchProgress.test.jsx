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
