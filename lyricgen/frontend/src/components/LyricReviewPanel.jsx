import { forwardRef, useEffect, useMemo, useState } from "react";
import { changedWords } from "../lib/lyricReview";

const GUIDE = [
  "Frase completa en una sola pantalla; nunca una palabra sola.",
  "¡ ! para exclamaciones; ¿ ? sólo si de verdad es una pregunta.",
  "Sin punto final en las líneas.",
  "Respetá los modismos: pa', na', querís, de onde.",
  "Ad-libs cantados sí (\"Y-yah-yah\"); gritos y aplausos no.",
  "El título se escribe igual en cada repetición.",
  "Si un coro se corrige, se corrige en todas sus repeticiones.",
];

function formatTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

// Palabras seguidas que cambian van en un solo resaltado ("dormite ya").
function runs(words) {
  const out = [];
  for (const { word, changed } of words) {
    const last = out[out.length - 1];
    if (last && last.changed === changed) last.text += ` ${word}`;
    else out.push({ text: word, changed });
  }
  return out;
}

function Preview({ occurrence }) {
  const unchanged = occurrence.before === occurrence.after;
  return (
    <p className="text-sm leading-snug text-white/90" title={occurrence.after}>
      {!occurrence.before && <span className="mr-1 text-xs text-amber-100/70">Línea nueva:</span>}
      {unchanged ? occurrence.after : runs(changedWords(occurrence.before, occurrence.after)).map((part, i) => (
        <span key={`${part.text}-${i}`}>
          {i > 0 && " "}
          {part.changed ? <mark className="rounded bg-amber-300/25 px-0.5 text-amber-50">{part.text}</mark> : part.text}
        </span>
      ))}
    </p>
  );
}

function Item({ item, active, playing, onPlay, onApply, onDismiss, onFocus }) {
  const occurrence = item.occurrences[0];
  const more = item.occurrences.length - 1;
  return (
    <li
      data-testid="lyric-review-item"
      data-active={active ? "true" : "false"}
      onMouseEnter={onFocus}
      className={`rounded-xl px-3 py-2 ring-1 transition ${active ? "bg-white/[0.07] ring-amber-300/50" : "bg-black/20 ring-transparent"}`}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <button type="button" onClick={() => onPlay(item)}
          className="shrink-0 rounded-lg bg-white/10 px-2 py-1 text-xs font-medium text-white hover:bg-white/15"
          aria-label={`Escuchar ${formatTime(item.start)}`}>
          {playing ? "■" : "▶"} {formatTime(item.start)}
        </button>
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-200/80">
            {item.title}
            {more > 0 && <span className="ml-2 normal-case tracking-normal text-white/50">+{more} {more === 1 ? "repetición" : "repeticiones"}, se corrigen juntas</span>}
          </p>
          <Preview occurrence={occurrence} />
          {occurrence.before && occurrence.before !== occurrence.after && (
            <p className="mt-0.5 truncate text-[11px] text-white/40">Ahora dice: {occurrence.before}</p>
          )}
          {item.why && <p className="text-[11px] text-white/45">{item.why}</p>}
          {item.alternatives?.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-white/50">
              <span>Otra opción:</span>
              {item.alternatives.map((alt) => (
                <button key={alt.replace} type="button" onClick={() => onApply(item, alt)}
                  className="rounded-md bg-white/10 px-2 py-0.5 text-white/80 hover:bg-white/15"
                  title={alt.why}>
                  {alt.replace}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <button type="button" onClick={() => onApply(item)}
            className="rounded-lg bg-amber-300 px-3 py-1 text-xs font-semibold text-black hover:bg-amber-200">
            {item.action || "Corregir"}
          </button>
          <button type="button" onClick={() => onDismiss(item)}
            className="rounded-lg bg-white/10 px-3 py-1 text-xs font-medium text-white hover:bg-white/15">
            {item.dismiss || "Está bien así"}
          </button>
        </div>
      </div>
    </li>
  );
}

// Revisión rápida: lo que falta, lo que se escucha distinto y el estilo UMG.
// Lo obligatorio va arriba y bloquea "Aprobar"; las sugerencias no bloquean.
// Teclado con el panel enfocado: A aplica, N descarta, E escucha, J/K mueven.
const LyricReviewPanel = forwardRef(function LyricReviewPanel({
  review, items, playingId, onPlay, onApply, onDismiss, onPasteOfficial,
}, ref) {
  const required = useMemo(() => items.filter((item) => item.required), [items]);
  const suggested = useMemo(() => items.filter((item) => !item.required), [items]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const visible = useMemo(
    () => (showSuggestions || !required.length ? [...required, ...suggested] : required),
    [required, suggested, showSuggestions],
  );
  const [activeId, setActiveId] = useState(null);
  const active = visible.find((item) => item.id === activeId) || visible[0] || null;
  useEffect(() => {
    if (activeId && !visible.some((item) => item.id === activeId)) setActiveId(visible[0]?.id || null);
  }, [activeId, visible]);

  if (!review || review.mode === "off") return null;
  const sources = review.sources || {};
  const risk = review.risk || {};
  const move = (delta) => {
    if (!visible.length) return;
    const index = Math.max(0, visible.findIndex((item) => item.id === active?.id));
    setActiveId(visible[(index + delta + visible.length) % visible.length].id);
  };
  const handleKeyDown = (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.tagName === "TEXTAREA" || event.target.tagName === "INPUT") return;
    const key = event.key.toLowerCase();
    if (!active || (key === "enter" && event.target.tagName === "BUTTON")) return;
    if (key === "a" || key === "enter") onApply(active);
    else if (key === "n") onDismiss(active);
    else if (key === "e") onPlay(active);
    else if (key === "j" || key === "arrowdown") move(1);
    else if (key === "k" || key === "arrowup") move(-1);
    else return;
    event.preventDefault();
  };
  const compared = [
    sources.official && `letra oficial${sources.official_origin === "operator" ? " (pegada)" : sources.official_origin === "sheet" ? " (planilla)" : ""}`,
    sources.gemini && "Gemini",
    sources.witness && "testigo",
  ].filter(Boolean);
  const allClear = required.length === 0;

  return (
    <section
      ref={ref}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
      data-testid="lyric-review-panel"
      aria-label="Revisión rápida de la letra"
      className={`mb-4 rounded-2xl px-4 py-3 outline-none ring-1 focus:ring-2 ${allClear ? "bg-emerald-400/[0.06] ring-emerald-300/25 focus:ring-emerald-300/60" : "bg-amber-400/[0.07] ring-amber-300/30 focus:ring-amber-300/70"}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className={`text-sm font-semibold ${allClear ? "text-emerald-100" : "text-amber-100"}`}>
          {allClear ? "Revisión rápida: todo listo para aprobar" : `Revisión rápida · ${required.length} para decidir antes de aprobar`}
        </p>
        {visible.length > 0 && (
          <p className="text-[11px] text-white/50">A aplicar · N está bien · E escuchar · J/K mover</p>
        )}
      </div>

      {risk.level === "high" && (
        <p data-testid="lyric-review-risk" className="mt-2 rounded-lg bg-rose-400/10 px-3 py-2 text-xs text-rose-100 ring-1 ring-rose-300/25">
          Canción difícil: {risk.reasons.join(". ")}. Escuchala entera o pedí una segunda revisión.
        </p>
      )}

      {visible.length > 0 && (
        <ul className="mt-2 space-y-2">
          {visible.map((item) => (
            <Item key={item.id} item={item} active={item.id === active?.id}
              playing={playingId === item.id} onFocus={() => setActiveId(item.id)}
              onPlay={onPlay} onApply={onApply} onDismiss={onDismiss} />
          ))}
        </ul>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-white/50">
        {suggested.length > 0 && required.length > 0 && (
          <button type="button" onClick={() => setShowSuggestions((v) => !v)} className="underline-offset-2 hover:underline">
            {showSuggestions ? "Ocultar sugerencias" : `Ver ${suggested.length} ${suggested.length === 1 ? "sugerencia" : "sugerencias"} (no bloquean)`}
          </button>
        )}
        <span>Comparado con: {compared.length ? compared.join(" · ") : "sin fuentes de control"}</span>
        {onPasteOfficial && (
          <button type="button" onClick={() => setPasting((v) => !v)} className="underline-offset-2 hover:underline">
            {sources.official ? "Cambiar letra oficial" : "Pegar letra oficial"}
          </button>
        )}
        <button type="button" onClick={() => setShowGuide((v) => !v)} className="underline-offset-2 hover:underline">
          Guía UMG
        </button>
      </div>

      {pasting && (
        <div className="mt-2 space-y-2">
          <textarea
            value={pasted}
            onChange={(event) => setPasted(event.target.value)}
            rows={6}
            placeholder="Pegá acá la letra oficial (Google, planilla de UMG). Sólo se usa para comparar: no cambia tu letra ni los tiempos."
            className="w-full rounded-lg bg-black/30 p-2 text-xs text-white ring-1 ring-white/10 focus:outline-none focus:ring-amber-300/50"
            aria-label="Letra oficial"
          />
          <div className="flex gap-2">
            <button type="button" disabled={!pasted.trim()}
              onClick={async () => { if (await onPasteOfficial(pasted)) { setPasting(false); setPasted(""); } }}
              className="rounded-lg bg-amber-300 px-3 py-1 text-xs font-semibold text-black disabled:opacity-40">
              Comparar con esta letra
            </button>
            <button type="button" onClick={() => setPasting(false)} className="rounded-lg bg-white/10 px-3 py-1 text-xs text-white">
              Cancelar
            </button>
          </div>
        </div>
      )}

      {showGuide && (
        <ul data-testid="lyric-review-guide" className="mt-2 list-disc space-y-0.5 pl-5 text-[11px] text-white/60">
          {GUIDE.map((rule) => <li key={rule}>{rule}</li>)}
        </ul>
      )}
    </section>
  );
});

export default LyricReviewPanel;
