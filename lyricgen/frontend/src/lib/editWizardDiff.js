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
  // Title card customization (Full Rotor v1). Persisted durably to
  // render_params, same visual-text axis as the rest of step 4.
  if (!strEq(baseline.titleTemplate, current.titleTemplate)) {
    typoDiff.title_template = current.titleTemplate || "auto";
  }
  if (!numEq(baseline.titleSize, current.titleSize)) {
    typoDiff.title_size = parseFloat(current.titleSize) || 1.0;
  }
  if (!strEq(baseline.titleArtistFont, current.titleArtistFont)) {
    typoDiff.title_artist_font = current.titleArtistFont || "";
  }
  if (!strEq(baseline.titleSongFont, current.titleSongFont)) {
    typoDiff.title_song_font = current.titleSongFont || "";
  }
  // UI v1.1 (2026-05-30): manual song-title line break. "" = auto.
  // Buckets with typography because it changes only the title-card overlay
  // (no bg regen, no segments touched). Backend persists in render_params.
  if (!strEq(baseline.titleSongBreak, current.titleSongBreak)) {
    typoDiff.title_song_break = current.titleSongBreak || "";
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
  // "Regenerar fondo (nueva versión)": intención explícita del operador de
  // re-generar el fondo (nueva tirada) aunque no haya cambiado ningún campo.
  // Se chequea sobre `current` (no vs baseline: es una acción, no un campo).
  // Fuerza un bucket background vacío → edit_type=background re-renderiza con
  // el hint actual. No aplica si eligió un asset de biblioteca (eso supersede).
  if (current.forceBackgroundRegen && !current.editBackgroundId && !out.background) {
    out.background = {};
  }

  // ── background_library ───────────────────────────────────────────────
  // Swap a un asset curado de biblioteca (backend edit_type=
  // "background_library", PR #940 — sin Veo, sin consumir slot). El
  // baseline es siempre null ("mantener fondo actual"), así que un pick
  // de biblioteca SIEMPRE es diff. Mutuamente excluyente con el bucket
  // `background`: elegir un asset concreto supersede cualquier hint de
  // regeneración IA que haya quedado en el formulario.
  if (current.editBackgroundId) {
    out.background_library = { background_id: current.editBackgroundId };
    delete out.background;
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
//
// `opts.jobStatus` (optional): when provided, the function bundles the
// typography fields into another payload to reduce POST count AND to
// sidestep the backend's `pending_review` gate for typography/background
// standalone edits. The font/text_case/contrast/lyrics_animation fields
// are ungated server-side (they write to edit_params regardless of
// edit_type, main.py:7576-7608), so a metadata or lyrics edit can carry
// them piggyback in the same re-render. Saves 1 re-render slot + 1
// status hop in the UI.
export function buildEditPayloads(diff, opts = {}) {
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
  return bundleTypographyIntoFirstBucket(payloads, opts);
}

// Merge the typography payload's fields into whichever non-typography
// payload comes first in the array (metadata → lyrics → background),
// then drop the separate typography slot. Keeps the wire-shape minimal
// and avoids the backend's pending_review gate when the operator
// changes typography alongside metadata (typo + font fix in one
// re-render). Pure / no side effects on the input.
export function bundleTypographyIntoFirstBucket(payloads, _opts = {}) {
  const typoIdx = payloads.findIndex((p) => p.edit_type === "typography");
  if (typoIdx < 0) return payloads;
  const typo = payloads[typoIdx];
  // eslint-disable-next-line no-unused-vars
  const { edit_type, ...typoFields } = typo;
  // Priority order: prefer bundling into metadata (cheaper, no edit_count)
  // then lyrics (already needs segments anyway) then background (last
  // resort — background regens Veo, so a piggyback there means the
  // operator wanted a regen anyway).
  const targetOrder = ["metadata", "lyrics", "background"];
  for (const target of targetOrder) {
    const targetIdx = payloads.findIndex((p) => p.edit_type === target);
    if (targetIdx >= 0) {
      // Mutate the existing object in-place? No — return a fresh array
      // so callers don't have to clone. Spread typo fields onto a copy.
      const merged = { ...payloads[targetIdx], ...typoFields };
      const next = payloads.slice();
      next[targetIdx] = merged;
      next.splice(typoIdx, 1);
      return next;
    }
  }
  // No other bucket — typography stays standalone (backend will require
  // pending_review status). Caller can detect this case and convert to
  // a lyrics-with-current-segments edit if the job is done/rejected.
  return payloads;
}

// ── background regen modifiers (Veo/Imagen + content-validation) ────────
// Estos NO son campos del baseline sino ACCIÓN del wizard de edición: el
// operador elige motor y política de validación como modificadores de un
// regen de fondo IA, y llegan a `current` vía onEditFieldChange (igual que
// forceBackgroundRegen). Se aplican SOLO cuando el edit resuelto es un regen
// IA (edit_type="background"; el swap de biblioteca no dispara Veo/Imagen).
//
// Paridad con la tarjeta "Regenerar fondo" que se plegó al wizard
// (unificación #973): esa tarjeta SIEMPRE mandaba exactamente uno de
// bypass/force_content_validation — si no se manda ninguno, el backend
// fail-closea a force y se pierde fondo-libre (cuentas no-UMG).
//
//   - bgRegenEngine === "imagen"  → background_mode:"imagen" (foto animada
//     barata, sin riesgo de caras). "veo"/undefined = default, no se manda.
//   - bgRegenValidation === false → bypass_content_validation (fondo-libre,
//     sólo no-UMG). Cualquier otra cosa (incl. undefined) → force (validar).
export function backgroundRegenExtras(current) {
  const c = current || {};
  const out = {};
  if (c.bgRegenEngine === "imagen") out.background_mode = "imagen";
  if (c.bgRegenValidation === false) {
    out.bypass_content_validation = true;
  } else {
    out.force_content_validation = true;
  }
  return out;
}
