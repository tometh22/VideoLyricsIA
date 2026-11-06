// How the portal treats a publication, as reported by the API
// (`publication_mode` on /admin/change-requests):
//   "snapshot" - publishing freezes a copy; the portal keeps serving the old
//                cut until the operator publishes the new one (default).
//   "pointer"  - publishing copies nothing; the portal serves the newest render,
//                so the client sees the new cut as soon as it finishes.
// UI copy must not promise "the portal still shows the old cut" in pointer mode.
let mode = "snapshot";

export function setPublicationMode(value) {
  mode = value === "pointer" ? "pointer" : "snapshot";
}

export function getPublicationMode() {
  return mode;
}

/** Pick the wording that is true for the active mode. */
export function byPublicationMode({ snapshot, pointer }) {
  return mode === "pointer" ? pointer : snapshot;
}

/**
 * Same as byPublicationMode, but a delivery that still has a FROZEN copy keeps
 * serving it whatever the global mode says (it was published in copy mode).
 */
export function byRowMode(publication, texts) {
  if (publication?.snapshot_pinned === true) return texts.snapshot;
  return byPublicationMode(texts);
}
