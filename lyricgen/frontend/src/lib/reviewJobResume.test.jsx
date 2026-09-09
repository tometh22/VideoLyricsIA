import { StrictMode, useEffect, useRef } from "react";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { beginReviewResume } from "./reviewJobRoute";

describe("review deep-link resume lifecycle", () => {
  it("restarts after StrictMode cleanup and publishes only the live response", async () => {
    const responses = [];
    const fetchStatus = vi.fn(() => new Promise(resolve => responses.push(resolve)));
    const mountEditor = vi.fn();
    renderHook(() => {
      const attempts = useRef(null);
      useEffect(() => {
        const attempt = beginReviewResume(attempts, "song");
        if (!attempt) return;
        fetchStatus().then(job => { if (!attempt.cancelled) mountEditor(job); });
        return () => attempt.cancel();
      }, []);
    }, { wrapper: StrictMode });
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    await act(async () => { responses[0]({ title: "cancelled response" }); });
    expect(mountEditor).not.toHaveBeenCalled();
    await act(async () => { responses[1]({ title: "current song" }); });
    expect(mountEditor).toHaveBeenCalledExactlyOnceWith({ title: "current song" });
  });

  it("a cancelled older request cannot release the next song's attempt", () => {
    const attempts = { current: null };
    const old = beginReviewResume(attempts, "old");
    const current = beginReviewResume(attempts, "current");
    old.cancel();
    expect(attempts.current).toBe(current);
    expect(beginReviewResume(attempts, "current")).toBeNull();
    current.cancel();
    expect(beginReviewResume(attempts, "current")).not.toBeNull();
  });
});
