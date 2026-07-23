export class SseUnauthorizedError extends Error {
  constructor(status = 401) {
    super(`SSE authentication failed (${status})`);
    this.name = "SseUnauthorizedError";
    this.status = status;
  }
}

const wait = (ms, signal) => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, ms);
  const onAbort = () => {
    clearTimeout(timer);
    reject(new DOMException("Aborted", "AbortError"));
  };
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });
});

/**
 * Fetch an SSE stream with an Authorization header and incremental parsing.
 * Retries short-lived network failures with bounded exponential backoff. The
 * caller owns `signal` and can fall back to polling after this promise rejects.
 */
export async function fetchSse(url, {
  token,
  signal,
  onMessage,
  onEvent,
  watchdogMs = 8_000,
  maxRetries = 2,
  backoffMs = 500,
} = {}) {
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const controller = new AbortController();
    const relayAbort = () => controller.abort();
    signal?.addEventListener("abort", relayAbort, { once: true });
    let watchdog = null;
    const armWatchdog = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => controller.abort("sse_watchdog"), watchdogMs);
    };
    try {
      armWatchdog();
      const response = await fetch(url, {
        headers: {
          Accept: "text/event-stream",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
        cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) {
        throw new SseUnauthorizedError(response.status);
      }
      if (!response.ok || !response.body) {
        throw new Error(`SSE HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventName = "message";
      let dataLines = [];
      const dispatch = () => {
        if (!dataLines.length) {
          eventName = "message";
          return;
        }
        const raw = dataLines.join("\n");
        dataLines = [];
        const name = eventName || "message";
        eventName = "message";
        let data = raw;
        try { data = JSON.parse(raw); } catch { /* valid string payload */ }
        if (name === "message") onMessage?.(data);
        else onEvent?.(name, data);
      };

      while (!signal?.aborted) {
        const { value, done } = await reader.read();
        if (value?.length) armWatchdog();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (line === "") dispatch();
          else if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
        }
        if (done) {
          if (buffer) {
            if (buffer.startsWith("data:")) dataLines.push(buffer.slice(5).trimStart());
          }
          dispatch();
          return;
        }
      }
      throw new DOMException("Aborted", "AbortError");
    } catch (error) {
      if (error instanceof SseUnauthorizedError) throw error;
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      if (attempt >= maxRetries) throw error;
      await wait(Math.min(backoffMs * (2 ** attempt), 4_000), signal);
    } finally {
      clearTimeout(watchdog);
      signal?.removeEventListener("abort", relayAbort);
      controller.abort();
    }
  }
}
