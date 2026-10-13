/**
 * SSE-driven progress for a job_id. Reads current_step + progress emitted
 * by the backend (_step() helper in main.py for transcription, update_job()
 * for render). Returns { currentStep, progress, error, status } that the
 * TranscribingProgress component renders.
 *
 * Falls back to polling /status/{jobId} every 3s if SSE drops. Closes on
 * unmount or when the job hits a terminal state.
 */

import { useEffect, useRef, useState } from "react";
import { fetchSse, SseUnauthorizedError } from "../lib/fetchSse";
import { isTerminalStatus } from "../lib/jobStatus";

export default function useJobProgress(jobId, { api, token } = {}) {
  const [state, setState] = useState({
    currentStep: null,
    progress: 0,
    error: null,
    status: null,
  });
  const esRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    if (!jobId || !api) return undefined;

    const forceReauth = () => {
      try {
        localStorage.removeItem("genly_token");
        localStorage.removeItem("genly_user");
      } catch { /* storage unavailable */ }
      if (import.meta.env.MODE !== "test") window.location.replace("/login");
    };

    const close = () => {
      if (esRef.current?.close) {
        try { esRef.current.close(); } catch { /* noop */ }
        esRef.current = null;
      }
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };

    const apply = (data) => {
      setState((prev) => ({
        currentStep: data.current_step ?? prev.currentStep,
        progress: typeof data.progress === "number" ? data.progress : prev.progress,
        error: data.error ?? prev.error,
        status: data.status ?? prev.status,
      }));
      if (data.status && isTerminalStatus(data.status)) close();
    };

    const startPolling = () => {
      // /status/{id} returns the same row fields. Lower frequency than SSE
      // because each call is a full DB query; 3s matches the existing pattern.
      const tick = async () => {
        try {
          const res = await fetch(`${api}/status/${jobId}`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          });
          if (res.status === 401) { forceReauth(); close(); return; }
          if (!res.ok) return;
          apply(await res.json());
        } catch { /* ignore transient network */ }
      };
      tick();
      pollRef.current = setInterval(tick, 3000);
    };

    const controller = new AbortController();
    esRef.current = { close: () => controller.abort() };
    fetchSse(`${api}/events/${jobId}`, {
      token,
      signal: controller.signal,
      onMessage: apply,
      onEvent: (name, data) => {
        if (name === "unauthorized") {
          setState((prev) => ({ ...prev, error: data?.reason || "unauthorized" }));
          controller.abort();
          forceReauth();
        }
      },
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if (error instanceof SseUnauthorizedError) {
        setState((prev) => ({ ...prev, error: "unauthorized" }));
        forceReauth();
      }
      startPolling();
    });

    return () => {
      close();
    };
  }, [jobId, api, token]);

  return state;
}
