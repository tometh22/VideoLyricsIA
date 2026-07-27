import { useEffect, useMemo, useState } from "react";

const cache = new Map(); // userId:assetId -> { token, expiresAt }

function cacheOwner() {
  try {
    const user = JSON.parse(localStorage.getItem("genly_user") || "null");
    return String(user?.id || user?.username || "anonymous");
  } catch { return "anonymous"; }
}

const cacheKey = (owner, assetId) => `${owner}:${assetId}`;

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

export function backgroundPreviewUrl(API, assetId, token) {
  if (!assetId || !token) return null;
  return `${API}/backgrounds/${assetId}/preview?token=${encodeURIComponent(token)}`;
}

async function mintBatch(API, ids, owner) {
  const res = await fetch(`${API}/backgrounds/preview-tokens`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ asset_ids: ids }),
  });
  if (!res.ok) throw new Error(`preview-tokens HTTP ${res.status}`);
  const data = await res.json();
  const expiresAt = Date.now() + Math.max(15, Number(data.expires_in || 300) - 15) * 1000;
  for (const [id, token] of Object.entries(data.tokens || {})) {
    if (token) cache.set(cacheKey(owner, String(id)), { token, expiresAt });
  }
}

export default function useBackgroundPreviewTokens(assetIds, API) {
  const idsKey = useMemo(
    () => [...new Set((assetIds || []).filter(Boolean).map(String))].sort().join(","),
    [assetIds],
  );
  const [version, setVersion] = useState(0);
  const owner = cacheOwner();

  useEffect(() => {
    const ids = idsKey ? idsKey.split(",") : [];
    if (!ids.length) return undefined;
    let cancelled = false;
    const now = Date.now();
    const missing = ids.filter((id) => {
      const item = cache.get(cacheKey(owner, id));
      return !item || item.expiresAt <= now;
    });
    if (!missing.length) {
      const nextExpiry = Math.min(...ids.map((id) => cache.get(cacheKey(owner, id)).expiresAt));
      const timer = setTimeout(() => setVersion((v) => v + 1), Math.max(1000, nextExpiry - now));
      return () => clearTimeout(timer);
    }
    (async () => {
      try {
        for (let i = 0; i < missing.length; i += 50) {
          await mintBatch(API, missing.slice(i, i + 50), owner);
        }
        if (!cancelled) setVersion((v) => v + 1);
      } catch (err) {
        // A missing thumbnail is safer than falling back to a login JWT URL.
        console.warn("[background-preview] token mint failed", err);
      }
    })();
    return () => { cancelled = true; };
  }, [API, idsKey, owner, version]);

  return useMemo(() => {
    const result = {};
    const now = Date.now();
    for (const id of idsKey ? idsKey.split(",") : []) {
      const item = cache.get(cacheKey(owner, id));
      if (item?.expiresAt > now) result[id] = item.token;
    }
    return result;
  }, [idsKey, owner, version]);
}
