import { useState, useMemo } from "react";
import { useI18n } from "../i18n";

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.floor((seconds % 1) * 10);
  return `${m}:${s.toString().padStart(2, "0")}.${ms}`;
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
    // Only suggest if there's a real word-level difference (not just punctuation/case)
    const normalize = (s) => s.toLowerCase().replace(/[^a-záéíóúüñ\s]/g, "").replace(/\s+/g, " ").trim();
    if (normalize(bestLine) !== normalize(whisperText)) {
      return bestLine;
    }
  }
  return null;
}

export default function LyricsEditor({ segments, filename, referenceLyrics, onApprove, onBack, isBatch = false, batchProgress = "" }) {
  const { t } = useI18n();
  // Each segment gets a unique ID so deletions don't mess up suggestions
  const [edited, setEdited] = useState(() =>
    segments.map((s, i) => ({ ...s, _id: i }))
  );

  const refLines = useMemo(() => {
    if (!referenceLyrics) return [];
    return referenceLyrics.split("\n").filter((l) => l.trim());
  }, [referenceLyrics]);

  // Suggestions mapped by _id (stable, not by array index)
  const suggestionsById = useMemo(() => {
    const map = {};
    let refIdx = 0;
    segments.forEach((seg, i) => {
      const suggestion = findSuggestion(seg.text, refLines, refIdx);
      map[i] = suggestion;
      if (suggestion) {
        const idx = refLines.findIndex(
          (l, i) => i >= refIdx && l.toLowerCase().includes(seg.text.toLowerCase().split(" ")[0]?.toLowerCase())
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

  const name = filename.replace(/\.mp3$/i, "");
  const pendingSuggestions = edited.filter((seg) => {
    const s = suggestionsById[seg._id];
    return s && s !== seg.text;
  }).length;
  const hasSuggestions = pendingSuggestions > 0;

  const handleApprove = () => {
    // Strip _id before passing up
    onApprove(edited.map(({ _id, ...rest }) => rest));
  };

  return (
    <div className="w-full max-w-3xl animate-fade-in">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h2 className="text-lg font-bold">{t("editor.title")}</h2>
            <p className="text-sm text-gray-500">
              {name}
              {batchProgress && <span className="ml-2 text-brand text-xs">({batchProgress})</span>}
            </p>
          </div>
        </div>
        <button onClick={handleApprove} className="btn-primary text-sm py-2.5 px-5">
          {isBatch ? t("editor.approve_next") : t("editor.approve_generate")}
          <svg className="inline-block ml-1.5 w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>
      </div>

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

      <div className="space-y-1 max-h-[60vh] overflow-y-auto pr-1">
        {edited.map((seg) => {
          const suggestion = suggestionsById[seg._id];
          const isApplied = suggestion && seg.text === suggestion;

          return (
            <div key={seg._id} className="group">
              <div className="flex items-start gap-2">
                <span className="text-[11px] text-gray-600 font-mono pt-2.5 w-14 shrink-0 text-right">
                  {formatTime(seg.start)}
                </span>
                <div className="flex-1 min-w-0">
                  <input
                    type="text"
                    value={seg.text}
                    onChange={(e) => updateText(seg._id, e.target.value)}
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
                    hover:text-red-400 transition-all">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex justify-between items-center">
        <span className="text-xs text-gray-600">{edited.length} {t("editor.lines")}</span>
        <button onClick={handleApprove} className="btn-primary text-sm py-2.5 px-5">
          {isBatch ? t("editor.approve_next") : t("editor.approve_generate")}
        </button>
      </div>
    </div>
  );
}
