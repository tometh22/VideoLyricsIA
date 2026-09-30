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
  ["J / K", "siguiente / anterior"], ["M", "corregir a mano"], ["Z", "deshacer"], ["?", "ayuda"],
];

// Ícono y color por tipo de punto: se reconoce de un vistazo.
const GROUP_STYLE = {
  text: { icon: "≠", tone: "text-amber-200", chip: "bg-amber-300/15 text-amber-100" },
  style: { icon: "Aa", tone: "text-sky-200", chip: "bg-sky-300/15 text-sky-100" },
  layout: { icon: "↵", tone: "text-violet-200", chip: "bg-violet-300/15 text-violet-100" },
  chorus: { icon: "↻", tone: "text-teal-200", chip: "bg-teal-300/15 text-teal-100" },
  timing: { icon: "⏱", tone: "text-rose-200", chip: "bg-rose-300/15 text-rose-100" },
};
const MISSING_STYLE = { icon: "+", tone: "text-emerald-200", chip: "bg-emerald-300/15 text-emerald-100" };

function styleFor(item) {
  if (item.kind === "missing") return MISSING_STYLE;
  return GROUP_STYLE[item.group] || GROUP_STYLE.text;
}

function formatTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function Diff({ occurrence }) {
  if (!occurrence.before) {
    return (
      <p className="text-base leading-relaxed text-white">
        <span className="mr-2 text-xs font-medium text-emerald-200/80">Línea nueva</span>
        <ins className="rounded bg-emerald-400/20 px-1 text-emerald-50 no-underline">{occurrence.after}</ins>
      </p>
    );
  }
  if (occurrence.before === occurrence.after) {
    return <p className="text-base leading-relaxed text-white">{occurrence.after}</p>;
  }
  return (
    <p className="text-base leading-relaxed text-white" title={`Ahora dice: ${occurrence.before}`}>
      {diffTokens(occurrence.before, occurrence.after).map((part, i) => (
        <span key={`${part.op}-${i}`}>
          {i > 0 && " "}
          {part.op === "ins" && <ins className="rounded bg-emerald-400/20 px-1 text-emerald-50 no-underline">{part.text}</ins>}
          {part.op === "del" && <del className="rounded px-0.5 text-rose-300/80 decoration-rose-300/70">{part.text}</del>}
          {part.op === "same" && part.text}
          {part.op === "punct" && punctuationDiff(part.before, part.text).map((bit, k) => (
            bit.op === "del"
              ? <del key={k} className="rounded bg-rose-400/20 px-0.5 text-rose-200 decoration-rose-200">{bit.text}</del>
              : bit.op === "ins"
                ? <ins key={k} className="rounded bg-emerald-400/20 text-emerald-50 no-underline">{bit.text}</ins>
                : <span key={k}>{bit.text}</span>
          ))}
        </span>
      ))}
    </p>
  );
}

function Kbd({ children }) {
  return (
    <kbd className="ml-1.5 rounded border border-black/20 bg-black/15 px-1 font-sans text-[10px] font-semibold opacity-70">
      {children}
    </kbd>
  );
}

function ActiveCard({ item, failed, playing, onPlay, onApply, onDismiss, onEdit }) {
  const occurrence = item.occurrences[0];
  const more = item.occurrences.length - 1;
  const look = styleFor(item);
  return (
    <div
      data-testid="lyric-review-active"
      className={`rounded-xl px-3 py-3 ring-1 ${failed ? "bg-rose-400/[0.08] ring-rose-300/50" : "bg-white/[0.06] ring-amber-300/40"}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => onPlay(item)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-white/10 px-2.5 py-1 text-xs font-semibold text-white hover:bg-white/15"
          aria-label={`${playing ? "Detener" : "Escuchar"} ${formatTime(item.start)}`}>
          <span aria-hidden="true">{playing ? "■" : "▶"}</span>{formatTime(item.start)}
        </button>
        <span className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-semibold ${look.chip}`}>
          <span aria-hidden="true">{look.icon}</span>{item.title}
        </span>
        {!item.required && <span className="text-xs text-white/55">Sugerencia</span>}
        {more > 0 && <span className="text-xs text-white/65">+{more} {more === 1 ? "repetición" : "repeticiones"}, se corrigen juntas</span>}
      </div>
      <div className="mt-2">
        <Diff occurrence={occurrence} />
      </div>
      {item.why && <p className="mt-1 text-xs text-white/65">{item.why}</p>}
      {failed && (
        <p role="alert" className="mt-2 text-xs text-rose-100">
          No se pudo aplicar: la línea cambió. Tocá <strong>M</strong> para corregirla a mano.
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => onApply(item)}
          className="inline-flex items-center rounded-lg bg-amber-300 px-3 py-1.5 text-sm font-semibold text-black hover:bg-amber-200">
          {item.action || "Corregir"}<Kbd>Enter</Kbd>
        </button>
        <button type="button" onClick={() => onDismiss(item)}
          className="inline-flex items-center rounded-lg bg-white/10 px-3 py-1.5 text-sm font-medium text-white hover:bg-white/15">
          {item.dismiss || "Está bien así"}<Kbd>⌫</Kbd>
        </button>
        {(item.alternatives || []).slice(0, 3).map((alt, k) => (
          <button key={`${alt.label || alt.replace}-${k}`} type="button" onClick={() => onApply(item, alt)}
            className="inline-flex items-center rounded-lg bg-white/[0.07] px-2.5 py-1.5 text-sm text-white/90 ring-1 ring-white/10 hover:bg-white/15"
            title={alt.why || undefined}>
            {alt.label || alt.replace}<Kbd>{k + 1}</Kbd>
          </button>
        ))}
        <button type="button" onClick={() => onEdit(item)}
          className="ml-auto inline-flex items-center rounded-lg px-2 py-1.5 text-xs text-white/70 hover:text-white">
          Corregir a mano<Kbd>M</Kbd>
        </button>
      </div>
    </div>
  );
}

function QueueRow({ item, onSelect }) {
  const look = styleFor(item);
  const occurrence = item.occurrences[0];
  return (
    <li>
      <button type="button" onClick={() => onSelect(item)} data-testid="lyric-review-item"
        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-white/75 hover:bg-white/[0.06]">
        <span className={`w-5 shrink-0 text-center font-semibold ${look.tone}`} aria-hidden="true">{look.icon}</span>
        <span className="w-10 shrink-0 tabular-nums text-white/55">{formatTime(item.start)}</span>
        <span className="shrink-0 font-medium text-white/85">{item.title}</span>
        <span className="min-w-0 flex-1 truncate text-white/55">{occurrence.after || occurrence.before}</span>
        {item.occurrences.length > 1 && <span className="shrink-0 text-white/45">×{item.occurrences.length}</span>}
      </button>
    </li>
  );
}

// Revisión rápida: una tarjeta a la vez, todo con el teclado. Lo obligatorio
// va primero y frena "Aprobar"; las sugerencias no frenan.
const LyricReviewPanel = forwardRef(function LyricReviewPanel({
  review, items, failedIds, decidedCount = 0, status = "", playingId, autoPlay = true,
  onToggleAutoPlay, onPlay, onApply, onDismiss, onEdit, onUndo, canUndo = false,
  onActivate, onPasteOfficial,
}, ref) {
  const required = useMemo(() => items.filter((item) => item.required), [items]);
  const suggested = useMemo(() => items.filter((item) => !item.required), [items]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const [activeId, setActiveId] = useState(null);
  const userMoved = useRef(false);
  const lastAction = useRef(0);

  const visible = useMemo(
    () => (showSuggestions || !required.length ? [...required, ...suggested] : required),
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
    else if ((key === "enter" && !onButton) || key === "a") decide(() => onApply(active));
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

  return (
    <section
      ref={ref}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
      data-testid="lyric-review-panel"
      aria-label="Revisión rápida de la letra"
      className={`mb-4 rounded-2xl px-3 py-3 outline-none ring-1 focus-visible:ring-2 focus:ring-2 sm:px-4 ${allClear ? "bg-emerald-400/[0.05] ring-emerald-300/25 focus:ring-emerald-300/60" : "bg-amber-400/[0.06] ring-amber-300/30 focus:ring-amber-300/70"}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`h-2 w-2 shrink-0 rounded-full ${allClear ? "bg-emerald-300" : "bg-amber-300"}`} aria-hidden="true" />
        <p className={`text-sm font-semibold ${allClear ? "text-emerald-100" : "text-amber-100"}`}>
          Revisión rápida · <span data-testid="lyric-review-heading">{heading}</span>
        </p>
        {decidedCount > 0 && <span className="text-xs text-white/60">{decidedCount} {decidedCount === 1 ? "resuelto" : "resueltos"}</span>}
        <div className="ml-auto flex items-center gap-2 text-xs text-white/65">
          {onToggleAutoPlay && (
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input type="checkbox" checked={autoPlay} onChange={onToggleAutoPlay} className="accent-amber-300" />
              Escuchar solo
            </label>
          )}
          <button type="button" onClick={() => setShowHelp((v) => !v)} aria-expanded={showHelp}
            className="rounded-md px-1.5 py-0.5 hover:bg-white/10" aria-label="Atajos de teclado">?</button>
        </div>
      </div>

      {showHelp && (
        <ul data-testid="lyric-review-help" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-white/70">
          {SHORTCUTS.map(([key, label]) => (
            <li key={key}><kbd className="rounded border border-white/15 px-1 text-[11px]">{key}</kbd> {label}</li>
          ))}
        </ul>
      )}

      {risk.level === "high" && reasons.length > 0 && (
        <p data-testid="lyric-review-risk" className="mt-2 rounded-lg bg-rose-400/10 px-3 py-2 text-xs text-rose-100 ring-1 ring-rose-300/25">
          Canción difícil: {reasons.join(". ")}. Escuchala entera o pedí una segunda revisión.
        </p>
      )}

      {active && (
        <div className="mt-3">
          <ActiveCard item={active} failed={failedIds?.has(active.id)} playing={playingId === active.id}
            onPlay={onPlay} onApply={(item, alt) => decide(() => onApply(item, alt))}
            onDismiss={(item) => decide(() => onDismiss(item))} onEdit={onEdit} />
        </div>
      )}

      {queue.length > 0 && (
        <ul className="mt-2 max-h-40 space-y-0.5 overflow-y-auto" aria-label="Siguientes puntos">
          {queue.map((item) => <QueueRow key={item.id} item={item} onSelect={select} />)}
        </ul>
      )}

      <p className="sr-only" aria-live="polite">{status}</p>
      {status && <p data-testid="lyric-review-status" className="mt-2 text-xs text-white/75">{status}</p>}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-white/60">
        {canUndo && (
          <button type="button" onClick={() => decide(onUndo)} className="font-medium text-white/80 hover:text-white">
            Deshacer <Kbd>Z</Kbd>
          </button>
        )}
        {suggested.length > 0 && required.length > 0 && (
          <button type="button" onClick={() => setShowSuggestions((v) => !v)} className="hover:text-white">
            {showSuggestions ? "Ocultar sugerencias" : `Ver ${suggested.length} ${suggested.length === 1 ? "sugerencia" : "sugerencias"} (no bloquean)`}
          </button>
        )}
        <span>Comparado con: {compared.length ? compared.join(" · ") : "sin fuentes de control"}</span>
        {onPasteOfficial && (
          <button type="button" onClick={() => setPasting((v) => !v)} className="hover:text-white">
            {sources.official ? "Cambiar letra oficial" : "Comparar con letra oficial"}
          </button>
        )}
        <button type="button" onClick={() => setShowGuide((v) => !v)} className="hover:text-white">Guía UMG</button>
      </div>

      {pasting && (
        <div className="mt-2 space-y-2">
          <textarea
            value={pasted}
            onChange={(event) => setPasted(event.target.value)}
            rows={6}
            placeholder="Pegá la letra oficial (Google, planilla de UMG). Sólo se usa para comparar: no cambia tu letra ni los tiempos."
            className="w-full rounded-lg bg-black/30 p-2 text-sm text-white ring-1 ring-white/10 focus:outline-none focus:ring-amber-300/50"
            aria-label="Letra oficial"
          />
          <div className="flex gap-2">
            <button type="button" disabled={!pasted.trim()}
              onClick={async () => { if (await onPasteOfficial(pasted)) { setPasting(false); setPasted(""); } }}
              className="rounded-lg bg-amber-300 px-3 py-1.5 text-sm font-semibold text-black disabled:opacity-40">
              Comparar con esta letra
            </button>
            <button type="button" onClick={() => setPasting(false)} className="rounded-lg bg-white/10 px-3 py-1.5 text-sm text-white">
              Cancelar
            </button>
          </div>
        </div>
      )}

      {showGuide && (
        <ul data-testid="lyric-review-guide" className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-white/70">
          {GUIDE.map((rule) => <li key={rule}>{rule}</li>)}
        </ul>
      )}
    </section>
  );
});

export default LyricReviewPanel;
