import { useCallback, useEffect, useRef } from "react";

export function useEditorAutosave({ enabled, segments, dirty, blocked, save, reconcile, onStatus, onMerged }) {
  const MAX_REBASE_ATTEMPTS = 3;
  const segmentsRef = useRef(segments);
  const runtimeRef = useRef({ enabled, blocked, save, reconcile, onStatus, onMerged });
  const mountedRef = useRef(false);
  const draftTimerRef = useRef(null);
  const checkpointTimerRef = useRef(null);
  const retryTimerRef = useRef(null);
  const retryCountRef = useRef(0);
  // A durable conflict blocks every save, including the manual "Guardar"
  // button, which returned `{ok:false}` without touching the network — the
  // operator saw a button that did nothing ("hay que apretar Guardar muchas
  // veces"). `forceRecover` is the ONLY sanctioned way past `blocked`: it
  // re-reads the remote document and three-way merges before writing, so it
  // can never clobber someone else's edit.
  const bypassBlockedRef = useRef(false);
  segmentsRef.current = segments;
  runtimeRef.current = { enabled, blocked, save, reconcile, onStatus, onMerged };

  const clearTimers = useCallback(() => {
    for (const ref of [draftTimerRef, checkpointTimerRef, retryTimerRef]) {
      if (ref.current) window.clearTimeout(ref.current);
      ref.current = null;
    }
  }, []);

  const runSave = useCallback(async (
    checkpoint = "draft",
    isRetry = false,
    overrideSegments = null,
    rebaseAttempts = 0,
  ) => {
    const runtime = runtimeRef.current;
    const blocked = runtime.blocked && !bypassBlockedRef.current;
    if (!mountedRef.current || !runtime.enabled || blocked) {
      return { ok: false, reason: runtime.blocked ? "conflict" : "disabled" };
    }
    let snapshot = overrideSegments || segmentsRef.current;
    if (isRetry && runtime.reconcile) {
      const state = await runtime.reconcile(snapshot);
      if (!mountedRef.current) return { ok: false, reason: "unmounted" };
      if (!state?.ok) {
        if (state?.reason === "conflict") {
          clearTimers();
          runtimeRef.current.onStatus?.("conflict", "conflict", { checkpoint, result: state });
          return state;
        }
        runtimeRef.current.onStatus?.("error", "server", { checkpoint, result: state });
        // Without this the retry chain died silently here: the reconcile
        // failed, no timer was armed, and autosave stayed dead until the
        // operator happened to edit again.
        const reconcileDelay = Math.min(30_000, 1_000 * (2 ** retryCountRef.current));
        retryCountRef.current += 1;
        if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
        retryTimerRef.current = window.setTimeout(() => {
          retryTimerRef.current = null;
          runSave(checkpoint, true);
        }, reconcileDelay + Math.random() * 250);
        return { ...state, reason: "server" };
      }
      if (Array.isArray(state?.mergedSegments)) {
        snapshot = state.mergedSegments;
        runtimeRef.current.onMerged?.(state.mergedSegments, state);
      }
    }
    runtimeRef.current.onStatus?.("saving", null);
    const started = performance.now();
    const result = await runtimeRef.current.save(snapshot, checkpoint);
    // A request already on the wire may settle after route navigation. Never
    // let that abandoned editor publish status or arm another retry timer.
    if (!mountedRef.current) return { ...result, abandoned: true };
    if (result?.ok) {
      retryCountRef.current = 0;
      if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
      runtimeRef.current.onStatus?.("saved", null, { durationMs: performance.now() - started, checkpoint, result });
      return result;
    }
    if (result?.reason === "conflict") {
      // A 409 on the same lyric is intentionally terminal for this draft.
      // Keep the local screen/draft intact and require an explicit recovery;
      // retrying here could overwrite the remote edit after a rebase.
      clearTimers();
      runtimeRef.current.onStatus?.("conflict", "conflict", { checkpoint, result });
      return result;
    }
    if (result?.reason === "merged" && Array.isArray(result.mergedSegments)) {
      // The first request was rejected because the base was stale, but the
      // three-way merge proved the edits were independent. Retry the rebased
      // document through the same serialized save queue. If a second writer
      // moves the document again, bounded retries keep the editor responsive
      // without ever bypassing the backend CAS check.
      if (rebaseAttempts < MAX_REBASE_ATTEMPTS) {
        runtimeRef.current.onMerged?.(result.mergedSegments, result);
        return runSave(checkpoint, false, result.mergedSegments, rebaseAttempts + 1);
      }
      const delay = Math.min(30_000, 1_000 * (2 ** retryCountRef.current));
      retryCountRef.current += 1;
      if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = window.setTimeout(
        () => runSave(checkpoint, true),
        delay + Math.random() * 250,
      );
      runtimeRef.current.onStatus?.("error", "server", { durationMs: performance.now() - started, checkpoint, result });
      return result;
    }
    const reason = result?.reason || "network";
    runtimeRef.current.onStatus?.(reason === "offline" ? "offline" : "error", reason, { checkpoint, result });
    const delay = Math.min(30_000, 1_000 * (2 ** retryCountRef.current));
    retryCountRef.current += 1;
    // Draft + checkpoint may fail close together. Keep one retry chain, not
    // one untracked timer per failure.
    if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      runSave(checkpoint, true);
    }, delay + Math.random() * 250);
    return result;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimers();
    };
  }, [clearTimers]);

  useEffect(() => {
    if (!enabled || !dirty || blocked) {
      clearTimers();
      return undefined;
    }
    if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current);
    if (checkpointTimerRef.current) window.clearTimeout(checkpointTimerRef.current);
    if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
    retryTimerRef.current = null;
    retryCountRef.current = 0;
    draftTimerRef.current = window.setTimeout(() => runSave("draft"), 800);
    checkpointTimerRef.current = window.setTimeout(() => runSave("autosave"), 5_000);
    return () => {
      if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current);
      if (checkpointTimerRef.current) window.clearTimeout(checkpointTimerRef.current);
    };
  }, [blocked, clearTimers, dirty, enabled, runSave, segments]);

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

  const flush = useCallback(
    (checkpoint = "draft", overrideSegments = null) => runSave(checkpoint, false, overrideSegments),
    [runSave],
  );

  // Explicit operator recovery from a durable conflict. Runs with `isRetry`
  // so the reconcile + three-way merge happens BEFORE the write: if the
  // remote moved, the merged document is what gets saved. If the merge still
  // conflicts, the status stays `conflict` and the banner keeps offering the
  // reload path — this never force-overwrites a remote edit.
  const forceRecover = useCallback(async (checkpoint = "manual") => {
    bypassBlockedRef.current = true;
    try {
      return await runSave(checkpoint, true);
    } finally {
      bypassBlockedRef.current = false;
    }
  }, [runSave]);

  return { flush, clearTimers, forceRecover };
}
