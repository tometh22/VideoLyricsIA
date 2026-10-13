// Media URL helper.
//
// Backend rejects the long-lived login JWT in /download and /preview query
// strings — those routes now require a separate, short-lived (~5 min)
// token scoped to a single (job_id, file_type). This module fetches that
// token and caches it until just before expiry so img/video/anchor tags
// can be rendered with a working URL.
//
// Why caching: a single JobDetail page can render thumbnail + preview +
// download anchor for the same (job, file_type) — a naive implementation
// would fire three /media-token requests. The cache keyed by job+filetype
// keeps it to one until the token is close to expiry.
//
// 2026-05-27 Phase-3 audit (UMG perf complaint): a /videos page with 200
// VideoCard instances was firing 200 parallel /media-token requests on
// mount, saturating the backend's 10-slot DB pool per process. Two
// mitigations added below:
//   1. Concurrency queue: max MAX_INFLIGHT (=6) requests at any time.
//      Excess requests wait in FIFO until a slot frees. The browser's
//      own connection limit (~6 per origin) means firing 200 didn't
//      actually parallelise anyway — it just buffered them at the
//      socket layer. Explicit queueing makes the limit visible and
//      releases slots in FIFO order so the first cards on screen win.
//   2. Lazy mode via `useLazyMediaUrl(ref, ...)`: only fetches the
//      token once the referenced element enters the viewport. Combined
//      with virtualization (HistoryView's react-window) the user that
//      never scrolls past card #20 never pays for the other 180.

import { useEffect, useRef, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";

// Tokens are 5 min by default; refresh ~30 s early to avoid using a
// just-expired token for a video that's mid-stream.
const REFRESH_LEAD_MS = 30 * 1000;

// Max concurrent /media-token requests. 6 mirrors the browser HTTP/1.1
// per-origin limit (most modern browsers default to 6); going higher
// only fills the socket buffer without actually parallelising.
const MAX_INFLIGHT = 6;

const cache = new Map(); // key -> { token, expiresAt, inflight }

// Concurrency queue. `inflightCount` is the number of /media-token
// requests currently executing; `waitQueue` holds {resolve, reject,
// fetcher} entries waiting for a slot. When a request finishes we
// dequeue the next one and increment/decrement `inflightCount`.
let inflightCount = 0;
const waitQueue = [];

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function cacheKey(jobId, fileType) {
  return `${jobId}::${fileType}`;
}

function startNextIfPossible() {
  while (inflightCount < MAX_INFLIGHT && waitQueue.length > 0) {
    const task = waitQueue.shift();
    inflightCount += 1;
    task.run().finally(() => {
      inflightCount -= 1;
      startNextIfPossible();
    });
  }
}

// Wrap a fetch in the concurrency limiter. Returns a Promise that
// resolves only after a slot was acquired and the fetch finished.
function withSlot(runFetch) {
  return new Promise((resolve, reject) => {
    const task = {
      run: () =>
        runFetch().then(
          (v) => { resolve(v); },
          (e) => { reject(e); }
        ),
    };
    waitQueue.push(task);
    startNextIfPossible();
  });
}

async function rawFetchMediaToken(jobId, fileType) {
  const res = await fetch(`${API}/media-token/${jobId}/${fileType}`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`media-token failed: ${res.status}`);
  }
  const { token } = await res.json();
  // Default expiry mirrors backend MEDIA_TOKEN_EXPIRE_SECONDS=300; we
  // refresh slightly early so a slow client doesn't hit the boundary.
  return { token, expiresAt: Date.now() + (5 * 60 * 1000) - REFRESH_LEAD_MS };
}

export function getMediaToken(jobId, fileType) {
  const key = cacheKey(jobId, fileType);
  const cached = cache.get(key);
  if (cached) {
    if (cached.token && Date.now() < cached.expiresAt) {
      return Promise.resolve(cached.token);
    }
    if (cached.inflight) return cached.inflight;
  }
  const inflight = withSlot(() => rawFetchMediaToken(jobId, fileType))
    .then(({ token, expiresAt }) => {
      cache.set(key, { token, expiresAt, inflight: null });
      return token;
    })
    .catch((err) => {
      cache.delete(key);
      throw err;
    });
  cache.set(key, { token: null, expiresAt: 0, inflight });
  return inflight;
}

// `version` (optional): opaque cache-buster appended as `&v=`. The backend
// ignores unknown query params — its only job is to change the URL STRING
// when the job's render changes. Without it, an edit that overwrites the
// same R2 key leaves the <video src> byte-identical, so a mounted player
// never re-fetches and the operator keeps seeing the PRE-edit render (the
// UMG "no me lo está actualizando" incident, job eaff5c7baf50: 4 edits OK
// server-side, stale player client-side). The media TOKEN cache stays
// keyed by (job, fileType) only — tokens are not content-bound, so reusing
// one across versions is correct and avoids extra /media-token requests.
export async function getDownloadUrl(jobId, fileType, version = "") {
  const token = await getMediaToken(jobId, fileType);
  const v = version ? `&v=${encodeURIComponent(version)}` : "";
  return `${API}/download/${jobId}/${fileType}?token=${encodeURIComponent(token)}${v}`;
}

export async function getPreviewUrl(jobId, fileType, version = "") {
  const token = await getMediaToken(jobId, fileType);
  const v = version ? `&v=${encodeURIComponent(version)}` : "";
  return `${API}/preview/${jobId}/${fileType}?token=${encodeURIComponent(token)}${v}`;
}

// React hook: returns the preview URL for an <img>/<video> src. Returns
// "" until the token is fetched. The hook re-runs if jobId, fileType or
// version change — `version` is what lets a mounted player pick up a NEW
// render of the SAME job after an edit, without a page refresh.
export function useMediaUrl(jobId, fileType, kind = "preview", version = "") {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!jobId || !fileType) {
      setUrl("");
      return;
    }
    let cancelled = false;
    const fetcher = kind === "download" ? getDownloadUrl : getPreviewUrl;
    fetcher(jobId, fileType, version)
      .then((u) => { if (!cancelled) setUrl(u); })
      .catch(() => { if (!cancelled) setUrl(""); });
    return () => { cancelled = true; };
  }, [jobId, fileType, kind, version]);
  return url;
}

// Lazy variant: only fetches the token once the referenced DOM node
// becomes visible (IntersectionObserver). The returned object has the
// `url` (string, "" until first intersection + fetch completes) and a
// `ref` callback to attach to the element you want to observe — works
// with both function-component refs and useRef return values.
//
// Usage:
//   const { url, ref } = useLazyMediaUrl(jobId, "thumbnail");
//   return <img ref={ref} src={url} alt="" />;
//
// rootMargin lets the observer trigger SLIGHTLY before the element
// enters the viewport (200 px above by default) so the token + image
// usually finish loading before the user actually sees the card. This
// hides the "blank thumbnail flash" you'd otherwise see during scroll.
export function useLazyMediaUrl(jobId, fileType, kind = "preview", { rootMargin = "200px", version = "" } = {}) {
  const [url, setUrl] = useState("");
  const [shouldFetch, setShouldFetch] = useState(false);
  const elementRef = useRef(null);

  // IntersectionObserver: flips `shouldFetch` to true once.
  useEffect(() => {
    if (shouldFetch) return; // already started, observer not needed
    const el = elementRef.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      // No IntersectionObserver (older browsers / SSR): just fetch.
      setShouldFetch(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            setShouldFetch(true);
            observer.disconnect();
            break;
          }
        }
      },
      { rootMargin, threshold: 0.01 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [shouldFetch, rootMargin]);

  // Actual fetch, gated on shouldFetch.
  useEffect(() => {
    if (!shouldFetch || !jobId || !fileType) {
      setUrl("");
      return;
    }
    let cancelled = false;
    const fetcher = kind === "download" ? getDownloadUrl : getPreviewUrl;
    fetcher(jobId, fileType, version)
      .then((u) => { if (!cancelled) setUrl(u); })
      .catch(() => { if (!cancelled) setUrl(""); });
    return () => { cancelled = true; };
  }, [shouldFetch, jobId, fileType, kind, version]);

  // ref is a callback so we can attach to any element type without
  // forwarding refs.
  const ref = (node) => { elementRef.current = node || null; };
  return { url, ref };
}

export function clearMediaCache() {
  cache.clear();
}
