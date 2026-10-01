import { translateBackendError } from "./lyricsEditSubmit";

const API = import.meta.env.VITE_API_URL || "";

export function campaignApiBase() {
  return API;
}

export function authHeaders(headers = {}) {
  const token = localStorage.getItem("genly_token");
  return token ? { ...headers, Authorization: `Bearer ${token}` } : headers;
}

/**
 * Campaign state changes while the operator is in the editor, so reads are
 * never served from the HTTP cache. Errors keep the backend status and code
 * because several flows branch on them (429 capacity, 409 conflicts).
 */
export async function campaignRequest(path, options = {}) {
  const { json, ...rest } = options;
  const init = {
    cache: "no-store",
    ...rest,
    headers: authHeaders({
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(rest.headers || {}),
    }),
    ...(json !== undefined ? { body: JSON.stringify(json) } : {}),
  };
  const response = await fetch(`${API}${path}`, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body?.detail;
    const message = translateBackendError(detail)
      || (typeof detail === "string" ? detail : detail?.message || detail?.code)
      || `Error ${response.status}`;
    throw Object.assign(new Error(message), { status: response.status, code: detail?.code });
  }
  return body;
}

export const campaignPost = (path, value, options = {}) => campaignRequest(path, { ...options, method: "POST", json: value ?? {} });

/** Every page of the lyric review queue, in the backend's effort order. */
export async function loadReviewQueue(campaignId, { order = "effort", signal } = {}) {
  const base = `/batch/campaigns/${encodeURIComponent(campaignId)}/review-queue?stage=lyrics&order=${order}&scope=all&limit=1000`;
  const first = await campaignRequest(base, { signal });
  const pages = Math.max(1, Number(first.pages) || 1);
  if (pages === 1) return first;
  const rest = await Promise.all(Array.from({ length: pages - 1 }, (_, index) => (
    campaignRequest(`${base}&page=${index + 2}`, { signal })
  )));
  return { ...first, items: [first.items || [], ...rest.map((page) => page.items || [])].flat() };
}
