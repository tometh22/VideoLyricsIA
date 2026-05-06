import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { useI18n } from "../i18n";

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

export default function LyricsEditor({ segments, filename, audioFile, referenceLyrics, coverageWarning = false, recoverySource = "", onApprove, onBack, isBatch = false, batchProgress = "" }) {
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

  // Global timestamp shift — solves the "desfasaje" case where every
  // line is offset by the same amount. The slider previews live; the
  // operator clicks "Aplicar" to bake it into segments.
  const [shiftOffset, setShiftOffset] = useState(0);
  const shiftedStart = (seg) => seg.start + shiftOffset;

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
  // moment (e.g. instrumental gap). Uses shifted starts so the shift
  // slider previews row highlighting live.
  const activeId = useMemo(() => {
    let containing = null;
    let lastStarted = null;
    for (const seg of edited) {
      const start = seg.start + shiftOffset;
      const end = seg.end + shiftOffset;
      if (currentTime >= start && currentTime < end) containing = seg;
      if (currentTime >= start) lastStarted = seg;
    }
    return (containing || lastStarted)?._id ?? null;
  }, [edited, currentTime, shiftOffset]);

  // Auto-scroll the active row into view while playing.
  const lastScrolledIdRef = useRef(null);
  useEffect(() => {
    if (!isPlaying || activeId === null) return;
    if (lastScrolledIdRef.current === activeId) return;
    lastScrolledIdRef.current = activeId;
    const el = rowRefs.current[activeId];
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeId, isPlaying]);

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

  // Spacebar toggles play/pause when no input is focused.
  useEffect(() => {
    const onKey = (e) => {
      if (e.code !== "Space") return;
      const tag = (document.activeElement?.tagName || "").toUpperCase();
      const editing = tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable;
      if (editing) return;
      e.preventDefault();
      togglePlay();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay]);

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

  const name = filename.replace(/\.(mp3|wav)$/i, "");
  const pendingSuggestions = edited.filter((seg) => {
    const s = suggestionsById[seg._id];
    return s && s !== seg.text;
  }).length;
  const hasSuggestions = pendingSuggestions > 0;

  // Bake the live shift into segments. Clamps to [0, duration] so we
  // never emit negative starts or push end past audio length.
  const applyShift = () => {
    if (shiftOffset === 0) return;
    setEdited((prev) =>
      prev.map((seg) => {
        const segDur = Math.max(0.5, seg.end - seg.start);
        const newStart = Math.max(0, seg.start + shiftOffset);
        let newEnd = newStart + segDur;
        if (duration && newEnd > duration) newEnd = duration;
        return { ...seg, start: newStart, end: newEnd };
      }),
    );
    setShiftOffset(0);
  };

  const handleApprove = () => {
    // Auto-bake any pending shift before approving so the worker
    // gets the operator's final view.
    const baked = shiftOffset === 0
      ? edited
      : edited.map((seg) => {
          const segDur = Math.max(0.5, seg.end - seg.start);
          const newStart = Math.max(0, seg.start + shiftOffset);
          let newEnd = newStart + segDur;
          if (duration && newEnd > duration) newEnd = duration;
          return { ...seg, start: newStart, end: newEnd };
        });
    onApprove(baked.map(({ _id, ...rest }) => rest));
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
        <div className="mb-4 flex items-center gap-3 px-3 py-2.5 rounded-card bg-surface-2/60 ring-1 ring-white/[0.05]">
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

      {/* ─── Global shift control ─────────────────────────────────── */}
      {audioUrl && (
        <div className="mb-3 px-3 py-2.5 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04]">
          <div className="flex items-center gap-3">
            <div className="shrink-0 flex items-center gap-1.5">
              <svg className="w-4 h-4 text-ink-secondary" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                <path d="M8 7l-4 5 4 5M16 7l4 5-4 5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="text-[11px] text-ink-secondary">
                {t("editor.shift_label") || "Desfasaje"}
              </span>
            </div>
            <span className="text-[11px] font-mono text-brand-light tabular-nums shrink-0 w-14 text-center">
              {shiftOffset > 0 ? "+" : ""}{shiftOffset.toFixed(1)}s
            </span>
            <input
              type="range"
              min="-60"
              max="60"
              step="0.5"
              value={shiftOffset}
              onChange={(e) => setShiftOffset(parseFloat(e.target.value))}
              onDoubleClick={() => setShiftOffset(0)}
              className="flex-1 h-1.5 accent-brand cursor-pointer"
              aria-label="Desfasaje global"
            />
            <button
              onClick={applyShift}
              disabled={shiftOffset === 0}
              className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-brand/15 text-brand-light
                ring-1 ring-brand/30 hover:bg-brand/25 disabled:opacity-30 disabled:cursor-not-allowed
                transition-colors"
            >
              {t("editor.shift_apply") || "Aplicar"}
            </button>
            <button
              onClick={() => setShiftOffset(0)}
              disabled={shiftOffset === 0}
              className="shrink-0 text-[11px] text-gray-500 hover:text-white px-2 py-1.5
                disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              {t("editor.shift_reset") || "Reset"}
            </button>
          </div>
          <p className="text-[10px] text-gray-600 mt-1.5 ml-6">
            {t("editor.shift_hint") || "Arrastrá si toda la letra está corrida en el tiempo"}
          </p>
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
          {edited.map((seg) => {
            const suggestion = suggestionsById[seg._id];
            const isApplied = suggestion && seg.text === suggestion;
            const isActive = seg._id === activeId;

            return (
              <div
                key={seg._id}
                ref={(el) => { rowRefs.current[seg._id] = el; }}
                className={`group rounded-xl transition-colors ${isActive ? "bg-brand/[0.07] ring-1 ring-brand/25" : ""}`}
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
                      onClick={() => seekTo(Math.max(0, shiftedStart(seg)), true)}
                      onDoubleClick={() => startEditTimestamp(seg)}
                      title={t("editor.timestamp_hint") || "Click: ir al tiempo · Doble click: editar"}
                      className={`text-[11px] font-mono pt-2.5 w-14 shrink-0 text-right transition-colors
                        ${shiftOffset !== 0 ? "italic" : ""}
                        ${isActive ? "text-brand-light" : "text-gray-600 hover:text-brand-light"}`}
                    >
                      {formatTimestamp(Math.max(0, shiftedStart(seg)))}
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
                  <button onClick={() => deleteSeg(seg._id)}
                    className="shrink-0 w-8 h-8 mt-0.5 rounded-lg opacity-0 group-hover:opacity-100
                      hover:bg-red-500/10 flex items-center justify-center text-gray-600
                      hover:text-red-400 transition-all"
                    title="Eliminar línea">
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-4 flex justify-between items-center">
        <span className="text-xs text-gray-600">{edited.length} {t("editor.lines")}</span>
        <button onClick={handleApprove} className="btn-primary text-sm h-11 px-5">
          {isBatch ? t("editor.approve_next") : t("editor.approve_generate")}
        </button>
      </div>
    </div>
  );
}
