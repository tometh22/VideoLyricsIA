import { useCallback, useEffect, useRef } from "react";

export function useEditorAutosave({ enabled, segments, dirty, blocked, save, reconcile, onStatus }) {
  const segmentsRef = useRef(segments);
  const draftTimerRef = useRef(null);
  const checkpointTimerRef = useRef(null);
  const retryTimerRef = useRef(null);
  const retryCountRef = useRef(0);
  segmentsRef.current = segments;

  const clearTimers = useCallback(() => {
    for (const ref of [draftTimerRef, checkpointTimerRef, retryTimerRef]) {
      if (ref.current) window.clearTimeout(ref.current);
      ref.current = null;
    }
  }, []);

  const runSave = useCallback(async (checkpoint = "draft", isRetry = false, overrideSegments = null) => {
    if (!enabled || blocked) return { ok: false, reason: blocked ? "conflict" : "disabled" };
    let snapshot = overrideSegments || segmentsRef.current;
    if (isRetry && reconcile) {
      const state = await reconcile(snapshot);
      if (!state?.ok) {
        if (state?.reason === "conflict") onStatus?.("conflict", "conflict", { checkpoint, result: state });
        return state;
      }
      if (Array.isArray(state?.mergedSegments)) snapshot = state.mergedSegments;
    }
    onStatus?.("saving", null);
    const started = performance.now();
    const result = await save(snapshot, checkpoint);
    if (result?.ok) {
      retryCountRef.current = 0;
      onStatus?.("saved", null, { durationMs: performance.now() - started, checkpoint, result });
      return result;
    }
    if (result?.reason === "conflict") {
      onStatus?.("conflict", "conflict", { checkpoint, result });
      return result;
    }
    if (result?.reason === "merged" && Array.isArray(result.mergedSegments)) {
      // The first request was rejected because the base was stale, but the
      // three-way merge proved the edits were independent. Retry the rebased
      // document through the same serialized save queue.
      return runSave(checkpoint, false, result.mergedSegments);
    }
    const reason = result?.reason || "network";
    onStatus?.(reason === "offline" ? "offline" : "error", reason, { checkpoint, result });
    const delay = Math.min(30_000, 1_000 * (2 ** retryCountRef.current));
    retryCountRef.current += 1;
    retryTimerRef.current = window.setTimeout(() => runSave(checkpoint, true), delay + Math.random() * 250);
    return result;
  }, [blocked, enabled, onStatus, reconcile, save]);

  useEffect(() => {
    if (!enabled || !dirty || blocked) return undefined;
    if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current);
    if (checkpointTimerRef.current) window.clearTimeout(checkpointTimerRef.current);
    draftTimerRef.current = window.setTimeout(() => runSave("draft"), 800);
    checkpointTimerRef.current = window.setTimeout(() => runSave("autosave"), 5_000);
    return () => {
      if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current);
      if (checkpointTimerRef.current) window.clearTimeout(checkpointTimerRef.current);
    };
  }, [blocked, dirty, enabled, runSave, segments]);

  useEffect(() => {
    if (!enabled || !dirty || blocked) return undefined;
    const onOffline = () => onStatus?.("offline", "offline");
    const onOnline = () => {
      if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
      runSave("draft", true);
    };
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return () => {
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, [blocked, dirty, enabled, onStatus, runSave]);

  useEffect(() => clearTimers, [clearTimers]);

  const flush = useCallback(
    (checkpoint = "draft", overrideSegments = null) => runSave(checkpoint, false, overrideSegments),
    [runSave],
  );

  return { flush, clearTimers };
}
