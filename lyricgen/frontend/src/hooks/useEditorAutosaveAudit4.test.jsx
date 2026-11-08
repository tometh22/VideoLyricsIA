import { useState } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEditorAutosave } from "./useEditorAutosave";
import { useEditorDocument } from "./useEditorDocument";

const row = (id, text) => ({ segment_id: id, start: id === "a" ? 0 : 2, end: id === "a" ? 1 : 3, text });
const reply = (body, status = 200) => ({ ok: status < 400, status, clone: () => ({ json: async () => body }) });
afterEach(() => vi.useRealTimers());

describe("fourth audit: durable flush races", () => {
  it("does not finish a navigation flush while text typed during that save remains unpersisted", async () => {
    const initial = [row("a", "Texto al salir")];
    const latest = [row("a", "Texto al salir con corrección tardía")];
    let release;
    const first = new Promise(resolve => { release = resolve; });
    const save = vi.fn().mockReturnValueOnce(first).mockImplementation(async segments => ({ ok: true, segments }));
    const { result, rerender } = renderHook(({ segments }) => useEditorAutosave({
      enabled: true, dirty: false, segments, save,
    }), { initialProps: { segments: initial } });
    let pending;
    act(() => { pending = result.current.flush("manual"); });
    rerender({ segments: latest });
    let outcome;
    await act(async () => {
      release({ ok: true, segments: initial });
      outcome = await pending;
    });
    expect(save.mock.calls.at(-1)[0]).toEqual(latest);
    expect(outcome.segments).toEqual(latest);
  });

  it("keeps an explicit approval snapshot exact even when newer text arrives", async () => {
    const approved = [row("a", "Snapshot aprobado")];
    const latest = [row("a", "Todavía sin confirmar")];
    let release;
    const save = vi.fn(() => new Promise(resolve => { release = resolve; }));
    const { result, rerender } = renderHook(({ segments }) => useEditorAutosave({
      enabled: true, dirty: false, segments, save,
    }), { initialProps: { segments: approved } });
    let pending;
    act(() => { pending = result.current.flush("manual", approved); });
    rerender({ segments: latest });
    await act(async () => { release({ ok: true, segments: approved }); await pending; });
    expect(save).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledWith(approved, "manual");
  });

  it("keeps navigation blocked if edits continue through every bounded drain attempt", async () => {
    const releases = [];
    const onStatus = vi.fn();
    const save = vi.fn(segments => new Promise(resolve => {
      releases.push(() => resolve({ ok: true, segments }));
    }));
    const { result, rerender } = renderHook(({ segments }) => useEditorAutosave({
      enabled: true, dirty: false, segments, save, onStatus,
    }), { initialProps: { segments: [row("a", "Primera")] } });
    let pending;
    act(() => { pending = result.current.flush("manual"); });
    for (let index = 0; index < 3; index += 1) {
      expect(save).toHaveBeenCalledTimes(index + 1);
      rerender({ segments: [row("a", `Nueva corrección ${index + 1}`)] });
      await act(async () => { releases[index](); });
    }
    const outcome = await pending;
    expect(save).toHaveBeenCalledTimes(3);
    expect(outcome).toEqual({ ok: false, reason: "pending-changes" });
    expect(onStatus).toHaveBeenLastCalledWith("local", null);
  });

  it("preserves both the newest local text and remote-only changes with two queued flushes", async () => {
    const initial = [row("a", "Original A"), row("b", "Original B")];
    const local = [row("a", "Edición A"), initial[1]];
    const latest = [row("a", "Edición A más nueva"), initial[1]];
    const remote = [initial[0], row("b", "Edición B remota")];
    let release;
    const conflict = new Promise(resolve => { release = resolve; });
    let revision = 1;
    let stored = remote;
    const patches = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock")) return reply({ released: true });
      if (!options.method) return reply({ job_id: "audit4", revision: 0, segments: initial, lock: { active: false } });
      const body = JSON.parse(options.body);
      patches.push(body);
      if (patches.length === 1) return conflict;
      if (body.base_revision !== revision) return reply({ server_revision: revision, server_segments: stored }, 409);
      stored = body.segments;
      revision += 1;
      return reply({ revision });
    });
    const { result } = renderHook(() => {
      const editor = useEditorDocument({ jobId: "audit4", enabled: true, request });
      const [segments, setSegments] = useState(local);
      const autosave = useEditorAutosave({ enabled: !editor.loading, dirty: false,
        segments, save: editor.save, reconcile: editor.reconcile, onMerged: setSegments });
      return { editor, autosave, setSegments };
    });
    await waitFor(() => expect(result.current.editor.loading).toBe(false));
    let first, second;
    act(() => { first = result.current.autosave.flush("draft"); });
    await waitFor(() => expect(patches).toHaveLength(1));
    act(() => result.current.setSegments(latest));
    act(() => { second = result.current.autosave.flush("manual"); });
    await act(async () => {
      release(reply({ server_revision: 1, server_segments: remote }, 409));
      await first;
      await second;
    });
    expect(stored.map(segment => segment.text)).toEqual(["Edición A más nueva", "Edición B remota"]);
  });
});
