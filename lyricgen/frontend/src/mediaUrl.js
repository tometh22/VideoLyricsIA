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

import { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";

// Tokens are 5 min by default; refresh ~30 s early to avoid using a
// just-expired token for a video that's mid-stream.
const REFRESH_LEAD_MS = 30 * 1000;

const cache = new Map(); // key -> { token, expiresAt, inflight }

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function cacheKey(jobId, fileType) {
  return `${jobId}::${fileType}`;
}

async function fetchMediaToken(jobId, fileType) {
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
  const inflight = fetchMediaToken(jobId, fileType)
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

export async function getDownloadUrl(jobId, fileType) {
  const token = await getMediaToken(jobId, fileType);
  return `${API}/download/${jobId}/${fileType}?token=${encodeURIComponent(token)}`;
}

export async function getPreviewUrl(jobId, fileType) {
  const token = await getMediaToken(jobId, fileType);
  return `${API}/preview/${jobId}/${fileType}?token=${encodeURIComponent(token)}`;
}

// React hook: returns the preview URL for an <img>/<video> src. Returns
// "" until the token is fetched. The hook re-runs if jobId or fileType
// change.
export function useMediaUrl(jobId, fileType, kind = "preview") {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!jobId || !fileType) {
      setUrl("");
      return;
    }
    let cancelled = false;
    const fetcher = kind === "download" ? getDownloadUrl : getPreviewUrl;
    fetcher(jobId, fileType)
      .then((u) => { if (!cancelled) setUrl(u); })
      .catch(() => { if (!cancelled) setUrl(""); });
    return () => { cancelled = true; };
  }, [jobId, fileType, kind]);
  return url;
}

export function clearMediaCache() {
  cache.clear();
}
