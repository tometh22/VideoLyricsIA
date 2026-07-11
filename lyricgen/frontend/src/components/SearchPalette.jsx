/**
 * SearchPalette — command palette overlay para búsqueda global de jobs.
 *
 * Pattern Linear/Vercel/Notion: ⌘K abre overlay full-width arriba,
 * input + lista de resultados, navegación con flechas, Enter abre el
 * JobDetail.
 *
 * 2026-05-25 — PR-2 Tier 1 redesign Dashboard+Historial.
 *
 * NO trae jobs propios — lee del `history` que ya tiene App component
 * (fetch único, no duplica). Fuzzy match client-side sobre
 * {artist, song_title, filename, job_id}. Con 1k jobs `includes()`
 * naive es <50 ms; >5k pasar a fuse.js (15 min de migración cuando ese
 * momento llegue).
 */
import { useCallback, useEffect, useRef, useState, useMemo } from "react";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

// Mapa compacto status → {label, color, icon symbol}. Espejado del
// HistoryView/StatusBadge pero condensado para una tabla densa de
// resultados (no necesitamos el badge pill, sí un mini icono).
const STATUS_DOT = {
  done:                { color: "bg-accent",   label: "Listo" },
  pending_review:      { color: "bg-amber-400",label: "Pendiente" },
  processing:          { color: "bg-brand animate-pulse", label: "Procesando" },
  queued:              { color: "bg-gray-500", label: "En cola" },
  editing:             { color: "bg-brand animate-pulse", label: "Re-render" },
  transcribed:         { color: "bg-gray-400", label: "Sin generar" },
  transcribed_pending: { color: "bg-gray-400", label: "Sin generar" },
  transcribing:        { color: "bg-brand animate-pulse", label: "Transcribiendo" },
  transcribing_queued: { color: "bg-gray-500", label: "En cola" },
  awaiting_upload:     { color: "bg-gray-500", label: "Subiendo" },
  transcription_failed:{ color: "bg-red-400",  label: "Error transcr" },
  error:               { color: "bg-red-400",  label: "Error" },
  validation_failed:   { color: "bg-red-400",  label: "Validación falló" },
  rejected:            { color: "bg-red-400",  label: "Rechazado" },
  bg_preview_queued:   { color: "bg-gray-500", label: "Pre-render BG" },
  bg_preview_failed:   { color: "bg-red-400",  label: "Pre-render falló" },
};

function parseName(filename) {
  const base = (filename || "").replace(/\.(mp3|wav)$/i, "");
  if (base.includes(" - ")) {
    const [a, ...rest] = base.split(" - ");
    return { artist: a.trim(), song: rest.join(" - ").trim() };
  }
  return { artist: "", song: base };
}

export default function SearchPalette({ isOpen, onClose, jobs, onSelectJob }) {
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  const selectJob = useCallback((job) => {
    if (!job?.job_id) return;
    const accepted = onSelectJob(job.job_id, job.status);
    if (accepted !== false) onClose();
  }, [onClose, onSelectJob]);

  // Reset al abrir/cerrar — no preservamos query entre opens (siempre
  // se entra al palette "fresh" como en Linear)
  useEffect(() => {
    if (isOpen) {
      setQuery("");
      setActiveIdx(0);
      // Pequeño delay para que el ref esté montado tras la transición
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  // Resultados fuzzy. Score simple: match en artist+song pesa más que
  // match en job_id o filename. Top 10 visibles. Cuando query está
  // vacío mostramos los 8 más recientes (acceso rápido sin escribir).
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return (jobs || []).slice(0, 8);
    return (jobs || [])
      .map((job) => {
        const { artist, song } = parseName(job.filename);
        const jobArtist = (job.artist || artist || "").toLowerCase();
        const jobSong = (job.song_title || song || "").toLowerCase();
        const jobFile = (job.filename || "").toLowerCase();
        const jobId = (job.job_id || "").toLowerCase();
        let score = 0;
        if (jobArtist.includes(q)) score += 10;
        if (jobSong.includes(q)) score += 10;
        if (jobFile.includes(q)) score += 5;
        if (jobId.includes(q)) score += 3;
        // Bonus: match al inicio de palabra (más relevante)
        if (jobArtist.startsWith(q)) score += 5;
        if (jobSong.startsWith(q)) score += 5;
        return { job, score };
      })
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 10)
      .map((r) => r.job);
  }, [jobs, query]);

  // Reset selección a 0 cuando cambia la query (no querés que el cursor
  // quede en idx 5 si la nueva query trae solo 2 resultados)
  useEffect(() => { setActiveIdx(0); }, [query]);

  // Teclado: Esc cierra, Enter abre el seleccionado, ↑↓ navegan.
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIdx((i) => Math.min(i + 1, Math.max(results.length - 1, 0)));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const sel = results[activeIdx];
        if (sel && sel.job_id) {
          selectJob(sel);
        }
        return;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isOpen, results, activeIdx, onClose, selectJob]);

  // Auto-scroll del item activo cuando navega con teclado
  useEffect(() => {
    if (!isOpen || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-result-idx="${activeIdx}"]`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeIdx, isOpen]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center pt-20 px-4 bg-black/50 backdrop-blur-sm animate-fade-in"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-2xl bg-surface-2 ring-1 ring-white/10 shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Buscar videos y acciones"
      >
        {/* Input */}
        <div className="flex items-center gap-3 px-5 py-4 border-b border-white/[0.06]">
          <svg className="w-4 h-4 text-gray-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar artista, canción o ID…"
            className="flex-1 bg-transparent outline-none text-[15px] text-white placeholder:text-gray-500"
            spellCheck={false}
            autoComplete="off"
          />
          <kbd className="hidden sm:inline-flex items-center gap-0.5 px-1.5 h-5 rounded text-[10px] font-mono text-gray-400 bg-white/[0.06] ring-1 ring-white/10">
            ESC
          </kbd>
        </div>

        {/* Resultados */}
        <div ref={listRef} className="max-h-[60vh] overflow-y-auto py-1">
          {results.length === 0 ? (
            <div className="px-5 py-10 text-center">
              <p className="text-sm text-gray-400">
                {query.trim()
                  ? <>No encontramos videos para <span className="font-mono text-white">"{query.trim()}"</span></>
                  : "Subí tu primer audio para arrancar"}
              </p>
            </div>
          ) : (
            <>
              {!query.trim() && (
                <div className="px-5 pt-2 pb-1">
                  <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500 font-semibold">
                    Recientes
                  </p>
                </div>
              )}
              {results.map((job, i) => {
                const { artist, song } = parseName(job.filename);
                const displayArtist = job.artist || artist || "—";
                const displaySong = job.song_title || song || "(sin nombre)";
                const status = STATUS_DOT[job.status] || { color: "bg-gray-500", label: job.status };
                const isActive = i === activeIdx;
                return (
                  <button
                    key={job.job_id}
                    data-result-idx={i}
                    onClick={() => selectJob(job)}
                    onMouseEnter={() => setActiveIdx(i)}
                    className={`w-full flex items-center gap-3 px-5 py-2.5 text-left transition-colors
                      ${isActive ? "bg-white/[0.06]" : "hover:bg-white/[0.03]"}`}
                  >
                    <span className={`w-2 h-2 rounded-full shrink-0 ${status.color}`} />
                    <div className="flex-1 min-w-0 flex items-baseline gap-2">
                      <span className="text-[13px] font-medium text-white truncate">
                        {displayArtist}
                      </span>
                      <span className="text-gray-600">·</span>
                      <span className="text-[13px] text-gray-300 truncate">
                        {displaySong}
                      </span>
                    </div>
                    <span className="text-[10px] text-gray-500 font-mono tabular-nums shrink-0">
                      {timeAgo(job.created_at)}
                    </span>
                    <span className="text-[10px] text-gray-500 shrink-0 w-[80px] text-right">
                      {status.label}
                    </span>
                  </button>
                );
              })}
            </>
          )}
        </div>

        {/* Footer: hotkeys hint */}
        <div className="flex items-center justify-between px-5 py-2.5 border-t border-white/[0.06] bg-surface-3/30">
          <div className="flex items-center gap-3 text-[10px] text-gray-500">
            <span className="flex items-center gap-1">
              <kbd className="px-1.5 h-4 inline-flex items-center rounded font-mono bg-white/[0.06] ring-1 ring-white/10">↑↓</kbd>
              navegar
            </span>
            <span className="flex items-center gap-1">
              <kbd className="px-1.5 h-4 inline-flex items-center rounded font-mono bg-white/[0.06] ring-1 ring-white/10">↵</kbd>
              abrir
            </span>
            <span className="flex items-center gap-1">
              <kbd className="px-1.5 h-4 inline-flex items-center rounded font-mono bg-white/[0.06] ring-1 ring-white/10">esc</kbd>
              cerrar
            </span>
          </div>
          <span className="text-[10px] text-gray-500 font-mono tabular-nums">
            {results.length} {results.length === 1 ? "resultado" : "resultados"}
          </span>
        </div>
      </div>
    </div>
  );
}
