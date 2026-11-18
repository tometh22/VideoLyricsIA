import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useJobBeats, beatPulseAt, _clearJobBeatsCache } from "./useJobBeats";

const ok = (data) => ({ ok: true, json: async () => data });

describe("useJobBeats", () => {
  beforeEach(() => {
    _clearJobBeatsCache();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("pide /jobs/{id}/beats con auth y devuelve los beats ordenados", async () => {
    global.fetch = vi.fn().mockResolvedValue(ok({ bpm: 120, beats: [1.0, 0.5, 1.5] }));
    const { result } = renderHook(() => useJobBeats("job1", true, {
      api: "http://api",
      authHeaders: () => ({ Authorization: "Bearer t" }),
    }));
    await waitFor(() => expect(result.current).toEqual([0.5, 1.0, 1.5]));
    expect(global.fetch).toHaveBeenCalledWith("http://api/jobs/job1/beats", {
      headers: { Authorization: "Bearer t" },
    });
  });

  it("deshabilitado o sin job no pide nada", () => {
    global.fetch = vi.fn();
    const a = renderHook(() => useJobBeats("job1", false));
    const b = renderHook(() => useJobBeats(null, true));
    expect(a.result.current).toBeNull();
    expect(b.result.current).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("pide una sola vez por job (cache de módulo)", async () => {
    global.fetch = vi.fn().mockResolvedValue(ok({ bpm: 100, beats: [0.6, 1.2] }));
    const first = renderHook(() => useJobBeats("job2", true));
    await waitFor(() => expect(first.result.current).toEqual([0.6, 1.2]));
    const second = renderHook(() => useJobBeats("job2", true));
    await waitFor(() => expect(second.result.current).toEqual([0.6, 1.2]));
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["404", () => Promise.resolve({ ok: false, status: 404, json: async () => ({}) })],
    ["422", () => Promise.resolve({ ok: false, status: 422, json: async () => ({}) })],
    ["red", () => Promise.reject(new Error("network"))],
    ["sin beats", () => Promise.resolve(ok({ bpm: 0, beats: [] }))],
  ])("falla %s → null en silencio y no reintenta", async (_name, impl) => {
    global.fetch = vi.fn().mockImplementation(impl);
    const { result } = renderHook(() => useJobBeats("job3", true));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 10));
    expect(result.current).toBeNull();
    renderHook(() => useJobBeats("job3", true));
    await new Promise((r) => setTimeout(r, 10));
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("beatPulseAt", () => {
  const beats = [1.0, 1.25, 1.5, 1.75, 2.0];

  it("usa uno de cada dos beats, como el backend (_beats_in every=2)", () => {
    // 1.0, 1.5, 2.0 son de pulso; 1.25 y 1.75 no.
    expect(beatPulseAt(beats, 1.05).pulsing).toBe(true);
    expect(beatPulseAt(beats, 1.3).pulsing).toBe(false);
    expect(beatPulseAt(beats, 1.55).pulsing).toBe(true);
    expect(beatPulseAt(beats, 1.8).pulsing).toBe(false);
  });

  it("sólo dentro de ~0,12 s después del beat", () => {
    expect(beatPulseAt(beats, 1.5).pulsing).toBe(true);
    expect(beatPulseAt(beats, 1.61).pulsing).toBe(true);
    expect(beatPulseAt(beats, 1.66).pulsing).toBe(false);
  });

  it("antes del primer beat no pulsa; sin beats devuelve null", () => {
    expect(beatPulseAt(beats, 0.5)).toEqual({ pulsing: false, since: null });
    expect(beatPulseAt(null, 1)).toBeNull();
    expect(beatPulseAt([], 1)).toBeNull();
  });
});
