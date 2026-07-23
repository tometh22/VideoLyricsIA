import { useState, useCallback, useEffect, useMemo } from "react";
import { CHANGELOG } from "../../changelog";

// Estado de "no-leídos" del changelog, persistido en localStorage.
//   SEEN_KEY  → id de la entrada más nueva que el usuario ya vio en el PANEL.
//   MODAL_KEY → id de la entrada `featured` que ya se mostró como MODAL.
// Si localStorage falla, degradamos a "todo visto" (no molestar).
const SEEN_KEY = "genly_changelog_seen_id";
const MODAL_KEY = "genly_changelog_modal_seen_id";
const LEGACY_OWNER_KEY = "genly_changelog_legacy_owner";

function currentUserId() {
  try {
    const user = JSON.parse(localStorage.getItem("genly_user") || "null");
    return String(user?.id || user?.username || "anonymous");
  } catch {
    return "anonymous";
  }
}

export function scopedChangelogKey(base, userId = currentUserId()) {
  return `${base}:${userId}`;
}

const rd = (k) => {
  try { return localStorage.getItem(k); } catch { return "__ls_off__"; }
};
const wr = (k, v) => {
  try { localStorage.setItem(k, v); } catch { /* storage bloqueado */ }
};

// Claim an old browser-wide receipt once for the first authenticated user.
// Subsequent accounts on the same browser start clean instead of inheriting
// somebody else's dismissed announcements.
export function readScopedReceipt(base, userId = currentUserId()) {
  const key = scopedChangelogKey(base, userId);
  const scoped = rd(key);
  if (scoped && scoped !== "__ls_off__") return scoped;
  if (scoped === "__ls_off__") return scoped;
  const legacy = rd(base);
  // Cada recibo legacy se reclama de forma independiente. Un owner global
  // hacía que migrar el panel impidiera migrar el modal del mismo usuario.
  const ownerKey = `${LEGACY_OWNER_KEY}:${base}`;
  const owner = rd(ownerKey);
  if (legacy && !owner && userId !== "anonymous") {
    wr(ownerKey, userId);
    wr(key, legacy);
    return legacy;
  }
  return null;
}

export function useChangelog() {
  const entries = CHANGELOG; // ya viene más-nuevo-primero
  const latestId = entries.length ? entries[0].id : null;

  // No-leídos = entradas más nuevas que la última vista en el panel.
  const userId = useMemo(() => currentUserId(), []);
  const seenKey = scopedChangelogKey(SEEN_KEY, userId);
  const modalKey = scopedChangelogKey(MODAL_KEY, userId);
  const [seenId, setSeenId] = useState(() => readScopedReceipt(SEEN_KEY, userId));
  let unreadCount;
  if (seenId === "__ls_off__") {
    unreadCount = 0; // localStorage off → no spamear el badge
  } else {
    const idx = seenId ? entries.findIndex((e) => e.id === seenId) : -1;
    unreadCount = idx === -1 ? entries.length : idx;
  }
  const markAllSeen = useCallback(() => {
    if (latestId) { wr(seenKey, latestId); setSeenId(latestId); }
  }, [latestId, seenKey]);

  // Modal one-time de la entrada destacada.
  const featured = entries.find((e) => e.featured) || null;
  const [modalSeenId, setModalSeenId] = useState(() => readScopedReceipt(MODAL_KEY, userId));
  const modalEntry =
    featured && modalSeenId !== "__ls_off__" && modalSeenId !== featured.id
      ? featured
      : null;
  const dismissModal = useCallback(() => {
    if (featured) { wr(modalKey, featured.id); setModalSeenId(featured.id); }
  }, [featured, modalKey]);

  useEffect(() => {
    const sync = (event) => {
      if (event.key === seenKey) setSeenId(event.newValue);
      if (event.key === modalKey) setModalSeenId(event.newValue);
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, [seenKey, modalKey]);

  return { entries, unreadCount, markAllSeen, modalEntry, dismissModal };
}
