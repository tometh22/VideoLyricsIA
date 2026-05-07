import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { useI18n } from "../i18n";
import { EditorTour } from "./OnboardingTour";

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

export default function LyricsEditor({ segments, filename, audioFile, referenceLyrics, coverageWarning = false, recoverySource = "", onApprove, onBack, isBatch = false, batchProgress = "", user = null }) {
  const { t } = useI18n();
  const [edited, setEdited] = useState(() =>
    segments.map((s, i) => ({ ...s, _id: i }))
  );

  // ─── Audio sync ─────────────────────────────────────────────────────
  const audioUrl = useMemo(
    () => (audioFile ? URL.createObjectURL(audioFile) : null),
    [audioFile],
  );
  useEffect(() => () => { if (audioUrl) URL.revokeObjectURL(audioUrl); }, [audioUrl]);

  const audioRef = useRef(null);
  const listRef = useRef(null);
  const rowRefs = useRef({});

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

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
  // Stack of {id, prevStart, prevEnd} so "Deshacer" can revert the
  // last tap if the operator overshot.
  const [syncHistory, setSyncHistory] = useState([]);

  const startEditTimestamp = (seg) => {
    setEditingId(seg._id);
    setEditValue(formatTimestamp(seg.start));
  };
  const cancelEditTimestamp = () => {
    setEditingId(null);
    setEditValue("");
  };
  // After committing an edit, capture the delta so we can offer to
  // propagate it forward. Stored as { segId, delta, count } so we can
  // undo or apply.
  const [pendingPropagation, setPendingPropagation] = useState(null);

  const commitEditTimestamp = (seg) => {
    const parsed = parseTimestamp(editValue);
    if (parsed == null) {
      // Bad input — silently revert.
      cancelEditTimestamp();
      return;
    }
    const newStart = Math.max(0, Math.min(parsed, duration || parsed));
    const delta = newStart - seg.start;
    setEdited((prev) => prev.map((s) => {
      if (s._id !== seg._id) return s;
      // Preserve segment duration when the operator nudges the start
      // unless that would push end past audio_duration.
      const segDur = Math.max(0.5, s.end - s.start);
      let newEnd = newStart + segDur;
      if (duration && newEnd > duration) newEnd = duration;
      return { ...s, start: newStart, end: newEnd };
    }));
    setEditingId(null);
    setEditValue("");
    // Offer to propagate when the change is meaningful (>0.3s) and
    // there are following lines to receive it.
    const followingCount = edited.filter((s) => s.start > seg.start).length;
    if (Math.abs(delta) >= 0.3 && followingCount > 0) {
      setPendingPropagation({ segId: seg._id, delta, count: followingCount });
    } else {
      setPendingPropagation(null);
    }
  };

  // Apply the captured delta to every segment that originally started
  // AFTER the edited one. We use the original start (pre-edit) of the
  // anchor segment as the threshold so we don't double-shift the line
  // the operator just edited.
  const applyPendingPropagation = () => {
    if (!pendingPropagation) return;
    const { segId, delta } = pendingPropagation;
    const anchor = edited.find((s) => s._id === segId);
    if (!anchor) {
      setPendingPropagation(null);
      return;
    }
    const anchorOrigStart = anchor.start - delta;
    setEdited((prev) =>
      prev.map((s) => {
        if (s.start <= anchorOrigStart) return s;
        const segDur = Math.max(0.5, s.end - s.start);
        const newStart = Math.max(0, s.start + delta);
        let newEnd = newStart + segDur;
        if (duration && newEnd > duration) newEnd = duration;
        return { ...s, start: newStart, end: newEnd };
      }),
    );
    setPendingPropagation(null);
  };

  const dismissPropagation = () => setPendingPropagation(null);

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
    const futureSnapshot = edited
      .slice(syncCursor + 1)
      .map((s) => ({ id: s._id, prevStart: s.start, prevEnd: s.end }));
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
        // Propagate delta to lines after the cursor when the shift is
        // meaningful. Skip when delta is tiny to avoid jittering on
        // micro-adjustments to a line whose original time was correct.
        if (i > syncCursor && Math.abs(delta) >= 0.2) {
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
  }, [syncMode, syncCursor, edited, currentTime, duration]);

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
    setPendingPropagation(null);
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
  }, [togglePlay, syncMode, tapAnchor, undoLastAnchor]);

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

  const updateText = (id, text) => {
    setEdited((prev) => prev.map((seg) => (seg._id === id ? { ...seg, text } : seg)));
  };

  const applySuggestion = (id) => {
    const suggestion = suggestionsById[id];
    if (suggestion) updateText(id, suggestion);
  };

  const applyAllSuggestions = () => {
    setEdited((prev) =>
      prev.map((seg) => {
        const suggestion = suggestionsById[seg._id];
        return suggestion ? { ...seg, text: suggestion } : seg;
      })
    );
  };

  const deleteSeg = (id) => {
    setEdited((prev) => prev.filter((seg) => seg._id !== id));
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

  const handleApprove = () => {
    // Drop empty / whitespace-only rows BEFORE clamping. Operator can
    // leave blanks from "Agregar línea" if they didn't type lyrics —
    // sending those to the worker triggers an ImageMagick "label
    // expected" crash that aborts the whole render.
    const sorted = [...edited]
      .filter((seg) => (seg.text || "").trim())
      .sort((a, b) => a.start - b.start);
    const cleaned = sorted.map((seg, i) => {
      let end = seg.end;
      if (i + 1 < sorted.length) {
        const nextStart = sorted[i + 1].start;
        if (end > nextStart - 0.05) {
          end = Math.max(seg.start + 0.3, nextStart - 0.05);
        }
      }
      return { ...seg, end };
    });
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

      <div className="flex items-center justify-between mb-6">
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

      {hasSuggestions && (
        <div className="flex items-center justify-between mb-4">
          <p className="text-xs text-gray-500">
            {pendingSuggestions} {t("editor.suggestions")}.
          </p>
          <button onClick={applyAllSuggestions}
            className="text-xs font-medium text-accent hover:text-accent/80 transition-colors flex items-center gap-1 px-3 py-1.5 rounded-lg bg-accent/5 hover:bg-accent/10">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
            {t("editor.apply_all")}
          </button>
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
              {t("editor.sync_cta_title") || "¿Los tiempos están todos mal?"}
            </p>
            <p className="text-[10px] text-gray-500 leading-tight mt-0.5">
              {t("editor.sync_cta_hint") || "Activá modo Sync: apretá Espacio cuando arranque cada línea"}
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
          <div className="flex items-center justify-between mb-1.5">
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
            <button
              onClick={exitSyncMode}
              className="text-[10px] text-gray-400 hover:text-white px-1.5 py-0.5 transition-colors shrink-0"
            >
              {t("editor.sync_exit") || "Salir"}
            </button>
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

      {/* ─── Propagate-from-here banner (after a manual timestamp edit) ─── */}
      {pendingPropagation && (
        <div className="mb-3 px-3 py-2.5 rounded-card bg-accent/[0.06] ring-1 ring-accent/25 flex items-center gap-3 animate-fade-in">
          <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <path d="M13 5l7 7-7 7M5 12h15" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <p className="text-xs text-ink-secondary flex-1">
            {t("editor.propagate_question") || "¿Aplicar"}{" "}
            <span className="text-brand-light font-mono">
              {pendingPropagation.delta > 0 ? "+" : ""}
              {pendingPropagation.delta.toFixed(1)}s
            </span>{" "}
            {t("editor.propagate_to") || "a las"}{" "}
            <span className="font-medium text-white">{pendingPropagation.count}</span>{" "}
            {t("editor.propagate_following") || "líneas siguientes también?"}
          </p>
          <button
            onClick={applyPendingPropagation}
            className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-accent/20 text-accent
              ring-1 ring-accent/40 hover:bg-accent/30 transition-colors"
          >
            {t("editor.propagate_yes") || "Sí, aplicar"}
          </button>
          <button
            onClick={dismissPropagation}
            className="shrink-0 text-[11px] text-gray-500 hover:text-white px-2 py-1.5 transition-colors"
          >
            {t("editor.propagate_no") || "Solo esta"}
          </button>
        </div>
      )}

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
                      onFocus={() => seekTo(seg.start, false)}
                      className={`w-full px-3 py-2 rounded-xl bg-surface-1 border text-sm text-white
                        focus:border-brand/40 focus:outline-none hover:border-white/[0.08] transition-all
                        ${suggestion && !isApplied ? "border-amber-500/20" : "border-white/[0.04]"}`}
                    />
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

      <EditorTour user={user} />
    </div>
  );
}
