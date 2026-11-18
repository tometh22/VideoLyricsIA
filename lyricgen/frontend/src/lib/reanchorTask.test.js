import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pollReanchorTask } from "./reanchorTask";
import { reanchorFailureMessage, shouldRecoverReanchor } from "./reanchorResult";

const response = (record, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => record,
});

describe("reanchor task lifetime", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(0); });
  afterEach(() => vi.useRealTimers());

  it.each([
    { ok: false, reason: "declined", revision: 1 },
    { ok: true, revision: 2, segments: [{ text: "texto verificado", start: 1, end: 3 }] },
  ])("delivers the exact task result after minute 12: $ok", async (payload) => {
    const request = vi.fn(async () => response(Date.now() < 863000
      ? { status: "running" }
      : { status: "done", http_status: 200, payload }));
    const waiting = pollReanchorTask({ taskId: "slow-task", request });
    await vi.advanceTimersByTimeAsync(865000);
    expect(await waiting).toEqual({ ...payload, terminal: true });
    expect(shouldRecoverReanchor(await waiting)).toBe(false);
    const reads = request.mock.calls.length;
    await vi.advanceTimersByTimeAsync(8 * 60 * 1000);
    expect(request).toHaveBeenCalledTimes(reads);
  });

  it("does not recover an unrelated document revision after a known task expires", async () => {
    const waiting = pollReanchorTask({ taskId: "running-task",
      request: async () => response({ status: "running" }), timeoutMs: 100, pollMs: 10 });
    await vi.advanceTimersByTimeAsync(100);
    const result = await waiting;
    expect(result).toEqual({ ok: false, reason: "task-timeout", taskId: "running-task" });
    expect(shouldRecoverReanchor(result)).toBe(false);
    expect(reanchorFailureMessage(result, () => "")).toContain("No se pudo confirmar");
  });

  it.each(["fetch", "body"])("bounds a hung %s, including transports that ignore abort", async (hung) => {
    const signals = [];
    const request = async ({ signal }) => {
      signals.push(signal);
      const forever = new Promise(() => {});
      return hung === "fetch" ? forever : { ok: true, status: 200, json: () => forever };
    };
    const waiting = pollReanchorTask({ taskId: "hung-task", request,
      timeoutMs: 100, pollMs: 10, readTimeoutMs: 30 });
    await vi.advanceTimersByTimeAsync(100);
    expect((await waiting).reason).toBe("task-timeout");
    expect(signals.length).toBeGreaterThan(0);
    expect(signals.every((s) => s.aborted)).toBe(true);
  });

  it("retains completed HTTP failures instead of starting revision recovery", async () => {
    const waiting = pollReanchorTask({ taskId: "failed-task", pollMs: 10,
      request: async () => response({ status: "done", http_status: 409,
        payload: { detail: "El audio original ya no está disponible para este job." } }) });
    await vi.advanceTimersByTimeAsync(10);
    const result = await waiting;
    expect(result).toMatchObject({ ok: false, status: 409, terminal: true });
    expect(shouldRecoverReanchor(result)).toBe(false);
  });

  it("handles transient failures and still reads the original task", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(response(null, 503))
      .mockResolvedValue(response({ status: "done", http_status: 200,
        payload: { ok: false, reason: "declined" } }));
    const waiting = pollReanchorTask({ taskId: "same-task", request, pollMs: 10 });
    await vi.advanceTimersByTimeAsync(30);
    expect(await waiting).toMatchObject({ reason: "declined", terminal: true });
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("does not use an autosave as proof that a disappeared task succeeded", async () => {
    const waiting = pollReanchorTask({ taskId: "lost-task", pollMs: 10,
      request: async () => response(null, 404) });
    await vi.advanceTimersByTimeAsync(10);
    const result = await waiting;
    expect(result).toMatchObject({ reason: "task-lost", taskId: "lost-task" });
    expect(shouldRecoverReanchor(result)).toBe(false);
    expect(reanchorFailureMessage(result, () => "")).toContain("No se pudo confirmar");
  });
});
