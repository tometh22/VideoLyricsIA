import React from "react";

// Sugerencias de un clic de la segmentación de pantalla. Nada se aplica solo:
// cada corte o unión necesita el clic del revisor, y "Descartar" la oculta.
function clock(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

export default function DisplaySegmentationSuggestions({
  items, onAccept, onDismiss, onAcceptAll, onDismissAll, onSeek, disabled = false, t,
}) {
  if (!Array.isArray(items) || items.length === 0) return null;
  const label = (key, fallback) => t?.(`editor.display_suggest_${key}`) || fallback;
  const button = "rounded-lg px-2 py-1 text-[11px] font-medium disabled:opacity-40";
  return (
    <section data-testid="display-seg-suggestions" aria-label={label("title", "Sugerencias de corte y unión")}
      className="mb-4 rounded-xl border border-sky-400/20 bg-sky-400/5 px-3 py-3 text-xs text-gray-200">
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-semibold">
          {label("title", "Sugerencias de corte y unión")} · {items.length}
        </p>
        <span className="flex-1" />
        <button type="button" disabled={disabled} onClick={onAcceptAll}
          className={`${button} bg-sky-500/20 text-sky-100 hover:bg-sky-500/30`}>
          {label("accept_all", "Aplicar todas")}
        </button>
        <button type="button" disabled={disabled} onClick={onDismissAll}
          className={`${button} bg-white/5 hover:bg-white/10`}>
          {label("dismiss_all", "Descartar todas")}
        </button>
      </div>
      <p className="mt-1 text-gray-400">
        {label("hint", "Propuestas a partir de las pausas cantadas. No se aplica nada hasta que elijas.")}
      </p>
      <ul className="mt-2 max-h-56 space-y-1.5 overflow-auto">
        {items.map((item) => (
          <li key={item.id} data-testid={`display-seg-suggestion-${item.id}`}
            className="flex flex-wrap items-center gap-2 rounded-lg bg-white/[0.03] px-2 py-1.5">
            <button type="button" onClick={() => onSeek?.(Math.max(0, Number(item.start) - 0.5))}
              className="tabular-nums text-gray-400 hover:text-white" aria-label={`Escuchar ${clock(item.start)}`}>
              {clock(item.start)}
            </button>
            <span className="min-w-0 flex-1">
              {item.kind === "split"
                ? item.parts.map((part, index) => (
                  <React.Fragment key={index}>
                    {index > 0 && <span aria-hidden="true" className="mx-1 text-sky-300">✂</span>}
                    <span>{part.text}</span>
                  </React.Fragment>
                ))
                : <>
                  <span>{item.text}</span>
                  <span aria-hidden="true" className="mx-1 text-sky-300">+</span>
                  <span>{item.next_text}</span>
                </>}
            </span>
            <button type="button" disabled={disabled} onClick={() => onAccept(item)}
              aria-label={`${item.kind === "split" ? label("split", "Cortar acá") : label("merge", "Unir con la siguiente")}: ${item.text}`}
              className={`${button} bg-sky-500/20 text-sky-100 hover:bg-sky-500/30`}>
              {item.kind === "split" ? label("split", "Cortar acá") : label("merge", "Unir con la siguiente")}
            </button>
            <button type="button" disabled={disabled} onClick={() => onDismiss(item)}
              aria-label={`${label("dismiss", "Descartar")}: ${item.text}`}
              className={`${button} bg-white/5 hover:bg-white/10`}>
              {label("dismiss", "Descartar")}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
