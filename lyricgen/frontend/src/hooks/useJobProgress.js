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

const TERMINAL = new Set([
  "done",
  "pending_review",
  "error",
  "validation_failed",
  "rejected",
]);

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
      if (data.status && TERMINAL.has(data.status)) close();
    };

    const startPolling = () => {
      // /status/{id} returns the same row fields. Lower frequency than SSE
      // because each call is a full DB query; 3s matches the existing pattern.
      const tick = async () => {
        try {
          const res = await fetch(`${api}/status/${jobId}`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          });
          if (!res.ok) return;
          apply(await res.json());
        } catch { /* ignore transient network */ }
      };
      tick();
      pollRef.current = setInterval(tick, 3000);
    };

    try {
      const url = token
        ? `${api}/events/${jobId}?token=${encodeURIComponent(token)}`
        : `${api}/events/${jobId}`;
      const es = new EventSource(url);
      esRef.current = es;
      es.onmessage = (ev) => {
        try { apply(JSON.parse(ev.data)); }
        catch { /* malformed event */ }
      };
      es.onerror = () => {
        // EventSource auto-reconnects; if it keeps failing, fall back to polling.
        if (es.readyState === EventSource.CLOSED) {
          esRef.current = null;
          startPolling();
        }
      };
    } catch {
      startPolling();
    }

    return close;
  }, [jobId, api, token]);

  return state;
}
