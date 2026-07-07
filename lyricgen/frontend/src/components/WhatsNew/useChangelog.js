import { useState, useCallback } from "react";
import { CHANGELOG } from "../../changelog";

// Estado de "no-leídos" del changelog, persistido en localStorage.
//   SEEN_KEY  → id de la entrada más nueva que el usuario ya vio en el PANEL.
//   MODAL_KEY → id de la entrada `featured` que ya se mostró como MODAL.
// Si localStorage falla, degradamos a "todo visto" (no molestar).
const SEEN_KEY = "genly_changelog_seen_id";
const MODAL_KEY = "genly_changelog_modal_seen_id";

const rd = (k) => {
  try { return localStorage.getItem(k); } catch { return "__ls_off__"; }
};
const wr = (k, v) => {
  try { localStorage.setItem(k, v); } catch { /* storage bloqueado */ }
};

export function useChangelog() {
  const entries = CHANGELOG; // ya viene más-nuevo-primero
  const latestId = entries.length ? entries[0].id : null;

  // No-leídos = entradas más nuevas que la última vista en el panel.
  const [seenId, setSeenId] = useState(() => rd(SEEN_KEY));
  let unreadCount;
  if (seenId === "__ls_off__") {
    unreadCount = 0; // localStorage off → no spamear el badge
  } else {
    const idx = seenId ? entries.findIndex((e) => e.id === seenId) : -1;
    unreadCount = idx === -1 ? entries.length : idx;
  }
  const markAllSeen = useCallback(() => {
    if (latestId) { wr(SEEN_KEY, latestId); setSeenId(latestId); }
  }, [latestId]);

  // Modal one-time de la entrada destacada.
  const featured = entries.find((e) => e.featured) || null;
  const [modalSeenId, setModalSeenId] = useState(() => rd(MODAL_KEY));
  const modalEntry =
    featured && modalSeenId !== "__ls_off__" && modalSeenId !== featured.id
      ? featured
      : null;
  const dismissModal = useCallback(() => {
    if (featured) { wr(MODAL_KEY, featured.id); setModalSeenId(featured.id); }
  }, [featured]);

  return { entries, unreadCount, markAllSeen, modalEntry, dismissModal };
}
