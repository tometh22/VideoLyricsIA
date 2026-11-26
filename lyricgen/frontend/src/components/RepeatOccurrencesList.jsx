// Las otras apariciones de una línea repetida (estribillo) que el operador
// acaba de corregir: cuáles se tocan si aplica el mismo cambio, con un
// casillero para dejar afuera una. UMG pide "corregir en todas las partes
// donde aparece"; también hay repeticiones que se cantan distinto a
// propósito, y ésas tienen que poder quedar como están.

const clock = (seconds) => {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
};

export function selectedRepeatIds(prompt) {
  const excluded = new Set(prompt?.excludedIds || []);
  return (prompt?.matchIds || []).filter((id) => !excluded.has(id));
}

export function repeatApplyLabel(prompt, t = () => null) {
  const selected = selectedRepeatIds(prompt).length;
  if (selected === (prompt?.matchIds || []).length) {
    return (t("editor.repeat_apply_all") || "Aplicar a todas ({n})").replace("{n}", selected);
  }
  return (t("editor.repeat_apply_selected") || "Aplicar a las {n} marcadas").replace("{n}", selected);
}

export default function RepeatOccurrencesList({ prompt, segments = [], onToggle, t = () => null }) {
  if (!prompt?.matchIds?.length) return null;
  const excluded = new Set(prompt.excludedIds || []);
  const byId = new Map(segments.map((row) => [row._id, row]));
  const rows = prompt.matchIds.map((id) => byId.get(id)).filter(Boolean)
    .sort((a, b) => Number(a.start) - Number(b.start));
  return (
    <ul className="mt-1.5 flex flex-wrap gap-1.5" aria-label={t("editor.repeat_list") || "Repeticiones que se corrigen"}>
      {rows.map((row) => (
        <li key={row._id}>
          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-black/25 px-2 py-1 text-[11px] text-white/80 ring-1 ring-white/10">
            <input
              type="checkbox"
              checked={!excluded.has(row._id)}
              onMouseDown={(event) => event.preventDefault()}
              onChange={() => onToggle?.(row._id)}
              aria-label={(t("editor.repeat_include") || "Corregir también la repetición de {time}").replace("{time}", clock(row.start))}
            />
            <span className="font-mono">{clock(row.start)}</span>
          </label>
        </li>
      ))}
    </ul>
  );
}
