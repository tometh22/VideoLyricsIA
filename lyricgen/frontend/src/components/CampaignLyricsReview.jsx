import { useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";

const timestamp = (value) => `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, "0")}`;

/** One selected line owns the text, playback and timing actions. */
export default function CampaignLyricsReview({ segments, focusedId, markedIds, audioAvailable,
  onSelect, onTextChange, onListen, onAdvanced, onDuplicate, onDelete, onAdd, onSplit,
  onUndo, canUndo, onMerge, onMergePrevious, onTextFocus, onTextBlur, rowRefs,
  suggestionsById = {}, onApplySuggestion, propagationPrompt, onApplyPropagation, onDismissPropagation }) {
  const { t } = useI18n();
  const [markedOnly, setMarkedOnly] = useState(false);
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const focusAfterChange = useRef(false);
  const selected = segments.find((line) => line._id === focusedId) || segments[0];
  const marked = (line) => line.review || markedIds.has(line._id);
  const visible = useMemo(() => segments.filter((line) => !markedOnly || line.review || markedIds.has(line._id)), [segments, markedOnly, markedIds]);
  useEffect(() => {
    const row = rowRefs.current[selected?._id];
    const list = listRef.current;
    // Scroll only the list: scrollIntoView also moved the entire editor page.
    if (row && list) {
      if (row.offsetTop < list.scrollTop) list.scrollTop = row.offsetTop;
      else if (row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight)
        list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight;
    }
    if (selected && (focusAfterChange.current || document.activeElement === inputRef.current)) {
      inputRef.current?.focus();
      inputRef.current?.setSelectionRange(0, 0);
      onTextFocus?.(selected);
    }
    focusAfterChange.current = false;
  // The edit baseline is reset only when the selected identity changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?._id, rowRefs]);
  const button = "min-h-10 rounded-xl px-3 py-2 text-sm ring-1 ring-white/15 hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light disabled:opacity-40";
  const navigate = (line) => { if (line) onSelect(line); };
  return <section data-testid="campaign-line-workspace" className="grid min-w-0 gap-4 lg:grid-cols-[minmax(240px,0.8fr)_minmax(0,1.2fr)]">
    <div className="min-w-0 rounded-2xl bg-surface-2 ring-1 ring-white/10">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 p-3">
        <h2 className="text-sm font-semibold">{t("review_workspace.lines") || "Líneas"} ({segments.length})</h2>
        <button className={button} aria-pressed={markedOnly} onClick={() => setMarkedOnly((value) => !value)}>{t("review_workspace.marked_only") || "Solo marcadas"}</button>
      </div>
      <div ref={listRef} className="relative max-h-[55vh] overflow-y-auto p-2 lg:max-h-none lg:overflow-visible" aria-label={t("review_workspace.lines") || "Líneas"}>
        {visible.map((line) => <button type="button" key={line._id} ref={(node) => { rowRefs.current[line._id] = node; }}
          data-testid={`review-line-${segments.indexOf(line) + 1}`} aria-current={selected?._id === line._id ? "true" : undefined}
          onClick={() => navigate(line)} onKeyDown={(event) => {
            if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
            event.preventDefault(); event.stopPropagation();
            const next = visible[visible.indexOf(line) + (event.key === "ArrowDown" ? 1 : -1)];
            if (next) { navigate(next); rowRefs.current[next._id]?.focus(); }
          }}
          className={`mb-1 flex w-full items-start gap-3 rounded-xl px-3 py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light ${selected?._id === line._id ? "bg-brand/20 ring-1 ring-brand/50" : "hover:bg-white/5"}`}>
          <span className="shrink-0 text-xs tabular-nums text-ink-secondary">
            <span className="block">{segments.indexOf(line) + 1}</span>
            <span className="mt-1 block whitespace-nowrap font-mono text-ink-primary">{timestamp(line.start)}</span>
            <span className="block whitespace-nowrap font-mono">{timestamp(line.end)}</span>
          </span>
          <span className="min-w-0 flex-1 break-words text-sm">{line.text || (t("review_workspace.empty_line") || "Línea sin texto")}</span>
          {marked(line) && <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-amber-300" aria-label={t("review_workspace.marked") || "Marcada para revisar"} />}
        </button>)}
        {!visible.length && <p className="p-4 text-sm text-ink-secondary">{t("review_workspace.no_marks") || "No hay líneas marcadas."}</p>}
      </div>
      <div className="border-t border-white/10 p-3"><button className={`${button} w-full`} onClick={() => { focusAfterChange.current = true; onAdd(); }}>{t("editor.add_line") || "Agregar línea"}</button></div>
    </div>
    <div className="order-first min-w-0 self-start rounded-2xl bg-surface-2 p-4 ring-1 ring-white/10 lg:sticky lg:top-4 lg:order-none" data-testid="selected-line-panel">
      {selected ? <>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-semibold">{(t("review_workspace.selected") || "Línea {n}").replace("{n}", segments.indexOf(selected) + 1)}</h2>
          <span className="font-mono text-sm text-ink-secondary">{timestamp(selected.start)} → {timestamp(selected.end)}</span>
        </div>
        {selected.timing_validation?.status === "unvalidated" && <p className="mb-3 text-sm text-amber-200">
          {t("review_workspace.timing_unvalidated") || "Timing no validado · revisá este tramo contra el audio. No se cambiaron sus tiempos."}
        </p>}
        {marked(selected) && <p className="mb-3 text-sm text-amber-200">{t("review_workspace.mark_hint") || "Escuchá esta parte y corregí lo que haga falta. La aprobación es por canción."}</p>}
        <label className="mb-2 block text-sm font-medium" htmlFor="selected-lyric-text">{t("review_workspace.text") || "Letra de esta línea"}</label>
        <input id="selected-lyric-text" ref={inputRef} value={selected.text || ""} onChange={(event) => onTextChange(selected._id, event.target.value)}
          onFocus={() => onTextFocus?.(selected)} onBlur={(event) => onTextBlur?.(selected._id, event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault();
            const caret = event.currentTarget.selectionStart ?? event.currentTarget.value.length;
            if (!event.currentTarget.value.slice(0, caret).trim() || !event.currentTarget.value.slice(caret).trim()) { event.currentTarget.blur(); return; }
            focusAfterChange.current = true;
            onSplit?.(selected._id, caret);
          } else if (event.key === "Backspace" && !event.nativeEvent.isComposing
            && event.currentTarget.value === "" && event.currentTarget.selectionStart === 0 && event.currentTarget.selectionEnd === 0) {
            event.preventDefault(); focusAfterChange.current = true; onMergePrevious?.(selected._id);
          } }}
          className="min-h-12 w-full rounded-xl border border-white/15 bg-surface-1 px-3 py-3 text-base text-white focus:border-brand-light focus:outline-none" />
        <p className="mt-2 text-xs text-ink-secondary">{t("review_workspace.autosave") || "Los cambios se guardan automáticamente. Enter divide la línea donde está el cursor."}</p>
        {suggestionsById[selected._id] && suggestionsById[selected._id] !== selected.text && <button
          type="button" className={`${button} mt-3`} onClick={() => onApplySuggestion?.(selected._id)}>
          {t("review_workspace.apply_suggestion") || "Aplicar sugerencia"}: {suggestionsById[selected._id]}
        </button>}
        {propagationPrompt && <div className="mt-3 rounded-xl bg-brand/10 p-3 text-sm ring-1 ring-brand/30">
          <p className="mb-2 break-words">{propagationPrompt.newText}</p>
          <p>{(t("editor.repeat_prompt") || "Esta línea se repite en otras {n}. ¿Aplicar el cambio a todas?").replace("{n}", propagationPrompt.matchIds.length)}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className={button} onClick={onApplyPropagation}>{(t("editor.repeat_apply_all") || "Aplicar a todas ({n})").replace("{n}", propagationPrompt.matchIds.length)}</button>
            <button type="button" className={button} onClick={onDismissPropagation}>{t("editor.repeat_only_this") || "Solo esta"}</button>
          </div>
        </div>}
        <div className="mt-4 flex flex-wrap gap-2">
          <button className={`${button} bg-brand text-white`} disabled={!audioAvailable} onClick={() => onListen(selected)}>{t("review_workspace.listen") || "Escuchar 2 s antes"}</button>
          <button className={button} onClick={() => onAdvanced(selected)}>{t("review_workspace.timing") || "Ajustar esta línea en la timeline"}</button>
        </div>
        <div className="mt-4 flex flex-wrap gap-2 border-t border-white/10 pt-4">
          <button className={button} onClick={() => { focusAfterChange.current = true; onDuplicate(selected._id); }}>{t("timeline.duplicate_line") || "Duplicar línea"}</button>
          {segments.indexOf(selected) < segments.length - 1 && <button className={button} title={t("review_workspace.merge") || "Unir con la línea siguiente"} onClick={() => onMerge(selected._id)}>{t("review_workspace.merge") || "Unir con la línea siguiente"}</button>}
          <button className={button} onClick={onUndo} disabled={!canUndo}>{t("review_workspace.undo") || "Deshacer"}</button>
          <button className={`${button} text-red-200`} onClick={() => { focusAfterChange.current = true; onDelete(selected._id); }}>{t("timeline.delete_line") || "Eliminar línea"}</button>
        </div>
      </> : <p className="text-sm">{t("review_workspace.empty") || "Agregá una línea para empezar."}</p>}
    </div>
  </section>;
}
