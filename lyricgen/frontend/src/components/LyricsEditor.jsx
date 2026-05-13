import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { useI18n } from "../i18n";
import { EditorTour } from "./OnboardingTour";

// Mismo flag que UploadZone/EditRequestPanel — oculta el label de motion
// en el strip de metadata mientras la feature de animación está pausada.
const SHOW_MOTION_PICKER = false;

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatTimestamp(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.floor((seconds % 1) * 10);
  return `${m}:${s.toString().padStart(2, "0")}.${ms}`;
}

// Parse "M:SS.t", "M:SS", or a raw seconds value into a non-negative
// float. Returns null when the string can't be interpreted, so the
// caller can decide to ignore the edit instead of writing garbage.
function parseTimestamp(str) {
  if (str == null) return null;
  const trimmed = String(str).trim().replace(",", ".");
  if (!trimmed) return null;
  if (trimmed.includes(":")) {
    const parts = trimmed.split(":");
    if (parts.length !== 2) return null;
    const m = parseInt(parts[0], 10);
    const s = parseFloat(parts[1]);
    if (Number.isNaN(m) || Number.isNaN(s)) return null;
    if (m < 0 || s < 0 || s >= 60) return null;
    return m * 60 + s;
  }
  const v = parseFloat(trimmed);
  if (Number.isNaN(v) || v < 0) return null;
  return v;
}

function findSuggestion(whisperText, refLines, startIdx) {
  if (!refLines.length) return null;
  const wLower = whisperText.toLowerCase().trim();
  let bestScore = 0;
  let bestLine = null;

  const searchStart = Math.max(0, startIdx - 3);
  const searchEnd = Math.min(refLines.length, startIdx + 10);

  for (let i = searchStart; i < searchEnd; i++) {
    const rLower = refLines[i].toLowerCase().trim();
    if (!rLower) continue;
    const wWords = wLower.split(/\s+/);
    const rWords = rLower.split(/\s+/);
    let matches = 0;
    for (const w of wWords) { if (rWords.includes(w)) matches++; }
    const score = matches / Math.max(wWords.length, rWords.length);
    if (score > bestScore) { bestScore = score; bestLine = refLines[i]; }

    if (i < refLines.length - 1) {
      const combined = rLower + " " + refLines[i + 1].toLowerCase().trim();
      const cWords = combined.split(/\s+/);
      let cMatches = 0;
      for (const w of wWords) { if (cWords.includes(w)) cMatches++; }
      const cScore = cMatches / Math.max(wWords.length, cWords.length);
      if (cScore > bestScore) { bestScore = cScore; bestLine = refLines[i] + " " + refLines[i + 1]; }
    }
  }

  if (bestScore > 0.3 && bestLine) {
    const normalize = (s) => s.toLowerCase().replace(/[^a-záéíóúüñ\s]/g, "").replace(/\s+/g, " ").trim();
    if (normalize(bestLine) !== normalize(whisperText)) {
      return bestLine;
    }
  }
  return null;
}

// Find two consecutive lines in `refLines` whose concatenation matches
// `segText`. Used by the auto-split banner: when a Whisper segment
// captures 2 lyric lines mergeadas en uno solo, lrclib plain (passed as
// referenceLyrics) has them como 2 entries. Si el match es lo
// suficientemente fuerte (>0.5), devolvemos el [lineA, lineB] que el
// caller usa para crear 2 segments separados.
//
// Threshold 0.5 — más permisivo que findSuggestion (0.3) porque acá
// estamos comparando contra la CONCATENACIÓN de 2 líneas vs 1 segment,
// el set de words es más grande y el match esperado es más alto.
function findReferenceSplitLines(segText, refLines) {
  if (!refLines || refLines.length < 2) return null;
  const normalize = (s) =>
    s.toLowerCase().replace(/[^a-záéíóúüñ\s]/g, "").replace(/\s+/g, " ").trim();
  const segNorm = normalize(segText);
  if (!segNorm) return null;
  const segWords = segNorm.split(/\s+/);
  if (segWords.length < 4) return null; // demasiado corto para split fiable

  let bestScore = 0;
  let bestPair = null;
  for (let i = 0; i < refLines.length - 1; i++) {
    const a = refLines[i];
    const b = refLines[i + 1];
    if (!a || !b) continue;
    const cNorm = normalize(a + " " + b);
    if (!cNorm) continue;
    const cWords = cNorm.split(/\s+/);
    let matches = 0;
    for (const w of segWords) {
      if (cWords.includes(w)) matches++;
    }
    const score = matches / Math.max(segWords.length, cWords.length);
    if (score > bestScore) {
      bestScore = score;
      bestPair = [a, b];
    }
  }
  if (bestScore > 0.5) return bestPair;
  return null;
}

// ─── Font-code → CSS map (mirrors UploadZone FONTS) ────────────────────────
const FONT_CSS_MAP = {
  "jost-bold":       "'Jost', sans-serif",
  "montserrat-bold": "'Montserrat', sans-serif",
  "poppins-bold":    "'Poppins', sans-serif",
  "outfit-bold":     "'Outfit', sans-serif",
  "roboto-bold":     "'Roboto', sans-serif",
  "bebas-neue":      "'Bebas Neue', sans-serif",
  "oswald-bold":     "'Oswald', sans-serif",
  "anton":           "'Anton', sans-serif",
  "":                "'Montserrat', sans-serif",
};

// Backend tier params (baseline 1920×1080, scale = 1.0).
const TIERS = [
  { maxChars: 50, sizePx: 85, maxWidthPx: 1500 },
  { maxChars: 80, sizePx: 70, maxWidthPx: 1650 },
  { maxChars: Infinity, sizePx: 55, maxWidthPx: 1700 },
];

function getTier(text) {
  const len = text.length;
  return TIERS.find((t) => len <= t.maxChars) || TIERS[TIERS.length - 1];
}

// Simulate moviepy's word-wrap with canvas.measureText.
// Returns the number of visual lines the segment will occupy in the video.
function estimateWrappedLines(text, fontCss, sizePx, maxWidthPx) {
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    ctx.font = `bold ${sizePx}px ${fontCss}`;
    const spaceW = ctx.measureText(" ").width;
    const words = text.split(" ");
    let lines = 1;
    let lineW = 0;
    for (const word of words) {
      const ww = ctx.measureText(word).width;
      if (lineW > 0 && lineW + spaceW + ww > maxWidthPx) {
        lines++;
        lineW = ww;
      } else {
        lineW = lineW > 0 ? lineW + spaceW + ww : ww;
      }
    }
    return lines;
  } catch {
    return 1;
  }
}

// Apply the same case transform as the backend _apply_case().
function applyCase(text, textCase) {
  if (textCase === "upper") return text.toUpperCase();
  if (textCase === "title") return text.replace(/\b\w/g, (c) => c.toUpperCase());
  if (textCase === "lower") return text.toLowerCase();
  return text;
}

export default function LyricsEditor({
  segments, filename, audioFile, referenceLyrics,
  coverageWarning = false, recoverySource = "",
  onApprove, onBack, isBatch = false, batchProgress = "",
  user = null,
  font = "",
  textCase = "upper",
  fontScale = 1.0,
  lyricTransition = "cut",
  textMotion = "none",
  textContrast = "medium",
}) {
  const { t } = useI18n();
  const [edited, setEdited] = useState(() =>
    segments.map((s, i) => ({ ...s, _id: i }))
  );
  const [isDirty, setIsDirty] = useState(false);

  // Warn browser on tab-close / external navigation when there are unsaved edits.
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  // ─── Audio sync ─────────────────────────────────────────────────────
  // Blob URL lifecycle must live in useEffect, not useMemo. useMemo is
  // not a lifecycle hook and React 18 StrictMode double-invokes its
  // callback in dev, leaking one URL per mount. More importantly, pairing
  // a useMemo-created URL with a useEffect cleanup keyed on [audioUrl]
  // causes StrictMode's simulated unmount to revoke the URL while the
  // <audio> element in the DOM still references it — playback dies a few
  // seconds in once the initial buffered range is consumed.
  const [audioUrl, setAudioUrl] = useState(null);
  useEffect(() => {
    if (!audioFile) { setAudioUrl(null); return undefined; }
    const url = URL.createObjectURL(audioFile);
    setAudioUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [audioFile]);

  const audioRef = useRef(null);
  const listRef = useRef(null);
  const rowRefs = useRef({});

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [wrapWarning, setWrapWarning] = useState(null); // {ids: [...]} for 3+ line segs
  const [focusedSegId, setFocusedSegId] = useState(null); // for preview panel

  // Inline timestamp edit state. Only one row can be in edit mode at a
  // time; clicking a different row's timestamp swaps the active editor.
  // Single-click on a timestamp seeks; double-click switches to edit.
  const [editingId, setEditingId] = useState(null);
  const [editValue, setEditValue] = useState("");

  // Tap-to-sync mode — operator hits Space (or button) while audio
  // plays to anchor each line at the current playback time. Solves
  // the generic case where timestamps are stretched, compressed, or
  // offset arbitrarily — listening + tapping is ground truth.
  const [syncMode, setSyncMode] = useState(false);
  const [syncCursor, setSyncCursor] = useState(0);
  // Global timing offset panel — UX entry point for "the whole song is
  // shifted by N ms" cases. Different from Sync Mode (which anchors a
  // line + propagates) and the "intro is too long" banner (which only
  // appears when first.start > 3 s). This panel is always available
  // and lets the operator nudge every line by ±1 s with a slider or
  // ±125/250/500 ms presets. Collapsed by default to keep the editor
  // tidy.
  const [shiftPanelOpen, setShiftPanelOpen] = useState(false);
  const [shiftDraftMs, setShiftDraftMs] = useState(0); // -1000..+1000
  // After applying a shift the slider resets to 0 so the next draft
  // starts clean. Without a confirmation chip the operator can't tell
  // whether the click landed — they see the preset highlight clear and
  // assume nothing happened, then re-apply, doubling the shift.
  // appliedShiftMs holds the last applied delta for ~2.5s purely as
  // visual receipt.
  const [appliedShiftMs, setAppliedShiftMs] = useState(null);
  useEffect(() => {
    if (appliedShiftMs == null) return undefined;
    const id = setTimeout(() => setAppliedShiftMs(null), 2500);
    return () => clearTimeout(id);
  }, [appliedShiftMs]);
  // When false (default), each Sync-Mode tap anchors ONLY the current
  // line — leaves every following timestamp alone. When true, the same
  // delta propagates to every line after the cursor (the previous-only
  // behaviour, useful when the whole timeline is uniformly off).
  // Operators reported that the cascading default was destroying their
  // already-correct lines when they only wanted to fix a single anchor;
  // the safer default is single-line.
  const [syncCascade, setSyncCascade] = useState(false);
  // Stack of {id, prevStart, prevEnd} so "Deshacer" can revert the
  // last tap if the operator overshot.
  const [syncHistory, setSyncHistory] = useState([]);

  // Manual-edit history. Each entry is the FULL `edited` snapshot taken
  // BEFORE a mutation lands (single-line timestamp tweak, suggestion
  // application, intro trim, etc.). Capped at 50 entries — that's enough
  // to walk back through a normal review session without bloating React
  // state. Cmd/Ctrl+Z pops one and replays it onto setEdited.
  const [editHistory, setEditHistory] = useState([]);
  const pushEditHistory = useCallback(() => {
    setIsDirty(true);
    setEditHistory((prev) => {
      const next = [...prev, edited];
      return next.length > 50 ? next.slice(next.length - 50) : next;
    });
  }, [edited]);
  const undoEdit = useCallback(() => {
    setEditHistory((prev) => {
      if (!prev.length) return prev;
      const snapshot = prev[prev.length - 1];
      setEdited(snapshot);
      return prev.slice(0, -1);
    });
  }, []);

  const startEditTimestamp = (seg) => {
    setEditingId(seg._id);
    setEditValue(formatTimestamp(seg.start));
  };
  const cancelEditTimestamp = () => {
    setEditingId(null);
    setEditValue("");
  };

  const commitEditTimestamp = (seg) => {
    const parsed = parseTimestamp(editValue);
    if (parsed == null) {
      // Bad input — silently revert.
      cancelEditTimestamp();
      return;
    }

    // Clamp to the window between the previous segment's end and the
    // next segment's start (in the original ordering by _id). Without
    // this, the operator can set a start past the next row's start,
    // producing overlapping segments that the renderer interprets as
    // simultaneous on-screen lines. We use the original index to find
    // neighbors so a previous edit that shifted siblings doesn't cause
    // the wrong rows to be picked up.
    const idx = edited.findIndex((s) => s._id === seg._id);
    const prevSeg = idx > 0 ? edited[idx - 1] : null;
    const nextSeg = idx >= 0 && idx < edited.length - 1 ? edited[idx + 1] : null;
    const minAllowed = prevSeg ? prevSeg.end : 0;
    const maxAllowed = nextSeg ? Math.max(minAllowed, nextSeg.start - 0.1) : (duration || parsed);
    const newStart = Math.max(minAllowed, Math.min(parsed, maxAllowed));

    // No-op edits (clamped value identical to current) shouldn't pollute
    // the undo stack — the user's Ctrl+Z would feel broken otherwise.
    if (Math.abs(newStart - seg.start) >= 1e-3) {
      pushEditHistory();
    }
    setEdited((prev) => prev.map((s) => {
      if (s._id !== seg._id) return s;
      // Preserve segment duration when the operator nudges the start
      // unless that would push end past audio_duration or the next row.
      const segDur = Math.max(0.5, s.end - s.start);
      let newEnd = newStart + segDur;
      const upperBound = nextSeg ? Math.min(nextSeg.start, duration || nextSeg.start) : duration;
      if (upperBound && newEnd > upperBound) newEnd = upperBound;
      return { ...s, start: newStart, end: newEnd };
    }));
    setEditingId(null);
    setEditValue("");
    // Manual edits never propagate to neighbouring lines. The earlier
    // behaviour offered a "shift the rest by the same delta" banner,
    // which the operator could miss (it sat above a long, scrolling
    // list) and accidentally accept — they reported a single-line
    // tweak that silently moved every following timestamp.
    // Use Sync Mode (Space + tap) when you actually want to anchor
    // a line and re-flow the rest.
  };

  // Active segment: the one whose [start, end] contains currentTime, or
  // the latest one whose start <= currentTime if no segment "owns" the
  // moment (e.g. instrumental gap).
  const activeId = useMemo(() => {
    let containing = null;
    let lastStarted = null;
    for (const seg of edited) {
      if (currentTime >= seg.start && currentTime < seg.end) containing = seg;
      if (currentTime >= seg.start) lastStarted = seg;
    }
    return (containing || lastStarted)?._id ?? null;
  }, [edited, currentTime]);

  // Tap handler: anchor the line at syncCursor to currentTime, then
  // propagate the same delta to every line AFTER it (the unanchored
  // ones). If the offset was constant the next line is already roughly
  // right and the operator only needs to confirm. Already-anchored
  // lines (idx < syncCursor) are ground truth and stay put.
  const tapAnchor = useCallback(() => {
    if (!syncMode) return;
    if (syncCursor < 0 || syncCursor >= edited.length) return;
    const target = edited[syncCursor];
    if (!target) return;
    const newStart = Math.max(0, currentTime);
    const delta = newStart - target.start;
    // Snapshot the future lines BEFORE mutating so undo can restore
    // every shifted timestamp, not just the anchor's.
    // Snapshot the future ONLY when we're going to touch it. Without
    // syncCascade the future array stays empty and Deshacer reverts a
    // single line — matching the user's mental model.
    const futureSnapshot = syncCascade
      ? edited
          .slice(syncCursor + 1)
          .map((s) => ({ id: s._id, prevStart: s.start, prevEnd: s.end }))
      : [];
    setSyncHistory((prev) => [
      ...prev,
      {
        id: target._id,
        prevStart: target.start,
        prevEnd: target.end,
        cursor: syncCursor,
        future: futureSnapshot,
        delta,
      },
    ]);
    setEdited((prev) =>
      prev.map((s, i) => {
        if (s._id === target._id) {
          const segDur = Math.max(0.5, s.end - s.start);
          let newEnd = newStart + segDur;
          if (duration && newEnd > duration) newEnd = duration;
          return { ...s, start: newStart, end: newEnd };
        }
        // Cascade only when the operator opted in. We used to have a
        // 200 ms dead-zone here, intended to avoid jittering on
        // micro-adjustments. In practice that swallowed the most
        // common real correction (Whisper drifts of 100-300 ms), so
        // operators reported "Arrastrar siguientes anda mal — apreté
        // y no pasó nada" — when actually the cascade math was running
        // but rejecting the delta as "too small to matter". 10 ms is
        // tight enough to filter pure floating-point noise without
        // discarding legitimate user-driven shifts. The user remains
        // in control via the explicit `syncCascade` opt-in.
        if (syncCascade && i > syncCursor && Math.abs(delta) >= 0.01) {
          const segDur = Math.max(0.5, s.end - s.start);
          const shifted = Math.max(0, s.start + delta);
          let newEnd = shifted + segDur;
          if (duration && newEnd > duration) newEnd = duration;
          return { ...s, start: shifted, end: newEnd };
        }
        return s;
      }),
    );
    // Advance to the next line; auto-exit when past the last one.
    if (syncCursor + 1 >= edited.length) {
      setSyncMode(false);
    } else {
      setSyncCursor(syncCursor + 1);
    }
  }, [syncMode, syncCursor, edited, currentTime, duration, syncCascade]);

  const undoLastAnchor = useCallback(() => {
    setSyncHistory((prev) => {
      if (prev.length === 0) return prev;
      const last = prev[prev.length - 1];
      const futureMap = new Map((last.future || []).map((f) => [f.id, f]));
      setEdited((segs) =>
        segs.map((s) => {
          if (s._id === last.id) return { ...s, start: last.prevStart, end: last.prevEnd };
          const f = futureMap.get(s._id);
          if (f) return { ...s, start: f.prevStart, end: f.prevEnd };
          return s;
        }),
      );
      setSyncCursor(last.cursor);
      return prev.slice(0, -1);
    });
  }, []);

  const enterSyncModeAt = (idx) => {
    if (edited.length === 0) return;
    const safeIdx = Math.max(0, Math.min(idx, edited.length - 1));
    setSyncCursor(safeIdx);
    setSyncHistory([]);
    setSyncMode(true);
    // Lead-in: scrub to ~1.5s before the chosen line so the operator
    // hears the run-up. Don't autoplay — let them press play when ready.
    const target = edited[safeIdx];
    if (target) seekTo(Math.max(0, target.start - 1.5), false);
  };

  const enterSyncMode = () => enterSyncModeAt(0);

  const exitSyncMode = () => {
    setSyncMode(false);
  };

  // Auto-scroll the active row into view while playing. In sync mode,
  // scroll to the armed row instead so the operator always sees what
  // they're about to anchor.
  const lastScrolledIdRef = useRef(null);
  useEffect(() => {
    if (syncMode) {
      const armed = edited[syncCursor];
      if (!armed) return;
      if (lastScrolledIdRef.current === armed._id) return;
      lastScrolledIdRef.current = armed._id;
      const el = rowRefs.current[armed._id];
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    if (!isPlaying || activeId === null) return;
    if (lastScrolledIdRef.current === activeId) return;
    lastScrolledIdRef.current = activeId;
    const el = rowRefs.current[activeId];
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeId, isPlaying, syncMode, syncCursor, edited]);

  const togglePlay = useCallback(() => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) a.play().catch(() => {});
    else a.pause();
  }, []);

  const seekTo = useCallback((seconds, autoplay = true) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = Math.max(0, seconds);
    if (autoplay && a.paused) a.play().catch(() => {});
  }, []);

  // Spacebar: in sync mode, anchors the current line; otherwise toggles
  // play/pause. Cmd/Ctrl+Z (or just Z) reverts the last anchor while
  // in sync mode so the operator can recover from a mistap.
  useEffect(() => {
    const onKey = (e) => {
      const tag = (document.activeElement?.tagName || "").toUpperCase();
      const editing = tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable;
      if (editing) return;
      if (e.code === "Space") {
        e.preventDefault();
        if (syncMode) tapAnchor();
        else togglePlay();
      } else if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        // Cmd/Ctrl+Z: undo. Sync Mode rolls back the last anchor (with
        // its propagated future); outside Sync Mode it pops the manual
        // edit history (single-line edits, suggestions, intro trim).
        e.preventDefault();
        if (syncMode) undoLastAnchor();
        else undoEdit();
      } else if (syncMode && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        undoLastAnchor();
      } else if (syncMode && e.key === "Escape") {
        e.preventDefault();
        exitSyncMode();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay, syncMode, tapAnchor, undoLastAnchor, undoEdit]);

  // ─── Reference lyrics suggestions (unchanged) ───────────────────────
  const refLines = useMemo(() => {
    if (!referenceLyrics) return [];
    return referenceLyrics.split("\n").filter((l) => l.trim());
  }, [referenceLyrics]);

  const suggestionsById = useMemo(() => {
    const map = {};
    let refIdx = 0;
    segments.forEach((seg, i) => {
      const suggestion = findSuggestion(seg.text, refLines, refIdx);
      map[i] = suggestion;
      if (suggestion) {
        const idx = refLines.findIndex(
          (l, j) => j >= refIdx && l.toLowerCase().includes(seg.text.toLowerCase().split(" ")[0]?.toLowerCase())
        );
        if (idx >= 0) refIdx = idx + 1;
      }
    });
    return map;
  }, [segments, refLines]);

  // Detección de segments mergeados (2 lyric lines en 1 segment) usando
  // lrclib plain como oracle. Caso real motivador: Whisper agrupa
  // 2 versos consecutivos en un solo segment ("Siento el calor de toda
  // tu piel en mi cuerpo otra vez") cuando lrclib los tiene como
  // entries separadas. El banner banner-prominent al tope del editor
  // ofrece auto-dividir TODO el lote con 1 click.
  const mergeableSegments = useMemo(() => {
    if (refLines.length < 2) return [];
    const out = [];
    edited.forEach((seg) => {
      if (!seg.text || !seg.text.trim()) return;
      const pair = findReferenceSplitLines(seg.text, refLines);
      if (pair) out.push({ _id: seg._id, splitLines: pair });
    });
    return out;
  }, [edited, refLines]);

  // Auto-dividir TODOS los segments mergeados usando reference como
  // oracle. Para cada uno: timestamp split proporcional al char-count
  // de cada línea (lineA más larga = más tiempo). Reverse-order para
  // no romper índices durante la mutación.
  const autoSplitAllFromReference = () => {
    if (mergeableSegments.length === 0) return;
    pushEditHistory();
    setEdited((prev) => {
      // Map id → splitLines para lookup rápido
      const byId = new Map(
        mergeableSegments.map((m) => [m._id, m.splitLines]),
      );
      const result = [];
      let nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      for (const seg of prev) {
        const splitLines = byId.get(seg._id);
        if (!splitLines) {
          result.push(seg);
          continue;
        }
        const [lineA, lineB] = splitLines;
        const totalChars = lineA.length + lineB.length;
        if (totalChars === 0) {
          result.push(seg);
          continue;
        }
        const ratio = lineA.length / totalChars;
        const dur = Math.max(0.6, seg.end - seg.start);
        const midTime = seg.start + dur * ratio;
        const gap = 0.05;
        result.push({
          ...seg,
          _id: nextId++,
          text: lineA,
          end: Math.max(seg.start + 0.3, midTime - gap),
        });
        result.push({
          ...seg,
          _id: nextId++,
          text: lineB,
          start: Math.min(seg.end - 0.3, midTime),
          end: seg.end,
        });
      }
      return result;
    });
  };

  const updateText = (id, text) => {
    pushEditHistory();
    setEdited((prev) => prev.map((seg) => (seg._id === id ? { ...seg, text } : seg)));
  };

  const applySuggestion = (id) => {
    const suggestion = suggestionsById[id];
    if (suggestion) updateText(id, suggestion);
  };

  const applyAllSuggestions = () => {
    pushEditHistory();
    setEdited((prev) =>
      prev.map((seg) => {
        const suggestion = suggestionsById[seg._id];
        return suggestion ? { ...seg, text: suggestion } : seg;
      })
    );
  };

  // Shift the entire timeline by `delta` seconds, clamping start/end to
  // [0, duration]. Used by the "Recortar intro" banner so the operator
  // can collapse a long instrumental intro down to a configurable
  // pre-roll without manually nudging every line.
  const shiftAllSegments = useCallback((delta) => {
    if (Math.abs(delta) < 0.05) return;
    pushEditHistory();
    setEdited((prev) =>
      prev.map((s) => {
        const segDur = Math.max(0.5, s.end - s.start);
        const newStart = Math.max(0, s.start + delta);
        let newEnd = newStart + segDur;
        if (duration && newEnd > duration) newEnd = duration;
        return { ...s, start: newStart, end: newEnd };
      }),
    );
  }, [pushEditHistory, duration]);

  const deleteSeg = (id) => {
    setEdited((prev) => prev.filter((seg) => seg._id !== id));
  };

  // Compute how many visual lines a segment will occupy in the video.
  const linesForSeg = useCallback((text) => {
    const displayText = applyCase(text || "", textCase);
    const tier = getTier(displayText);
    const fontCss = FONT_CSS_MAP[font] || FONT_CSS_MAP[""];
    const sizePx = Math.round(tier.sizePx * Math.max(0.6, Math.min(1.5, fontScale)));
    return estimateWrappedLines(displayText, fontCss, sizePx, tier.maxWidthPx);
  }, [font, textCase, fontScale]);

  // Split a segment at the optimal word boundary (last word that fits on line 1).
  // Creates two child segments with timestamps split proportionally to word count.
  const splitSeg = (id) => {
    pushEditHistory();
    setEdited((prev) => {
      const idx = prev.findIndex((s) => s._id === id);
      if (idx === -1) return prev;
      const seg = prev[idx];
      const displayText = applyCase(seg.text || "", textCase);
      const tier = getTier(displayText);
      const fontCss = FONT_CSS_MAP[font] || FONT_CSS_MAP[""];
      const sizePx = Math.round(tier.sizePx * Math.max(0.6, Math.min(1.5, fontScale)));

      // Find the split word index: last word whose prefix fits in maxWidthPx
      const words = seg.text.split(" ");
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      ctx.font = `bold ${sizePx}px ${fontCss}`;
      const spaceW = ctx.measureText(" ").width;
      let lineW = 0;
      let splitIdx = Math.floor(words.length / 2); // fallback: half
      for (let wi = 0; wi < words.length - 1; wi++) {
        const ww = ctx.measureText(applyCase(words[wi], textCase)).width;
        lineW = lineW > 0 ? lineW + spaceW + ww : ww;
        if (lineW > tier.maxWidthPx) {
          splitIdx = wi > 0 ? wi : 1;
          break;
        }
        splitIdx = wi + 1;
      }

      const part1 = words.slice(0, splitIdx).join(" ");
      const part2 = words.slice(splitIdx).join(" ");
      const ratio = splitIdx / words.length;
      const midTime = seg.start + (seg.end - seg.start) * ratio;
      const gap = 0.05;
      const nextId1 = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      const nextId2 = nextId1 + 1;
      const s1 = { ...seg, _id: nextId1, text: part1, end: Math.max(seg.start + 0.3, midTime - gap) };
      const s2 = { ...seg, _id: nextId2, text: part2, start: Math.min(seg.end - 0.3, midTime), end: seg.end };
      return [...prev.slice(0, idx), s1, s2, ...prev.slice(idx + 1)];
    });
  };

  // Insert a duplicate of `seg` immediately after it. Same text, same
  // duration, start placed right after the original ends so the new
  // row visibly differs in time. Operator typically re-syncs it via
  // Sync mode tap or manual edit. Useful when Whisper missed a chorus
  // repeat — duplicate the chorus block, then tap-sync the copies.
  const duplicateSeg = (id) => {
    setEdited((prev) => {
      const idx = prev.findIndex((s) => s._id === id);
      if (idx === -1) return prev;
      const orig = prev[idx];
      const segDur = Math.max(0.5, orig.end - orig.start);
      const newStart = Math.min(duration || orig.end + segDur, orig.end);
      const newEnd = Math.min(duration || newStart + segDur, newStart + segDur);
      const nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      const dup = { ...orig, _id: nextId, start: newStart, end: newEnd };
      return [...prev.slice(0, idx + 1), dup, ...prev.slice(idx + 1)];
    });
  };

  // Append a blank line at the end of the list. Operator types the
  // missing lyrics into the text input, then tap-syncs it.
  const addBlankLine = () => {
    setEdited((prev) => {
      const last = prev[prev.length - 1];
      const baseStart = last ? Math.min(duration || last.end + 2, last.end + 0.5) : 0;
      const baseEnd = Math.min(duration || baseStart + 3, baseStart + 3);
      const nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      return [...prev, { _id: nextId, start: baseStart, end: baseEnd, text: "" }];
    });
  };

  const name = filename.replace(/\.(mp3|wav)$/i, "");
  const pendingSuggestions = edited.filter((seg) => {
    const s = suggestionsById[seg._id];
    return s && s !== seg.text;
  }).length;
  const hasSuggestions = pendingSuggestions > 0;
  const blankCount = edited.filter((seg) => !(seg.text || "").trim()).length;

  const _buildCleanedSegments = () => {
    const sorted = [...edited]
      .filter((seg) => (seg.text || "").trim())
      .sort((a, b) => a.start - b.start);
    return sorted.map((seg, i) => {
      let end = seg.end;
      if (i + 1 < sorted.length) {
        const nextStart = sorted[i + 1].start;
        if (end > nextStart - 0.05) {
          end = Math.max(seg.start + 0.3, nextStart - 0.05);
        }
      }
      return { ...seg, end };
    });
  };

  const handleApprove = () => {
    // Check for 3+ line segments before submitting — show a warning banner
    // so the operator can auto-split them rather than discover the issue
    // after waiting for the full video render.
    const problematic = edited.filter(
      (seg) => (seg.text || "").trim() && linesForSeg(seg.text) >= 3
    );
    if (problematic.length > 0 && !wrapWarning) {
      setWrapWarning({ ids: problematic.map((s) => s._id) });
      return;
    }
    setWrapWarning(null);
    setIsDirty(false);
    const cleaned = _buildCleanedSegments();
    onApprove(cleaned.map(({ _id, ...rest }) => rest));
  };

  const progressPct = duration > 0 ? (currentTime / duration) * 100 : 0;

  const handleScrub = (e) => {
    if (!duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    seekTo(pct * duration, false);
  };

  return (
    <div className="w-full max-w-3xl animate-fade-in">
      {/* Hidden audio element drives playback. */}
      {audioUrl && (
        <audio
          ref={audioRef}
          src={audioUrl}
          onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          onEnded={() => setIsPlaying(false)}
        />
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div className="flex items-center gap-3">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h2 className="text-lg font-bold tracking-tight">{t("editor.title")}</h2>
            <p className="text-sm text-ink-secondary">
              {name}
              {batchProgress && <span className="ml-2 text-brand-light text-xs">({batchProgress})</span>}
            </p>
          </div>
        </div>
        <button onClick={handleApprove} className="btn-primary text-sm h-11 px-5">
          {isBatch ? t("editor.approve_next") : t("editor.approve_generate")}
          <svg className="inline-block ml-1.5 w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>
      </div>

      {coverageWarning && (
        <div className="mb-4 rounded-2xl ring-1 ring-accent/25 bg-accent/[0.06] px-4 py-3 flex items-start gap-3">
          <svg className="w-5 h-5 text-accent flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
          <p className="text-xs text-ink-secondary leading-relaxed">
            {t("editor.coverage_warning")}
          </p>
        </div>
      )}

      {/* Auto-split banner. Solo aparece cuando detectamos ≥1 segment
          que matchea contra 2 lineas consecutivas de la referencia.
          Es la acción primaria sugerida cuando la pipeline cayó al
          recovery path o cualquier output mergeó 2 lyric lines en 1. */}
      {mergeableSegments.length > 0 && (
        <div className="mb-4 rounded-2xl ring-1 ring-amber-500/30 bg-amber-500/[0.08] px-4 py-3 flex items-start gap-3">
          <svg className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <path d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium text-white">
              {(t("editor.auto_split_title") || "Detectamos {n} segments con 2 líneas mergeadas")
                .replace("{n}", mergeableSegments.length)}
            </p>
            <p className="text-[11px] text-ink-secondary mt-0.5 leading-relaxed">
              {t("editor.auto_split_desc") ||
                "Podemos auto-dividirlos usando tu letra de referencia para que cada línea tenga su propio timestamp."}
            </p>
          </div>
          <button
            type="button"
            onClick={autoSplitAllFromReference}
            className="shrink-0 px-3 py-1.5 rounded-md text-xs font-medium text-white bg-amber-500/80 hover:bg-amber-500 ring-1 ring-amber-400/30 transition-colors"
          >
            {(t("editor.auto_split_button") || "Auto-dividir {n}")
              .replace("{n}", mergeableSegments.length)}
          </button>
        </div>
      )}

      {(hasSuggestions || editHistory.length > 0) && (
        <div className="flex items-center justify-between mb-4 gap-3">
          <p className="text-xs text-gray-500 truncate">
            {hasSuggestions
              ? `${pendingSuggestions} ${t("editor.suggestions")}.`
              : ""}
          </p>
          <div className="flex items-center gap-2 shrink-0">
            {editHistory.length > 0 && (
              <button onClick={undoEdit}
                title={t("editor.undo_hint") || "Cmd/Ctrl+Z"}
                className="text-xs font-medium text-gray-400 hover:text-white transition-colors flex items-center gap-1 px-3 py-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] ring-1 ring-white/[0.06]">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M3 7v6h6M3 13a9 9 0 109-9" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                {t("editor.undo") || "Deshacer"}
              </button>
            )}
            {hasSuggestions && (
              <button onClick={applyAllSuggestions}
                className="text-xs font-medium text-accent hover:text-accent/80 transition-colors flex items-center gap-1 px-3 py-1.5 rounded-lg bg-accent/5 hover:bg-accent/10">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
                {t("editor.apply_all")}
              </button>
            )}
          </div>
        </div>
      )}

      {/* ─── Audio control bar — sticky-ish above the lyrics list ─── */}
      {audioUrl && (
        <div className="mb-4 flex items-center gap-3 px-3 py-2.5 rounded-card bg-surface-2/60 ring-1 ring-white/[0.05]" data-tour="editor-playbar">
          <button
            onClick={togglePlay}
            className="w-10 h-10 rounded-full bg-brand hover:bg-brand-light text-white flex items-center justify-center transition-colors shrink-0"
            aria-label={isPlaying ? "Pausar" : "Reproducir"}
          >
            {isPlaying ? (
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="5" width="4" height="14" rx="1"/>
                <rect x="14" y="5" width="4" height="14" rx="1"/>
              </svg>
            ) : (
              <svg className="w-4 h-4 ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z"/>
              </svg>
            )}
          </button>
          <span className="text-xs text-ink-secondary tabular-nums shrink-0 w-10 text-right">
            {formatTime(currentTime)}
          </span>
          <button
            onClick={handleScrub}
            className="flex-1 h-1.5 bg-surface-3/60 rounded-full overflow-hidden cursor-pointer relative"
            aria-label="Buscar"
          >
            <div
              className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-100 ease-linear"
              style={{ width: `${Math.min(100, Math.max(0, progressPct))}%` }}
            />
          </button>
          <span className="text-xs text-gray-500 tabular-nums shrink-0 w-10">
            {formatTime(duration)}
          </span>
          <span className="hidden sm:inline text-[10px] text-gray-600 ml-1 shrink-0">
            <kbd className="px-1.5 py-0.5 rounded bg-surface-3/60 ring-1 ring-white/[0.05]">space</kbd>
          </span>
        </div>
      )}

      {/* ─── Tap-to-sync entry / active panel ────────────────────── */}
      {audioUrl && !syncMode && (
        <div className="mb-3 px-3 py-2.5 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] flex items-center gap-3">
          <svg className="w-4 h-4 text-ink-secondary shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7v5l3 2" strokeLinecap="round" />
          </svg>
          <div className="flex-1 min-w-0">
            <p className="text-[12px] text-white font-medium leading-tight">
              {t("editor.sync_cta_title") || "¿Necesitás ajustar los tiempos?"}
            </p>
            <p className="text-[10px] text-gray-500 leading-tight mt-0.5">
              {t("editor.sync_cta_hint") || "Activá modo Sync y apretá Espacio cuando arranque cada línea"}
            </p>
          </div>
          <button
            data-tour="editor-sync-entry"
            onClick={enterSyncMode}
            className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-brand/15 text-brand-light
              ring-1 ring-brand/30 hover:bg-brand/25 transition-colors"
          >
            {t("editor.sync_enter") || "Activar modo Sync"}
          </button>
        </div>
      )}

      {audioUrl && syncMode && (
        <div className="mb-3 px-3 py-2 rounded-card bg-brand/[0.08] ring-1 ring-brand/40 animate-fade-in">
          {/* Top row: status + counter + exit. Compact, single line. */}
          <div className="flex items-center justify-between mb-1.5 gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-brand animate-pulse shrink-0" />
              <span className="text-[10px] font-semibold text-brand-light uppercase tracking-wider shrink-0">
                {t("editor.sync_mode_on") || "Sync"}
              </span>
              <span className="text-[10px] text-gray-500 tabular-nums shrink-0">
                {syncCursor + 1}/{edited.length}
              </span>
              <span className="hidden sm:inline text-[10px] text-gray-600 ml-2 truncate">
                <kbd className="px-1 py-0.5 rounded bg-surface-3/60 ring-1 ring-white/[0.05] font-mono text-[9px]">space</kbd>
                {" anclar · "}
                <kbd className="px-1 py-0.5 rounded bg-surface-3/60 ring-1 ring-white/[0.05] font-mono text-[9px]">Z</kbd>
                {" deshace"}
              </span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <label className="flex items-center gap-1.5 text-[10px] text-gray-400 cursor-pointer select-none"
                title={t("editor.sync_cascade_hint") || "Cuando está activo, el delta de cada tap se aplica también a las líneas siguientes"}>
                <input
                  type="checkbox"
                  checked={syncCascade}
                  onChange={(e) => setSyncCascade(e.target.checked)}
                  className="w-3 h-3 accent-brand"
                />
                {t("editor.sync_cascade_label") || "Arrastrar siguientes"}
              </label>
              <button
                onClick={exitSyncMode}
                className="text-[10px] text-gray-400 hover:text-white px-1.5 py-0.5 transition-colors"
              >
                {t("editor.sync_exit") || "Salir"}
              </button>
            </div>
          </div>
          {/* Action row: line text on left (visual hero), compact button on right. */}
          <div className="flex items-center gap-2">
            <p className="flex-1 text-sm text-white font-medium leading-snug line-clamp-1 min-w-0">
              {edited[syncCursor]?.text || <span className="text-gray-500 italic">(sin texto)</span>}
            </p>
            <span className="text-[10px] font-mono text-brand-light tabular-nums shrink-0">
              {formatTime(currentTime)}
            </span>
            <button
              onClick={tapAnchor}
              className="shrink-0 h-8 px-3 rounded-lg bg-brand hover:bg-brand-light text-white text-[12px]
                font-semibold transition-colors flex items-center gap-1.5"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              {t("editor.sync_tap") || "Anclar"}
            </button>
            <button
              onClick={undoLastAnchor}
              disabled={syncHistory.length === 0}
              title={t("editor.sync_undo_btn") || "Deshacer"}
              className="shrink-0 w-8 h-8 rounded-lg bg-surface-2/60 ring-1 ring-white/[0.05]
                text-gray-300 hover:text-white hover:bg-surface-2 disabled:opacity-30
                disabled:cursor-not-allowed transition-colors flex items-center justify-center"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M3 7v6h6M3 13a9 9 0 109-9" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </div>
      )}


      {/* ─── Misaligned-first-line banner ───────────────────────────────
          Two signals merged into one banner:

          (a) Real instrumental intro: first lyric > 3 s into the audio.
              Offer to collapse it to 2 s or 0 s of pre-roll.

          (b) LRC author put line 1 at 0:00 even though there's a long
              instrumental intro before vocals start. Detected by an
              anomalously large gap between line 1 and line 2 — a chorus
              line typically follows ~8 s after the first verse line, so
              a 15+ s gap with line 1 at ~0:00 is a strong signal the
              author marked line 1 to "show through the intro" and the
              real vocal entry is roughly where line 2 starts. We offer
              to nudge line 1 only — leaves the rest of the timeline
              (which is correct relative to line 2) untouched. */}
      {(() => {
        if (syncMode || edited.length === 0) return null;
        const first = edited[0];
        const second = edited[1];

        if (first.start > 3) {
          return (
            <div className="mb-3 px-3 py-2.5 rounded-card bg-brand/[0.06] ring-1 ring-brand/20 flex items-center gap-3 animate-fade-in">
              <svg className="w-4 h-4 text-brand-light shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
              </svg>
              <p className="text-xs text-ink-secondary flex-1">
                {t("editor.intro_long_title") || "Tu canción arranca a"}{" "}
                <span className="font-mono text-brand-light">{formatTimestamp(first.start)}</span>
                {" "}
                <span className="text-gray-500">
                  ({Math.round(first.start)}s {t("editor.intro_long_hint") || "de intro instrumental"})
                </span>
              </p>
              <button
                onClick={() => shiftAllSegments(-(first.start - 2))}
                className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-brand/15 text-brand-light
                  ring-1 ring-brand/30 hover:bg-brand/25 transition-colors"
              >
                {t("editor.intro_trim_to_2") || "Recortar a 2s"}
              </button>
              <button
                onClick={() => shiftAllSegments(-first.start)}
                className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-surface-2/60
                  ring-1 ring-white/[0.06] text-gray-300 hover:bg-surface-2 hover:text-white transition-colors"
              >
                {t("editor.intro_trim_to_0") || "Empezar en 0s"}
              </button>
            </div>
          );
        }

        // Detect lrclib's "first line at 0:00" pattern: line 1 is near
        // t=0 but line 2 is suspiciously far away — usually an LRC
        // authoring quirk where the first line is anchored to song
        // start instead of the first vocal entry.
        if (first.start <= 1.0 && second && edited.length >= 4) {
          // Compute typical gap from lines 2..min(6) so a single odd
          // value doesn't skew the threshold.
          const gaps = [];
          for (let i = 1; i < Math.min(edited.length - 1, 6); i++) {
            gaps.push(edited[i + 1].start - edited[i].start);
          }
          const median = gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)] || 0;
          const firstGap = second.start - first.start;
          // Trigger when line 1 → line 2 is meaningfully longer than
          // typical gap and the absolute gap is non-trivial. Threshold
          // is conservative so a normal song-with-no-intro doesn't
          // false-positive.
          if (median > 0 && firstGap > median * 2 && firstGap > 8) {
            const suggested = Math.max(0, second.start - median);
            const fixFirstOnly = () => {
              pushEditHistory();
              setEdited((prev) =>
                prev.map((s, i) => {
                  if (i !== 0) return s;
                  const segDur = Math.max(0.5, s.end - s.start);
                  let newEnd = suggested + segDur;
                  if (duration && newEnd > duration) newEnd = duration;
                  return { ...s, start: suggested, end: newEnd };
                }),
              );
            };
            return (
              <div className="mb-3 px-3 py-2.5 rounded-card bg-amber-500/[0.07] ring-1 ring-amber-500/25 flex items-center gap-3 animate-fade-in">
                <svg className="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
                </svg>
                <p className="text-xs text-ink-secondary flex-1">
                  {t("editor.first_line_misaligned") ||
                    "La primera línea parece estar en 0:00 pero la canción arranca más tarde."}{" "}
                  <span className="text-gray-500">
                    {t("editor.first_line_misaligned_hint") || "¿Moverla a"}{" "}
                    <span className="font-mono text-amber-300">{formatTimestamp(suggested)}</span>?
                  </span>
                </p>
                <button
                  onClick={fixFirstOnly}
                  className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-amber-500/15 text-amber-300
                    ring-1 ring-amber-500/30 hover:bg-amber-500/25 transition-colors"
                >
                  {t("editor.first_line_fix") || "Mover sólo línea 1"}
                </button>
              </div>
            );
          }
        }

        return null;
      })()}

      {/* ─── Global timing offset ───────────────────────────────────
          Always-available panel for the common "the whole song is ±N ms
          off" case. Whisper's per-segment timestamps can drift by 200-
          800 ms (codec lag, intro silence, etc.); rather than nudging
          every line manually, the operator shifts the entire timeline.
          Collapsed by default — opens when user clicks the toggle.   */}
      <div className="mb-3">
        <button
          onClick={() => setShiftPanelOpen((v) => !v)}
          className="w-full flex items-center justify-between px-3 py-2 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] text-xs text-gray-300 hover:text-white transition-colors"
        >
          <span className="flex items-center gap-2">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M8 7h12M8 12h12M8 17h12M4 7h.01M4 12h.01M4 17h.01" />
            </svg>
            {t("editor.shift_panel_title") || "Mover toda la canción"}
          </span>
          <svg
            className={`w-3.5 h-3.5 transition-transform ${shiftPanelOpen ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
          ><path d="M19 9l-7 7-7-7" /></svg>
        </button>

        {shiftPanelOpen && (
          <div className="mt-2 px-3 py-3 rounded-card bg-surface-1/40 ring-1 ring-white/[0.04] space-y-3 animate-fade-in">
            <p className="text-[11px] text-gray-500 leading-relaxed">
              {t("editor.shift_panel_hint") ||
                "Aplica un offset uniforme a todas las líneas. Si la letra aparece tarde, usá valores negativos (anticipar). Si aparece antes de tiempo, positivos (atrasar). Drift típico de lyrics curadas: 100-200ms."}
            </p>

            {/* Slider continuo */}
            <div className="flex items-center gap-3">
              <span className="text-[10px] font-mono text-gray-500 w-12 text-right">-1000ms</span>
              <input
                type="range"
                min={-1000}
                max={1000}
                step={10}
                value={shiftDraftMs}
                onChange={(e) => setShiftDraftMs(parseInt(e.target.value, 10))}
                className="flex-1 accent-brand"
              />
              <span className="text-[10px] font-mono text-gray-500 w-12">+1000ms</span>
            </div>

            {/* Presets + valor actual + input custom. Granularidad fina
                para drift típico de lrclib synced (100-200ms) + presets
                más gruesos para mismatches mayores. */}
            <div className="flex flex-wrap items-center gap-2">
              {[-250, -150, -100, -50, 0, 50, 100, 150, 250].map((preset) => (
                <button
                  key={preset}
                  onClick={() => setShiftDraftMs(preset)}
                  className={`text-[11px] font-mono px-2.5 py-1 rounded ring-1 transition-colors ${
                    shiftDraftMs === preset
                      ? "bg-brand/20 ring-brand/40 text-brand-light"
                      : "bg-surface-2/40 ring-white/[0.05] text-gray-300 hover:text-white"
                  }`}
                >
                  {preset > 0 ? "+" : ""}{preset}ms
                </button>
              ))}
              <span className="text-[10px] text-gray-500">{t("editor.shift_or_custom") || "o"}</span>
              <input
                type="number"
                step={10}
                value={shiftDraftMs}
                onChange={(e) => {
                  const v = parseInt(e.target.value || "0", 10);
                  if (!Number.isNaN(v)) {
                    // clamp to slider range; users can still apply by
                    // calling repeatedly if they need bigger shifts.
                    setShiftDraftMs(Math.max(-1000, Math.min(1000, v)));
                  }
                }}
                className="w-20 text-[11px] font-mono px-2 py-1 rounded bg-surface-2/40 ring-1 ring-white/[0.05] text-white"
              />
              <span className="text-[10px] text-gray-500">ms</span>
              <button
                onClick={() => {
                  if (shiftDraftMs === 0) return;
                  const applied = shiftDraftMs;
                  shiftAllSegments(applied / 1000);  // ms → seconds
                  setAppliedShiftMs(applied);
                  setShiftDraftMs(0);
                }}
                disabled={shiftDraftMs === 0}
                className="ml-auto text-[11px] font-semibold px-3 py-1.5 rounded-lg bg-brand/20 ring-1 ring-brand/40 text-brand-light hover:bg-brand/30 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {t("editor.shift_apply") || "Aplicar"}
              </button>
            </div>

            {/* Inline confirmation chip — clears after 2.5s. Without it
                the operator can't distinguish "applied" from "didn't
                register" because the slider returns to 0 on success. */}
            {appliedShiftMs != null && (
              <div className="flex items-center gap-2 text-[11px] text-emerald-300 animate-fade-in">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span className="font-mono">
                  {(t("editor.shift_applied") || "Aplicado: {n}ms")
                    .replace("{n}", appliedShiftMs > 0 ? `+${appliedShiftMs}` : appliedShiftMs)}
                </span>
                <span className="text-gray-500">·</span>
                <span className="text-gray-400">{t("editor.shift_applied_undo") || "Cmd/Ctrl+Z para revertir"}</span>
              </div>
            )}

            <p className="text-[10px] text-gray-600 leading-relaxed">
              {t("editor.shift_undo_hint") || "Deshacer con Cmd/Ctrl+Z o el botón de deshacer."}
            </p>
          </div>
        )}
      </div>

      {/* ─── Lyrics list ──────────────────────────────────────────── */}
      <p className="text-[11px] text-gray-600 mb-2 px-1">
        {t("editor.list_hint") || "Click en un tiempo para reproducir desde ahí · doble click para editarlo"}
      </p>
      <div className="relative">
        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-surface to-transparent pointer-events-none z-10 rounded-b-2xl" />
        <div ref={listRef} className="space-y-1 max-h-[55vh] overflow-y-auto pr-1 pb-8">
          {edited.map((seg, idx) => {
            const suggestion = suggestionsById[seg._id];
            const isApplied = suggestion && seg.text === suggestion;
            const isActive = seg._id === activeId;
            const isArmed = syncMode && idx === syncCursor;
            const isAnchored = syncMode && idx < syncCursor;

            return (
              <div
                key={seg._id}
                ref={(el) => { rowRefs.current[seg._id] = el; }}
                {...(idx === 0 ? { "data-tour": "editor-list-row" } : {})}
                className={`group rounded-xl transition-all
                  ${isArmed ? "bg-brand/[0.18] ring-2 ring-brand shadow-glow scale-[1.01]" : ""}
                  ${!isArmed && isActive ? "bg-brand/[0.07] ring-1 ring-brand/25" : ""}
                  ${isAnchored ? "opacity-60" : ""}`}
              >
                <div className="flex items-start gap-2 p-1">
                  {editingId === seg._id ? (
                    <input
                      type="text"
                      autoFocus
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      onBlur={() => commitEditTimestamp(seg)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") { e.preventDefault(); commitEditTimestamp(seg); }
                        else if (e.key === "Escape") { e.preventDefault(); cancelEditTimestamp(); }
                      }}
                      className="text-[11px] font-mono pt-2 w-14 shrink-0 text-right bg-surface-1
                        border border-brand/40 focus:border-brand outline-none rounded-md px-1
                        text-brand-light"
                    />
                  ) : (
                    <button
                      onClick={() => seekTo(Math.max(0, seg.start), true)}
                      onDoubleClick={() => startEditTimestamp(seg)}
                      title={t("editor.timestamp_hint") || "Click: ir al tiempo · Doble click: editar"}
                      className={`text-[11px] font-mono pt-2.5 w-14 shrink-0 text-right transition-colors
                        ${isActive ? "text-brand-light" : "text-gray-600 hover:text-brand-light"}`}
                    >
                      {formatTimestamp(seg.start)}
                    </button>
                  )}
                  <div className="flex-1 min-w-0">
                    <input
                      type="text"
                      value={seg.text}
                      onChange={(e) => updateText(seg._id, e.target.value)}
                      onFocus={() => { seekTo(seg.start, false); setFocusedSegId(seg._id); }}
                      className={`w-full px-3 py-2 rounded-xl bg-surface-1 border text-sm text-white
                        focus:border-brand/40 focus:outline-none hover:border-white/[0.08] transition-all
                        ${suggestion && !isApplied ? "border-amber-500/20" : "border-white/[0.04]"}`}
                    />
                    {/* Wrap indicator + split action */}
                    {(() => {
                      if (!(seg.text || "").trim()) return null;
                      const lines = linesForSeg(seg.text);
                      if (lines <= 1) return null;
                      return (
                        <div className="flex items-center gap-2 mt-1 ml-1">
                          {lines === 2 ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                              bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/25 text-[10px] font-medium">
                              <span className="relative flex h-1.5 w-1.5">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60"/>
                                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400"/>
                              </span>
                              ⚠ 2 líneas
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                              bg-red-500/10 text-red-300 ring-1 ring-red-500/25 text-[10px] font-medium">
                              ✗ {lines} líneas
                            </span>
                          )}
                          <button
                            onClick={() => splitSeg(seg._id)}
                            className="text-[10px] text-brand hover:text-brand-light transition-colors
                              flex items-center gap-0.5 px-2 py-0.5 rounded-lg
                              bg-brand/5 hover:bg-brand/15 ring-1 ring-brand/20"
                          >
                            ✂ Dividir
                          </button>
                        </div>
                      );
                    })()}
                    {suggestion && !isApplied && (
                      <button onClick={() => applySuggestion(seg._id)}
                        className="flex items-center gap-1.5 mt-1 ml-1 px-2 py-1 rounded-lg
                          bg-accent/5 hover:bg-accent/15 text-accent/70 hover:text-accent
                          text-[11px] transition-all group/btn">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                        <span className="text-gray-500 group-hover/btn:text-accent transition-colors">
                          {suggestion}
                        </span>
                      </button>
                    )}
                  </div>
                  <div className="shrink-0 flex items-center gap-0.5 mt-0.5">
                    {!syncMode && (
                      <button onClick={() => enterSyncModeAt(idx)}
                        {...(idx === 0 ? { "data-tour": "editor-row-sync" } : {})}
                        className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                          hover:bg-brand/15 flex items-center justify-center text-gray-600
                          hover:text-brand-light transition-all"
                        title={t("editor.sync_from_here") || "Activar Sync desde esta línea"}>
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                          <circle cx="12" cy="12" r="9" />
                          <circle cx="12" cy="12" r="4" />
                          <circle cx="12" cy="12" r="1" fill="currentColor" />
                        </svg>
                      </button>
                    )}
                    <button onClick={() => duplicateSeg(seg._id)}
                      className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                        hover:bg-brand/10 flex items-center justify-center text-gray-600
                        hover:text-brand-light transition-all"
                      title={t("editor.duplicate_line") || "Duplicar línea (útil para estribillos repetidos)"}>
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <rect x="9" y="9" width="11" height="11" rx="1.5" />
                        <path d="M5 15V5a1 1 0 011-1h10" />
                      </svg>
                    </button>
                    <button onClick={() => deleteSeg(seg._id)}
                      className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                        hover:bg-red-500/10 flex items-center justify-center text-gray-600
                        hover:text-red-400 transition-all"
                      title="Eliminar línea">
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <path d="M18 6L6 18M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
          <button
            data-tour="editor-add-line"
            onClick={addBlankLine}
            className="w-full mt-2 py-2.5 rounded-xl border border-dashed border-white/[0.08]
              hover:border-brand/40 hover:bg-brand/[0.04] text-gray-500 hover:text-brand-light
              text-[12px] transition-all flex items-center justify-center gap-1.5"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 5v14M5 12h14" />
            </svg>
            {t("editor.add_line") || "Agregar línea"}
          </button>
        </div>
      </div>

      <div className="mt-4 flex justify-between items-center gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs text-gray-600 shrink-0">
            {edited.length} {t("editor.lines")}
          </span>
          {blankCount > 0 && (
            <span className="text-[11px] text-amber-400 truncate">
              · {blankCount} {blankCount === 1 ? t("editor.blank_singular") || "línea en blanco" : t("editor.blank_plural") || "líneas en blanco"} —{" "}
              {t("editor.blanks_dropped") || "se omitirán"}
            </span>
          )}
        </div>
        <button onClick={handleApprove} className="btn-primary text-sm h-11 px-5 shrink-0" data-tour="editor-approve">
          {isBatch ? t("editor.approve_next") : t("editor.approve_generate")}
        </button>
      </div>

      {/* ── 3+ line wrap warning banner ────────────────────────────── */}
      {wrapWarning && (
        <div className="mt-3 rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-4 animate-fade-in">
          <p className="text-sm font-semibold text-red-300 mb-1">
            {wrapWarning.ids.length === 1
              ? "1 línea ocupará 3+ renglones en el video"
              : `${wrapWarning.ids.length} líneas ocuparán 3+ renglones en el video`}
          </p>
          <p className="text-xs text-red-400/70 mb-3">
            Las líneas marcadas en rojo quedarán muy largas. Podés dividirlas ahora o continuar igual.
          </p>
          <div className="flex gap-3">
            <button
              onClick={() => {
                // Auto-split all problematic segments
                wrapWarning.ids.forEach((id) => splitSeg(id));
                setWrapWarning(null);
              }}
              className="inline-flex items-center gap-1.5 h-9 px-4 rounded-button text-xs font-semibold
                text-white bg-brand hover:bg-brand/90 transition-colors"
            >
              ✂ Auto-dividir todo
            </button>
            <button
              onClick={() => {
                setWrapWarning(null);
                const cleaned = _buildCleanedSegments();
                onApprove(cleaned.map(({ _id, ...rest }) => rest));
              }}
              className="btn-secondary h-9 px-4 text-xs"
            >
              Continuar igual
            </button>
          </div>
        </div>
      )}

      {/* ── Live preview panel ──────────────────────────────────────── */}
      {focusedSegId !== null && (() => {
        const seg = edited.find((s) => s._id === focusedSegId);
        if (!seg || !(seg.text || "").trim()) return null;
        const displayText = applyCase(seg.text, textCase);
        const tier = getTier(displayText);
        // AUTO means the worker random-picks per-job from an 8-font pool
        // at render time (pipeline.py:_FONT_POOL + random.choice). The
        // preview can't honestly show what that pick will be, so we
        // render with a neutral fallback (Montserrat) and dim it +
        // surface a badge so the operator knows the final font will
        // differ. Without this the preview looks identical for every
        // song in a batch and the operator (rightly) thinks the worker
        // is going to render them all the same.
        const isAutoFont = !font;
        const fontCss = FONT_CSS_MAP[font] || FONT_CSS_MAP[""];
        const basePx = tier.sizePx;
        const scaledPx = Math.round(basePx * Math.max(0.6, Math.min(1.5, fontScale)));
        // Scale down to preview container (preview is ~480px wide → video is 1920px)
        const previewRatio = 480 / 1920;
        const previewFontPx = Math.max(10, Math.round(scaledPx * previewRatio));
        const lines = estimateWrappedLines(displayText, fontCss, scaledPx, tier.maxWidthPx);
        return (
          <div className="mt-4 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] overflow-hidden animate-fade-in">
            <div className="flex items-center justify-between px-4 py-2 border-b border-white/[0.04]">
              <span className="text-[11px] text-gray-500 uppercase tracking-wider">
                {t("editor.preview_header") || "Preview — cómo quedarán las lyrics"}
              </span>
              <button onClick={() => setFocusedSegId(null)} className="text-gray-600 hover:text-white text-xs transition-colors">✕</button>
            </div>
            {/* AUTO badge: shown only when no explicit font picked. Sits
                above the 16:9 preview so it's the first thing the eye
                hits before reading the rendered text. */}
            {isAutoFont && (
              <div className="px-4 py-2 bg-amber-500/[0.06] border-b border-amber-500/20 flex items-start gap-2">
                <svg className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M12 8h.01M11 12h1v4h1" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                <p className="text-[11px] text-amber-200/90 leading-relaxed">
                  <span className="font-semibold">{t("editor.auto_font_badge") || "Tipografía: Auto"}</span>
                  {" · "}
                  {t("editor.auto_font_explainer") || "el render va a elegir una de 8 fuentes al azar por canción. Esta vista previa usa Montserrat solo de referencia — el video final puede verse distinto."}
                </p>
              </div>
            )}
            {/* 16:9 preview card */}
            <div
              className="relative w-full flex items-center justify-center bg-gradient-to-b from-gray-900 to-black"
              style={{ aspectRatio: "16/9", maxHeight: "180px" }}
            >
              <p
                style={{
                  fontFamily: fontCss,
                  fontSize: `${previewFontPx}px`,
                  fontWeight: 700,
                  color: "white",
                  // Dim AUTO previews so they don't read as "final look"
                  opacity: isAutoFont ? 0.7 : 1,
                  textShadow: textContrast === "strong"
                    ? "0 0 8px rgba(0,0,0,1), -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000, 2px 2px 4px rgba(0,0,0,0.9)"
                    : textContrast === "medium"
                    ? "0 0 4px rgba(0,0,0,0.9), 1px 1px 3px rgba(0,0,0,0.8)"
                    : "1px 1px 2px rgba(0,0,0,0.6)",
                  WebkitTextStroke: textContrast === "strong" ? "1px black" : textContrast === "medium" ? "0.5px black" : "0px",
                  textTransform: "none",
                  textAlign: "center",
                  maxWidth: `${Math.round(tier.maxWidthPx * previewRatio)}px`,
                  lineHeight: 1.25,
                  wordBreak: "break-word",
                  padding: "0 12px",
                }}
              >
                {displayText}
              </p>
              {/* line count badge overlay */}
              <span className={`absolute bottom-2 right-3 text-[10px] font-medium px-2 py-0.5 rounded-full ${
                lines === 1 ? "bg-accent/20 text-accent" :
                lines === 2 ? "bg-amber-500/20 text-amber-300" :
                "bg-red-500/20 text-red-300"
              }`}>
                {lines} {lines === 1 ? "línea" : "líneas"} en el video
              </span>
            </div>
            <div className="px-4 py-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-gray-600">
              <span className={isAutoFont ? "text-amber-400 font-medium" : ""}>
                Fuente: {font || (t("editor.auto_font_inline") || "Auto (se elige al renderizar)")}
              </span>
              <span>Tamaño: {fontScale}×</span>
              <span className={textCase === "title" ? "text-amber-400 font-medium" : ""}>
                Caja: {textCase === "upper" ? "MAYÚSCULAS" : textCase === "title" ? "Título (cada palabra capitalizada)" : textCase === "lower" ? "minúsculas" : "Original"}
              </span>
              <span>Transición: {lyricTransition === "cut" ? "Corte" : lyricTransition === "fade" ? "Fade" : "Fade lento"}</span>
              {SHOW_MOTION_PICKER && (
                <span>Movimiento: {textMotion === "none" ? "Estático" : textMotion === "subtle" ? "Sutil" : "Flotante"}</span>
              )}
              <span>Contraste: {textContrast === "subtle" ? "Suave" : textContrast === "strong" ? "Fuerte" : "Medio"}</span>
            </div>
          </div>
        );
      })()}

      <EditorTour user={user} />
    </div>
  );
}
