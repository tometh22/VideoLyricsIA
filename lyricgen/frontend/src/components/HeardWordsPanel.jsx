import { forwardRef } from "react";
import { previewHeardWordsAlert } from "../lib/heardWords";

const SOURCE_LABELS = {
  both: "Lo oyeron la máquina y el testigo",
  witness: "Lo oyó el testigo y es letra que se repite en la canción",
  machine: "Lo transcribió la máquina",
};

function formatTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function Highlighted({ text, words }) {
  const index = text.toLowerCase().indexOf(String(words || "").toLowerCase());
  if (!words || index === -1) return text;
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded bg-amber-300/25 px-0.5 text-amber-50">{text.slice(index, index + words.length)}</mark>
      {text.slice(index + words.length)}
    </>
  );
}

// Palabras que se escuchan en el audio y no están en la letra. Cada una se
// resuelve con un click (o una tecla) antes de aprobar: A agrega, N marca
// "no se canta", E la hace escuchar.
const HeardWordsPanel = forwardRef(function HeardWordsPanel(
  { alerts, segments, playingId, onPlay, onAdd, onDismiss }, ref,
) {
  if (!alerts?.length) return null;
  const first = alerts[0];
  const handleKeyDown = (event) => {
    if (event.target !== event.currentTarget || event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key === "a") onAdd(first);
    else if (key === "n") onDismiss(first);
    else if (key === "e") onPlay(first);
    else return;
    event.preventDefault();
  };
  return (
    <section
      ref={ref}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
      data-testid="heard-words-panel"
      aria-label="Palabras que se escuchan y no están en la letra"
      className="mb-4 rounded-2xl bg-amber-400/[0.07] px-4 py-3 ring-1 ring-amber-300/30 outline-none focus:ring-2 focus:ring-amber-300/70"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-xs font-semibold text-amber-100">
          Se escucha y no está en la letra · {alerts.length} por decidir
        </p>
        <p className="text-[11px] text-amber-100/60">
          A agregar · N no se canta · E escuchar
        </p>
      </div>
      <ul className="mt-2 space-y-2">
        {alerts.map((alert) => {
          const preview = previewHeardWordsAlert(segments, alert);
          return (
            <li key={alert.id} data-testid="heard-words-item"
              className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl bg-black/20 px-3 py-2">
              <button type="button" onClick={() => onPlay(alert)}
                className="shrink-0 rounded-lg bg-white/10 px-2 py-1 text-xs font-medium text-white hover:bg-white/15"
                aria-label={`Escuchar ${formatTime(alert.start)}`}>
                {playingId === alert.id ? "■" : "▶"} {formatTime(alert.start)}
              </button>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-white/90" title={preview.after}>
                  {preview.kind === "new_line" && <span className="mr-1 text-xs text-amber-100/70">Línea nueva:</span>}
                  <Highlighted text={preview.after} words={preview.kind === "new_line" ? preview.after : alert.text} />
                </p>
                <p className="text-[11px] text-white/45">{SOURCE_LABELS[alert.sources] || SOURCE_LABELS.machine}</p>
              </div>
              <div className="flex shrink-0 gap-2">
                <button type="button" onClick={() => onAdd(alert)}
                  className="rounded-lg bg-amber-300 px-3 py-1 text-xs font-semibold text-black hover:bg-amber-200">
                  Agregar
                </button>
                <button type="button" onClick={() => onDismiss(alert)}
                  className="rounded-lg bg-white/10 px-3 py-1 text-xs font-medium text-white hover:bg-white/15">
                  No se canta
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
});

export default HeardWordsPanel;
