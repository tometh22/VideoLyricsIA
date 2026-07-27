/**
 * telemetryTrack: cola en memoria + flush batched a /telemetry/events.
 * Contrato: best-effort (errores tragados), no-op sin token o con
 * features.telemetry === false, cap de batch 25.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { track, flushTelemetry, _resetForTests, _queueForTests } from "./telemetryTrack";

describe("telemetryTrack", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    _resetForTests();
    global.fetch = vi.fn(() => Promise.resolve({ ok: true }));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("sin token es no-op", () => {
    track("wizard.step", { step_to: 2 });
    expect(_queueForTests().length).toBe(0);
  });

  it("con telemetry === false es no-op", () => {
    localStorage.setItem("genly_token", "t");
    localStorage.setItem("genly_user", JSON.stringify({ features: { telemetry: false } }));
    track("wizard.step", { step_to: 2 });
    expect(_queueForTests().length).toBe(0);
  });

  it("encola y flushea en batch a los 10s", () => {
    localStorage.setItem("genly_token", "t");
    track("wizard.step", { step_from: 1, step_to: 2 });
    track("wizard.generate", { batch_size: 1, mode: "direct" });
    expect(_queueForTests().length).toBe(2);
    expect(global.fetch).not.toHaveBeenCalled();

    vi.advanceTimersByTime(10_000);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toMatch(/\/telemetry\/events$/);
    expect(opts.keepalive).toBe(true);
    expect(opts.headers.Authorization).toBe("Bearer t");
    const body = JSON.parse(opts.body);
    expect(body.events).toEqual([
      { type: "wizard.step", data: { step_from: 1, step_to: 2 } },
      { type: "wizard.generate", data: { batch_size: 1, mode: "direct" } },
    ]);
    expect(_queueForTests().length).toBe(0);
  });

  it("flushTelemetry dispara el envío inmediato (caso visibilitychange)", () => {
    localStorage.setItem("genly_token", "t");
    track("wizard.scene_mode", { mode: "library" });
    flushTelemetry();
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("respeta el cap de 25 por request y reprograma el resto", () => {
    localStorage.setItem("genly_token", "t");
    for (let i = 0; i < 30; i++) track("wizard.step", { step_to: 2 });
    flushTelemetry();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(global.fetch.mock.calls[0][1].body).events.length).toBe(25);
    expect(_queueForTests().length).toBe(5);
  });
});
