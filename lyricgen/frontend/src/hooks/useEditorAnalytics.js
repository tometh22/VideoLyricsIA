import { useCallback, useEffect, useRef } from "react";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function useEditorAnalytics(jobId) {
  const queueRef = useRef([]);
  const timerRef = useRef(null);

  const flush = useCallback(async () => {
    if (!queueRef.current.length) return;
    const events = queueRef.current.splice(0, 50);
    try {
      await fetch(`${API}/analytics/events`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ events }),
        keepalive: true,
      });
    } catch {
      // Analytics must never interrupt editing. Drop the batch if offline.
    }
  }, []);

  const track = useCallback((name, properties = {}) => {
    queueRef.current.push({
      name,
      job_id: jobId || null,
      occurred_at: new Date().toISOString(),
      properties,
    });
    if (timerRef.current) return;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      flush();
    }, 1200);
  }, [flush, jobId]);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    flush();
  }, [flush]);

  return track;
}
