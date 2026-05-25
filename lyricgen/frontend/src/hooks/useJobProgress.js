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
      // INCIDENT (audit 2026-05-24): the previous fallback only fired
      // when `es.readyState === EventSource.CLOSED`. But browsers park
      // in CONNECTING when the server returns 500 chronic — they retry
      // forever, never reach CLOSED, and the polling fallback never
      // ran. UI stayed at progress=0 indefinitely.
      //
      // Now we treat ANY onerror burst as a signal to start polling in
      // parallel (the polling does no harm even if SSE eventually
      // reconnects — `apply` is idempotent and both sources read the
      // same row). Additionally, a wall-clock timeout: if we haven't
      // received any event within 8 seconds of opening, start polling
      // proactively so the user always sees motion.
      let openedAt = Date.now();
      let receivedAny = false;
      let pollingStarted = false;
      const startPollingOnce = () => {
        if (pollingStarted) return;
        pollingStarted = true;
        startPolling();
      };
      const noEventTimer = setTimeout(() => {
        if (!receivedAny) startPollingOnce();
      }, 8000);
      es.onmessage = (ev) => {
        receivedAny = true;
        try { apply(JSON.parse(ev.data)); }
        catch { /* malformed event */ }
      };
      es.onerror = () => {
        // The error fires both on a recoverable disconnect (browser
        // will reconnect) AND on terminal 4xx/5xx. We can't distinguish
        // reliably across browsers — so any error after a brief grace
        // period triggers polling. If SSE recovers, both sources run
        // and `apply` deduplicates by overwriting with the latest data.
        const sinceOpen = Date.now() - openedAt;
        if (es.readyState === EventSource.CLOSED || sinceOpen > 3000) {
          startPollingOnce();
        }
      };
      // Safety: if we unmount or the job closes, kill the timer too.
      const origClose = close;
      esRef.currentCleanup = () => {
        try { clearTimeout(noEventTimer); } catch { /* noop */ }
      };
    } catch {
      startPolling();
    }

    return () => {
      // Tear down both the timer and the existing close handler.
      try { esRef.currentCleanup?.(); } catch { /* noop */ }
      close();
    };
  }, [jobId, api, token]);

  return state;
}
