import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

// Stale-while-revalidate cache shared by every campaign screen. Switching
// between pipeline stages, opening a song and coming back from the editor
// shows the last snapshot instantly and refreshes it in the background,
// instead of the 7–10 s "Cargando configuración…" of a full reload.
//
// Only the latest request per key may write: a slow response that started
// before a newer one can never overwrite fresher data.

const entries = new Map();
const EMPTY = Object.freeze({ data: undefined, error: null, at: 0, inflight: false });

function entryFor(key) {
  let entry = entries.get(key);
  if (!entry) {
    entry = { snapshot: EMPTY, listeners: new Set(), seq: 0, controller: null, fetcher: null };
    entries.set(key, entry);
  }
  return entry;
}

function publish(entry, patch) {
  entry.snapshot = { ...entry.snapshot, ...patch };
  entry.listeners.forEach((listener) => listener());
}

function revalidate(key) {
  const entry = entries.get(key);
  if (!entry?.fetcher) return Promise.resolve(undefined);
  entry.controller?.abort();
  const controller = new AbortController();
  const seq = ++entry.seq;
  entry.controller = controller;
  publish(entry, { inflight: true });
  return Promise.resolve()
    .then(() => entry.fetcher({ signal: controller.signal }))
    .then((data) => {
      if (entry.seq !== seq) return entry.snapshot.data;
      entry.controller = null;
      publish(entry, { data, error: null, at: Date.now(), inflight: false });
      return data;
    }, (error) => {
      if (entry.seq !== seq || error?.name === "AbortError") return entry.snapshot.data;
      entry.controller = null;
      publish(entry, { error, inflight: false, at: Date.now() });
      return entry.snapshot.data;
    });
}

/** Refresh every mounted resource whose key starts with `prefix`. */
export function invalidateCampaignResources(prefix) {
  const pending = [];
  for (const [key, entry] of entries) {
    if (!key.startsWith(prefix)) continue;
    if (entry.listeners.size) pending.push(revalidate(key));
    else entry.snapshot = { ...entry.snapshot, at: 0 };
  }
  return Promise.all(pending);
}

/** Optimistic local update; the next refresh replaces it with the truth. */
export function mutateCampaignResource(key, updater) {
  const entry = entries.get(key);
  if (!entry || entry.snapshot.data === undefined) return;
  publish(entry, { data: updater(entry.snapshot.data) });
}

export function clearCampaignResourceCache() {
  for (const entry of entries.values()) entry.controller?.abort();
  entries.clear();
}

export default function useCampaignResource(key, fetcher, { enabled = true, staleMs = 4000, pollMs = 30000 } = {}) {
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const activeKey = enabled && key ? key : null;

  const subscribe = useCallback((listener) => {
    if (!activeKey) return () => {};
    const entry = entryFor(activeKey);
    entry.listeners.add(listener);
    return () => {
      entry.listeners.delete(listener);
      if (!entry.listeners.size && entry.controller) {
        entry.controller.abort();
        entry.controller = null;
        entry.seq += 1;
        entry.snapshot = { ...entry.snapshot, inflight: false };
      }
    };
  }, [activeKey]);
  const getSnapshot = useCallback(() => (activeKey ? entryFor(activeKey).snapshot : EMPTY), [activeKey]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (!activeKey) return undefined;
    const entry = entryFor(activeKey);
    entry.fetcher = (options) => fetcherRef.current(options);
    if (!entry.snapshot.inflight && (entry.snapshot.data === undefined || Date.now() - entry.snapshot.at > staleMs)) {
      void revalidate(activeKey);
    }
    const refresh = () => {
      if (document.visibilityState === "visible" && !entry.snapshot.inflight) void revalidate(activeKey);
    };
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", refresh);
    const timer = pollMs ? window.setInterval(refresh, pollMs) : null;
    return () => {
      window.removeEventListener("pageshow", refresh);
      document.removeEventListener("visibilitychange", refresh);
      if (timer) window.clearInterval(timer);
    };
  }, [activeKey, staleMs, pollMs]);

  const reload = useCallback(() => (activeKey ? revalidate(activeKey) : Promise.resolve(undefined)), [activeKey]);
  return {
    data: snapshot.data,
    error: snapshot.error,
    loading: Boolean(activeKey) && snapshot.data === undefined && !snapshot.error,
    refreshing: snapshot.inflight && snapshot.data !== undefined,
    updatedAt: snapshot.at,
    reload,
  };
}
