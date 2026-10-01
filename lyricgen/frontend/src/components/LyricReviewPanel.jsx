import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import { diffTokens, punctuationDiff } from "../lib/lyricReview";

const GUIDE = [
  "Frase completa en una sola pantalla; nunca una palabra sola.",
  "¡ ! para exclamaciones; ¿ ? sólo si de verdad es una pregunta.",
  "Sin punto final en las líneas.",
  "Respetá los modismos: pa', na', querís, de onde.",
  "Ad-libs cantados sí (\"Y-yah-yah\"); gritos y aplausos no.",
  "El título se escribe igual en cada repetición.",
  "Si un coro se corrige, se corrige en todas sus repeticiones.",
];

const SHORTCUTS = [
  ["Enter", "aplicar"], ["⌫", "está bien así"], ["1–3", "otra opción"], ["E", "escuchar"],
  ["J / K", "siguiente / anterior"], ["M", "editar a mano"], ["Z", "deshacer"], ["?", "ayuda"],
];

function formatTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

// Un único lenguaje visual: verde lo que entra, tachado lo que sale. El resto
// del panel es neutro para que el cambio sea lo único que salta a la vista.
const INS = "rounded bg-emerald-400/15 px-1 text-emerald-100 no-underline";
const DEL = "rounded px-0.5 text-rose-300/75 decoration-rose-300/60";

function Diff({ occurrence }) {
  const base = "text-[17px] leading-relaxed text-white";
  if (!occurrence.before) {
    return (
      <p className={base}>
        <span className="mr-2 align-middle text-[11px] font-medium text-white/50">Línea nueva</span>
        <ins className={INS}>{occurrence.after}</ins>
      </p>
    );
  }
  if (occurrence.before === occurrence.after) return <p className={base}>{occurrence.after}</p>;
  return (
    <p className={base} title={`Ahora dice: ${occurrence.before}`}>
      {diffTokens(occurrence.before, occurrence.after).map((part, i) => (
        <span key={`${part.op}-${i}`}>
          {i > 0 && " "}
          {part.op === "ins" && <ins className={INS}>{part.text}</ins>}
          {part.op === "del" && <del className={DEL}>{part.text}</del>}
          {part.op === "same" && part.text}
          {part.op === "punct" && punctuationDiff(part.before, part.text).map((bit, k) => (
            bit.op === "del"
              ? <del key={k} className="rounded bg-rose-400/15 px-0.5 text-rose-200 decoration-rose-200">{bit.text}</del>
              : bit.op === "ins"
                ? <ins key={k} className="rounded bg-emerald-400/15 text-emerald-100 no-underline">{bit.text}</ins>
                : <span key={k}>{bit.text}</span>
          ))}
        </span>
      ))}
    </p>
  );
}

// Un solo oído automático: la línea tal cual, con las palabras dudosas
// subrayadas. Su versión va en el "por qué", nunca como arreglo.
function Doubt({ occurrence }) {
  const base = "text-[17px] leading-relaxed text-white";
  if (!occurrence.before) return <p className={base}>{occurrence.after}</p>;
  return (
    <p className={base}>
      {diffTokens(occurrence.before, occurrence.after).filter((part) => part.op !== "ins").map((part, i) => (
        <span key={`${part.op}-${i}`}>
          {i > 0 && " "}
          {part.op === "same"
            ? part.text
            : <span className="underline decoration-amber-200/70 decoration-dotted decoration-2 underline-offset-4">{part.before || part.text}</span>}
        </span>
      ))}
    </p>
  );
}

function Kbd({ children, dark = false }) {
  return (
    <kbd className={`ml-2 hidden rounded px-1 font-sans text-[10px] font-semibold sm:inline ${dark
      ? "bg-black/20 text-white/80" : "bg-white/[0.08] text-ink-secondary"}`}>
      {children}
    </kbd>
  );
}

function Icon({ path, className = "h-3.5 w-3.5" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true">
      <path d={path} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
const PLAY = "M7 5v14l11-7z";
const STOP = "M7 7h10v10H7z";
const CHECK = "m5 12 5 5 9-10";
const UNDO = "M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3";

const ghost = "inline-flex h-10 items-center rounded-button px-3 text-sm text-white ring-1 ring-white/10 transition-colors hover:bg-white/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light sm:h-9";
const link = "rounded px-1 text-ink-secondary transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light";

function ActiveCard({ item, failed, playing, onPlay, onApply, onDismiss, onEdit }) {
  const occurrence = item.occurrences[0];
  const count = item.occurrences.length;
  return (
    <div
      data-testid="lyric-review-active"
      className={`rounded-xl bg-surface-2 p-4 ring-1 ${failed ? "ring-rose-300/40" : "ring-white/[0.07]"}`}
    >
      <div className="flex items-center gap-2 text-xs">
        <span className="font-medium text-white/90">{item.title}</span>
        {!item.required && <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-ink-secondary">Sugerencia</span>}
        {count > 1 && <span className="text-white/50">· {count} lugares, se corrigen juntos</span>}
        <button type="button" onClick={() => onPlay(item)}
          className="ml-auto inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full bg-white/[0.06] px-2.5 font-medium tabular-nums text-white transition-colors hover:bg-white/[0.12] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light"
          aria-label={`${playing ? "Detener" : "Escuchar"} ${formatTime(item.start)}`}>
          <Icon path={playing ? STOP : PLAY} className="h-3 w-3" />{formatTime(item.start)}
        </button>
      </div>
      <div className="mt-2.5">{item.listen ? <Doubt occurrence={occurrence} /> : <Diff occurrence={occurrence} />}</div>
      {item.why && <p className="mt-1 text-xs text-white/50">{item.why}</p>}
      {failed && (
        <p role="alert" className="mt-2 text-xs text-rose-200">
          No se pudo aplicar: la línea cambió. Editala a mano (<strong>M</strong>).
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => (item.listen ? onPlay(item) : onApply(item))}
          className="inline-flex h-10 items-center rounded-button bg-brand px-4 text-sm font-semibold text-white shadow-lg shadow-brand/20 transition-colors hover:bg-brand-light focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 sm:h-9">
          {item.listen && <Icon path={playing ? STOP : PLAY} className="mr-1.5 h-3 w-3" />}
          {item.listen ? (playing ? "Detener" : "Escuchar") : (item.action || "Corregir")}<Kbd dark>Enter</Kbd>
        </button>
        <button type="button" onClick={() => onDismiss(item)} className={ghost}>
          {item.dismiss || "Está bien así"}<Kbd>⌫</Kbd>
        </button>
        {(item.alternatives || []).slice(0, 3).map((alt, k) => (
          <button key={`${alt.label || alt.replace}-${k}`} type="button" onClick={() => onApply(item, alt)}
            className={ghost} title={alt.why || undefined}>
            {alt.label || alt.replace}<Kbd>{k + 1}</Kbd>
          </button>
        ))}
        <button type="button" onClick={() => onEdit(item)} className={`ml-auto text-xs ${link}`}>
          Editar a mano<Kbd>M</Kbd>
        </button>
      </div>
    </div>
  );
}

function QueueRow({ item, onSelect }) {
  const occurrence = item.occurrences[0];
  return (
    <li>
      <button type="button" onClick={() => onSelect(item)} data-testid="lyric-review-item"
        className="flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left text-xs transition-colors hover:bg-white/[0.04] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light">
        <span className="w-9 shrink-0 tabular-nums text-white/50">{formatTime(item.start)}</span>
        <span className="shrink-0 text-white/80">{item.title}</span>
        <span className="min-w-0 flex-1 truncate text-white/50">{item.listen ? occurrence.before : occurrence.after || occurrence.before}</span>
        {item.occurrences.length > 1 && <span className="shrink-0 tabular-nums text-white/50">×{item.occurrences.length}</span>}
        {!item.required && <span className="shrink-0 text-[10px] text-white/50">sugerencia</span>}
      </button>
    </li>
  );
}

function Help({ compared }) {
  const [guide, setGuide] = useState(false);
  return (
    <div data-testid="lyric-review-help" className="mx-4 mt-3 rounded-xl bg-surface-2 p-3 text-xs text-ink-secondary ring-1 ring-white/[0.06]">
      <ul className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
        {SHORTCUTS.map(([key, label]) => (
          <li key={key} className="flex items-center gap-2">
            <kbd className="min-w-[2.25rem] rounded bg-white/[0.08] px-1 text-center font-sans text-[10px] font-semibold text-white/85">{key}</kbd>
            {label}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/[0.06] pt-2.5">
        <span>Comparado con: {compared.length ? compared.join(" · ") : "sin fuentes de control"}</span>
        <button type="button" onClick={() => setGuide((v) => !v)} aria-expanded={guide} className={`ml-auto ${link}`}>
          Guía UMG
        </button>
      </div>
      {guide && (
        <ul data-testid="lyric-review-guide" className="mt-2 list-disc space-y-0.5 pl-4">
          {GUIDE.map((rule) => <li key={rule}>{rule}</li>)}
        </ul>
      )}
    </div>
  );
}

// Revisión rápida: una tarjeta a la vez, todo con el teclado. Lo obligatorio
// va primero y frena "Aprobar"; las sugerencias no frenan.
const LyricReviewPanel = forwardRef(function LyricReviewPanel({
  review, items, failedIds, decidedCount = 0, status = "", playingId, autoPlay = true,
  onToggleAutoPlay, onPlay, onApply, onDismiss, onEdit, onUndo, canUndo = false,
  onActivate, onOpenOfficial,
}, ref) {
  const required = useMemo(() => items.filter((item) => item.required), [items]);
  const suggested = useMemo(() => items.filter((item) => !item.required), [items]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [activeId, setActiveId] = useState(null);
  const userMoved = useRef(false);
  const lastAction = useRef(0);

  const visible = useMemo(
    () => (showSuggestions ? [...required, ...suggested] : required),
    [required, suggested, showSuggestions],
  );
  const active = visible.find((item) => item.id === activeId) || visible[0] || null;

  // Al cambiar el punto activo por una acción del revisor: se muestra su línea
  // en la letra y, si está activado, se escucha solo (flujo "escuchar → Enter").
  useEffect(() => {
    if (!active) {
      onActivate?.(null, { scroll: false });
      return;
    }
    const byUser = userMoved.current;
    userMoved.current = false;
    // La línea del punto activo siempre queda marcada en la letra; se lleva
    // a la vista y se escucha sólo si el revisor se movió.
    onActivate?.(active, { scroll: byUser });
    if (byUser && autoPlay) onPlay(active);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id]);

  if (!review || review.mode === "off") return null;
  const enforce = review.mode === "enforce";
  const sources = review.sources || {};
  const risk = review.risk || {};
  const reasons = Array.isArray(risk.reasons) ? risk.reasons : [];

  const select = (item) => {
    userMoved.current = true;
    setActiveId(item.id);
  };
  const move = (delta) => {
    if (!visible.length) return;
    const index = Math.max(0, visible.findIndex((item) => item.id === active?.id));
    select(visible[(index + delta + visible.length) % visible.length]);
  };
  const decide = (fn) => {
    const now = Date.now();
    if (now - lastAction.current < 250) return; // doble click / tecla trabada
    lastAction.current = now;
    userMoved.current = true;
    fn();
  };
  const handleKeyDown = (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
    const tag = event.target.tagName;
    if (tag === "TEXTAREA" || tag === "INPUT") return;
    const key = event.key.toLowerCase();
    const onButton = tag === "BUTTON";
    let handled = true;
    if (key === "?") setShowHelp((v) => !v);
    else if (key === "z" && canUndo) decide(onUndo);
    else if (!active) handled = false;
    else if ((key === "enter" && !onButton) || key === "a") {
      if (active.listen) onPlay(active);
      else decide(() => onApply(active));
    }
    else if (key === "backspace" || key === "n") decide(() => onDismiss(active));
    else if (["1", "2", "3"].includes(key) && active.alternatives?.[Number(key) - 1]) {
      decide(() => onApply(active, active.alternatives[Number(key) - 1]));
    } else if (key === "e" || key === "r") onPlay(active);
    else if (key === "m") onEdit(active);
    else if (key === "j" || key === "arrowdown") move(1);
    else if (key === "k" || key === "arrowup") move(-1);
    else handled = false;
    if (handled) {
      // El editor tiene sus propios atajos (N = siguiente línea a revisar):
      // la tecla ya la usó el panel.
      event.preventDefault();
      event.stopPropagation();
    }
  };

  const compared = [
    sources.official && `letra oficial${sources.official_origin === "operator" ? " (pegada)" : sources.official_origin === "sheet" ? " (planilla)" : ""}`,
    sources.gemini && "Gemini",
    sources.witness && "testigo",
  ].filter(Boolean);
  const allClear = required.length === 0;
  const queue = visible.filter((item) => item.id !== active?.id);
  const heading = allClear
    ? "Todo listo para aprobar"
    : enforce
      ? `Faltan ${required.length} para aprobar`
      : `${required.length} para revisar`;
  const total = decidedCount + required.length;
  const progress = total > 0 ? Math.round((decidedCount / total) * 100) : 100;

  return (
    <section
      ref={ref}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
      data-testid="lyric-review-panel"
      aria-label="Revisión rápida de la letra"
      className="relative mb-4 overflow-hidden rounded-card bg-surface-1 outline-none ring-1 ring-white/[0.08] transition-shadow focus:ring-brand-light/60"
    >
      {total > 0 && (
        <div className="absolute inset-x-0 top-0 h-0.5 bg-white/[0.04]" aria-hidden="true">
          <div className={`h-full transition-all duration-brand ease-brand ${allClear ? "bg-emerald-300/80" : "bg-brand-light"}`}
            style={{ width: `${progress}%` }} />
        </div>
      )}

      <header className="flex items-center gap-3 px-4 pt-3.5">
        <span className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${allClear
          ? "bg-emerald-400/15 text-emerald-200" : "bg-amber-300/15 text-amber-200"}`} aria-hidden="true">
          {allClear ? <Icon path={CHECK} /> : <span className="text-[11px] font-bold tabular-nums">{required.length}</span>}
        </span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-white">Revisión rápida</h3>
          <p className="text-xs text-ink-secondary">
            <span data-testid="lyric-review-heading">{heading}</span>
            {decidedCount > 0 && <span className="text-white/50"> · {decidedCount} {decidedCount === 1 ? "resuelto" : "resueltos"}</span>}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-1">
          {onToggleAutoPlay && (
            <button type="button" onClick={onToggleAutoPlay} aria-pressed={autoPlay}
              title="Reproduce cada punto al pasar al siguiente"
              className={`inline-flex h-8 items-center gap-1.5 rounded-full px-2.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light ${autoPlay
                ? "bg-brand/15 text-brand-50" : "text-ink-secondary hover:bg-white/[0.06]"}`}>
              <Icon path="M11 5 6 9H3v6h3l5 4V5zM15.5 8.5a5 5 0 0 1 0 7" />
              <span className="hidden sm:inline">Reproducir al avanzar</span>
            </button>
          )}
          <button type="button" onClick={() => setShowHelp((v) => !v)} aria-expanded={showHelp}
            className={`inline-flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light ${showHelp
              ? "bg-white/[0.1] text-white" : "text-ink-secondary hover:bg-white/[0.06]"}`}
            aria-label="Atajos y guía">?</button>
        </div>
      </header>

      {showHelp && <Help compared={compared} />}

      {risk.level === "high" && reasons.length > 0 && (
        <p data-testid="lyric-review-risk" className="mx-4 mt-3 text-xs leading-relaxed text-amber-200/90">
          Canción difícil: {reasons.join(". ")}. Escuchala entera o pedí una segunda revisión.
        </p>
      )}

      {active && (
        <div className="px-3 pt-3">
          <ActiveCard item={active} failed={failedIds?.has(active.id)} playing={playingId === active.id}
            onPlay={onPlay} onApply={(item, alt) => decide(() => onApply(item, alt))}
            onDismiss={(item) => decide(() => onDismiss(item))} onEdit={onEdit} />
        </div>
      )}

      {queue.length > 0 && (
        <ul className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto px-2" aria-label="Siguientes puntos">
          {queue.map((item) => <QueueRow key={item.id} item={item} onSelect={select} />)}
        </ul>
      )}

      <p className="sr-only" aria-live="polite">{status}</p>
      <footer className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/[0.06] px-4 py-2.5 text-xs">
        {status
          ? <p data-testid="lyric-review-status" className="min-w-0 truncate text-ink-secondary">{status}</p>
          : !active && !suggested.length && <p className="text-white/50">Sin puntos para revisar en esta letra.</p>}
        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1">
          {canUndo && (
            <button type="button" onClick={() => decide(onUndo)} className={`inline-flex items-center gap-1 ${link}`}>
              <Icon path={UNDO} className="h-3 w-3" />Deshacer<Kbd>Z</Kbd>
            </button>
          )}
          {suggested.length > 0 && (
            <button type="button" onClick={() => setShowSuggestions((v) => !v)} className={link}>
              {showSuggestions ? "Ocultar sugerencias" : `Ver ${suggested.length} ${suggested.length === 1 ? "sugerencia" : "sugerencias"} (${required.length ? "no bloquean" : "opcional"})`}
            </button>
          )}
          {onOpenOfficial && (
            <button type="button" onClick={onOpenOfficial} className={`inline-flex items-center gap-1 ${link}`}>
              {sources.official && <Icon path={CHECK} className="h-3 w-3 text-emerald-300" />}
              {sources.official ? "Letra oficial" : "Comparar con letra oficial"}
            </button>
          )}
        </div>
      </footer>
    </section>
  );
});

export default LyricReviewPanel;
