import { useCallback, useEffect, useRef } from "react";

export function useEditorAutosave({ enabled, segments, dirty, save, reconcile, onStatus, onMerged }) {
  const MAX_REBASE_ATTEMPTS = 3;
  // Tope de reintentos cuando el reconcile falla de forma persistente, para que
  // el backoff no encadene timers indefinidamente.
  const MAX_RECONCILE_RETRIES = 3;
  const segmentsRef = useRef(segments);
  const runtimeRef = useRef({ enabled, save, reconcile, onStatus, onMerged });
  const mountedRef = useRef(false);
  const draftTimerRef = useRef(null);
  const checkpointTimerRef = useRef(null);
  const retryTimerRef = useRef(null);
  const retryCountRef = useRef(0);
  segmentsRef.current = segments;
  runtimeRef.current = { enabled, save, reconcile, onStatus, onMerged };

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
    if (!mountedRef.current || !runtime.enabled) {
      return { ok: false, reason: "disabled" };
    }
    let snapshot = overrideSegments || segmentsRef.current;
    if (isRetry && runtime.reconcile) {
      const state = await runtime.reconcile(snapshot);
      if (!mountedRef.current) return { ok: false, reason: "unmounted" };
      if (!state?.ok) {
        const reason = state?.reason === "conflict" ? "server" : state?.reason || "server";
        runtimeRef.current.onStatus?.("error", reason, { checkpoint, result: state });
        // Antes la cadena moría en silencio acá: el reconcile fallaba, no se
        // armaba ningún timer, y el autosave quedaba muerto hasta que el
        // operador volviera a editar. Se reintenta con backoff, pero ACOTADO:
        // un reconcile que falla de forma persistente (endpoint caído, job
        // borrado) no debe generar una cadena infinita de timers. Agotado el
        // tope, el próximo cambio del operador rearma el ciclo normal.
        if (retryCountRef.current < MAX_RECONCILE_RETRIES) {
          const reconcileDelay = Math.min(30_000, 1_000 * (2 ** retryCountRef.current));
          retryCountRef.current += 1;
          if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current);
          retryTimerRef.current = window.setTimeout(() => {
            retryTimerRef.current = null;
            runSave(checkpoint, true);
          }, reconcileDelay + Math.random() * 250);
        }
        return { ...state, reason };
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
    if ((result?.reason === "merged" || result?.reason === "conflict")
      && Array.isArray(result.mergedSegments)) {
      // A stale request is always rebased before retrying. This retains the
      // local value for a same-line collision but still writes through the
      // backend CAS check, so no request can overwrite a newer revision raw.
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
    const reason = result?.reason === "conflict" ? "server" : result?.reason || "network";
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
    if (!enabled || !dirty) {
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
  }, [clearTimers, dirty, enabled, runSave, segments]);

  useEffect(() => {
    if (!enabled || !dirty) return undefined;
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
  }, [dirty, enabled, onStatus, runSave]);

  const flush = useCallback(
    (checkpoint = "draft", overrideSegments = null) => runSave(checkpoint, false, overrideSegments),
    [runSave],
  );

  const forceRecover = useCallback(
    (checkpoint = "manual") => runSave(checkpoint, true),
    [runSave],
  );

  return { flush, clearTimers, forceRecover };
}
