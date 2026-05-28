// Compute the diff between a baseline (snapshot at edit-open time) and
// the current wizard state, bucketed by the backend's edit_type. Used by
// the post-render edit-wizard flow (App.jsx submitEdit) so a single
// "Re-renderizar" click translates into the minimum POST /edit calls
// needed to reflect the operator's changes.
//
// Buckets map 1:1 to the four backend edit_types:
//   - metadata    → { artist?, song_title? }
//   - typography  → { font?, font_scale?, text_case?, text_contrast?,
//                     lyrics_animation?, line_transition?, effect? }
//   - lyrics      → { segments: [...] }
//   - background  → { background_hint?, bg_verbatim?, background_mode?,
//                     movement_style? }
//
// A bucket is OMITTED entirely if none of its fields changed — the
// caller iterates over the present buckets and fires one POST each.
//
// The function is pure: no fetches, no React, no side effects. Lives in
// /lib so it can be unit-tested without spinning up a DOM.

import {
  normalizeSegmentsForEdit,
  segmentsUnchanged,
  layoutChanged,
} from "./lyricsEditSubmit.js";

// String equality with empty-vs-undefined treated as equal. The wizard
// stores "" for "field not set" while the baseline can have null/undefined
// for legacy jobs whose render_params predates a field.
function strEq(a, b) {
  return (a || "") === (b || "");
}

// Number equality with stringy tolerance. font_scale travels as a string
// in the wizard ("1.0") but the baseline reads it as a number from
// render_params. Compare via parseFloat with a small epsilon to avoid
// 1.0 !== 1 false positives.
function numEq(a, b) {
  const na = parseFloat(a);
  const nb = parseFloat(b);
  if (Number.isNaN(na) && Number.isNaN(nb)) return true;
  if (Number.isNaN(na) || Number.isNaN(nb)) return false;
  return Math.abs(na - nb) < 1e-6;
}

// Compute the four-bucket diff. Returns an object whose keys are the
// edit_types that have changes; bucket values are objects ready to spread
// into the POST body (with backend snake_case field names, not the
// camelCase the wizard uses internally).
export function computeFieldDiff(baseline, current) {
  if (!baseline || !current) return {};
  const out = {};

  // ── metadata ─────────────────────────────────────────────────────────
  const metaDiff = {};
  if (!strEq(baseline.artist, current.artist)) {
    metaDiff.artist = (current.artist || "").trim();
  }
  if (!strEq(baseline.songTitle, current.songTitle)) {
    metaDiff.song_title = (current.songTitle || "").trim();
  }
  if (Object.keys(metaDiff).length > 0) {
    out.metadata = metaDiff;
  }

  // ── typography ───────────────────────────────────────────────────────
  // Includes effect + lyrics_animation + line_transition because the
  // backend persists those durably to render_params on any edit_type and
  // they belong to the visual-text axis the operator picks in step 4.
  const typoDiff = {};
  if (!strEq(baseline.font, current.font)) typoDiff.font = current.font || "";
  if (!numEq(baseline.fontScale, current.fontScale)) {
    typoDiff.font_scale = parseFloat(current.fontScale);
  }
  if (!strEq(baseline.textCase, current.textCase)) {
    typoDiff.text_case = current.textCase || "upper";
  }
  if (!strEq(baseline.textContrast, current.textContrast)) {
    typoDiff.text_contrast = current.textContrast || "medium";
  }
  if (!strEq(baseline.lyricsAnimation, current.lyricsAnimation)) {
    typoDiff.lyrics_animation = current.lyricsAnimation || "none";
  }
  if (!strEq(baseline.lineTransition, current.lineTransition)) {
    typoDiff.line_transition = current.lineTransition || "none";
  }
  if (!strEq(baseline.effect, current.effect)) {
    typoDiff.effect = current.effect || "";
  }
  if (Object.keys(typoDiff).length > 0) {
    out.typography = typoDiff;
  }

  // ── lyrics ───────────────────────────────────────────────────────────
  // Reuses the lyrics-edit submit helpers so the unchanged check matches
  // the modal flow exactly (text + start/end equality + layout diff).
  if (Array.isArray(current.segments) && current.segments.length > 0) {
    const normalized = normalizeSegmentsForEdit(current.segments);
    const baselineSegs = Array.isArray(baseline.segments)
      ? normalizeSegmentsForEdit(baseline.segments)
      : [];
    const textChanged = !segmentsUnchanged(baselineSegs, normalized);
    const layoutDelta = layoutChanged(baselineSegs, normalized);
    if (textChanged || layoutDelta) {
      out.lyrics = { segments: normalized };
    }
  }

  // ── background ───────────────────────────────────────────────────────
  // Backend only honours these fields when edit_type=="background", so
  // they MUST stay in their own bucket — sending background_hint inside
  // a metadata POST would be a no-op.
  const bgDiff = {};
  if (!strEq(baseline.backgroundHint, current.backgroundHint)) {
    bgDiff.background_hint = (current.backgroundHint || "").trim();
  }
  if (!!baseline.bgVerbatim !== !!current.bgVerbatim) {
    bgDiff.bg_verbatim = !!current.bgVerbatim;
  }
  if (!strEq(baseline.backgroundMode, current.backgroundMode) && current.backgroundMode) {
    // Background mode is optional in the wire: only send when the
    // operator set a non-empty value, otherwise the backend's
    // None-means-keep default applies.
    bgDiff.background_mode = current.backgroundMode;
  }
  if (!strEq(baseline.movementStyle, current.movementStyle)) {
    bgDiff.movement_style = current.movementStyle || "";
  }
  if (Object.keys(bgDiff).length > 0) {
    out.background = bgDiff;
  }

  return out;
}

// Convenience: convert a diff into the array of POST bodies that
// submitEdit will fan out, with edit_type set on each. Returned in a
// stable order so submitEdit's sequential POSTs always run metadata →
// typography → lyrics → background. The backend reads CURRENT row state
// at the start of each edit, so this order means later edits see earlier
// edits' DB mutations — important when (e.g.) a metadata edit lands in
// the audit log before a typography edit consumes an edit_count slot.
export function buildEditPayloads(diff) {
  const payloads = [];
  if (diff.metadata) {
    payloads.push({ edit_type: "metadata", ...diff.metadata });
  }
  if (diff.typography) {
    payloads.push({ edit_type: "typography", ...diff.typography });
  }
  if (diff.lyrics) {
    payloads.push({ edit_type: "lyrics", ...diff.lyrics });
  }
  if (diff.background) {
    payloads.push({ edit_type: "background", ...diff.background });
  }
  return payloads;
}
