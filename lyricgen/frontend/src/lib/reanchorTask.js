import { reanchorHttpFailure } from "./reanchorResult";

// Keep the same task identity through the entire wait. Previously the first
// 12 minutes queried the task, then 8 minutes watched an unrelated document
// revision. A decline at minute 14 was therefore invisible to the editor.
export const REANCHOR_TASK_TIMEOUT_MS = 20 * 60 * 1000;

async function readTask(request, timeoutMs) {
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([
      (async () => {
        const response = await request({ signal: controller.signal, cache: "no-store" });
        return {
          status: response.status,
          record: response.ok ? await response.json() : null,
        };
      })(),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("reanchor status timeout"));
        }, timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function pollReanchorTask({
  taskId, request, onProgress,
  timeoutMs = REANCHOR_TASK_TIMEOUT_MS,
  pollMs = 3000,
  readTimeoutMs = 15000,
}) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(pollMs, deadline - Date.now())));
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    try {
      onProgress?.({ elapsedS: Math.round((Date.now() - startedAt) / 1000), taskId });
    } catch { /* progress must not interrupt status reads */ }
    let status, record;
    try {
      ({ status, record } = await readTask(request, Math.min(readTimeoutMs, remaining)));
    } catch {
      continue; // A slow response body or lost connection is bounded too.
    }
    if (status === 404) return { ok: false, reason: "task-lost", taskId };
    if (record?.status !== "done") continue;
    const httpStatus = Number(record.http_status);
    const payload = record.payload || {};
    if (httpStatus >= 200 && httpStatus < 300) return { ...payload, terminal: true };
    return reanchorHttpFailure(httpStatus, payload, { terminal: true });
  }
  // Expiring a client wait does not cancel a server task or prove failure.
  // Keep its identity so callers cannot mistake an autosave for its result.
  return { ok: false, reason: "task-timeout", taskId };
}
