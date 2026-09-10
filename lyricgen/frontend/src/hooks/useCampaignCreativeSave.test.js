import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import useCampaignCreativeSave from "./useCampaignCreativeSave";

afterEach(() => vi.useRealTimers());
const config = onSave => ({ identity: "campaign/song", revision: 4, settings: { font: "anton" }, file: null, onSave });

describe("campaign creative autosave", () => {
  it("does not pin on opening; debounces edits and flushes before exit with the new revision", async () => {
    vi.useFakeTimers();
    const onSave = vi.fn().mockResolvedValueOnce({ revision: 5 }).mockResolvedValueOnce({ revision: 6 });
    const props = config(onSave);
    const { result, rerender } = renderHook(p => useCampaignCreativeSave(p), { initialProps: props });
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(onSave).not.toHaveBeenCalled();
    rerender({ ...props, settings: { font: "poppins-bold" } });
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(onSave).toHaveBeenLastCalledWith({ font: "poppins-bold" }, 4, null);
    rerender({ ...props, settings: { font: "poppins-bold", font_scale: 1.2 } });
    await act(async () => result.current.save());
    expect(onSave).toHaveBeenLastCalledWith({ font: "poppins-bold", font_scale: 1.2 }, 5, null);
    expect(result.current.status).toBe("saved");
  });

  it("serializes edits typed during a save and duplicate flushes", async () => {
    let finish;
    const onSave = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue({ revision: 6 });
    const props = config(onSave);
    const { result, rerender } = renderHook(p => useCampaignCreativeSave(p), { initialProps: props });
    rerender({ ...props, settings: { font: "new" } });
    let first, second;
    act(() => { first = result.current.save(); second = result.current.save(); });
    rerender({ ...props, settings: { font: "newer" } });
    await act(async () => { finish({ revision: 5 }); await Promise.all([first, second]); });
    expect(onSave).toHaveBeenCalledTimes(2);
    expect(onSave).toHaveBeenLastCalledWith({ font: "newer" }, 5, null);
  });

  it("surfaces conflicts and never borrows a revision when changing songs", async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error("Configuración cambiada")).mockResolvedValue({ revision: 11 });
    const props = config(onSave);
    const { result, rerender } = renderHook(p => useCampaignCreativeSave(p), { initialProps: props });
    rerender({ ...props, settings: { font: "new" } });
    await act(async () => { await expect(result.current.save()).rejects.toThrow("Configuración cambiada"); });
    expect(result.current.status).toBe("error");
    rerender({ ...props, identity: "campaign/other", revision: 10 });
    rerender({ ...props, identity: "campaign/other", revision: 10, settings: { font: "new" } });
    await act(async () => result.current.save());
    expect(onSave).toHaveBeenLastCalledWith({ font: "new" }, 10, null);
  });
});
