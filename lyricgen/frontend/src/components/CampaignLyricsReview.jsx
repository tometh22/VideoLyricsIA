import { useEffect, useMemo, useRef, useState } from "react";
import { planReviewedSegmentSplit } from "../lib/splitWords";
import { useI18n } from "../i18n";
import RepeatOccurrencesList, { repeatApplyLabel, selectedRepeatIds } from "./RepeatOccurrencesList";

const timestamp = value => {
  const ticks = Math.max(0, Math.round((Number(value) || 0) * 10));
  return `${Math.floor(ticks / 600)}:${(Math.floor(ticks / 10) % 60).toString().padStart(2, "0")}.${ticks % 10}`;
};

/** One selected line owns the text, playback and timing actions. */
export default function CampaignLyricsReview({ segments, focusedId, markedIds, audioAvailable, activeId = null, isPlaying = false,
  preview = null, onSelect, onTextChange, onListen, onAdvanced, onDuplicate, onDelete, onAdd, onSplit,
  getSplitPlan, onUndo, canUndo, onMerge, onMergePrevious, onTextFocus, onTextBlur, rowRefs,
  suggestionsById = {}, onApplySuggestion, propagationPrompt, onApplyPropagation, onDismissPropagation, onTogglePropagation }) {
  const { t } = useI18n();
  const [markedOnly, setMarkedOnly] = useState(false);
  const [follow, setFollow] = useState(true);
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const splitButtonRef = useRef(null);
  const [selection, setSelection] = useState(null);
  const [composing, setComposing] = useState(false);
  const focusAfterChange = useRef(false);
  const selected = segments.find((line) => line._id === focusedId) || segments[0];
  const captureSelection = (input) => setSelection({
    id: selected?._id, text: input.value,
    start: input.selectionStart, end: input.selectionEnd,
  });
  const planAtSelection = (cursor) => {
    if (!onSplit || composing || !selected || cursor?.id !== selected._id || cursor.text !== selected.text
      || cursor.start == null || cursor.start !== cursor.end
      || !cursor.text.slice(0, cursor.start).trim() || !cursor.text.slice(cursor.start).trim()) return null;
    return getSplitPlan ? getSplitPlan(selected._id, cursor.start)
      : planReviewedSegmentSplit(selected, { charOffset: cursor.start });
  };
  const splitPlan = planAtSelection(selection);
  const splitHere = (cursor) => {
    // Recompute from the current line: never apply an old preview after sync.
    if (!planAtSelection(cursor)) return;
    focusAfterChange.current = true;
    onSplit(selected._id, cursor.start);
    setSelection(null);
  };
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
  useEffect(() => {
    if (!follow || !isPlaying || activeId == null) return;
    const row = rowRefs.current[activeId];
    const list = listRef.current;
    if (!row || !list) return;
    // Follow only inside the list. Never move the document or the text caret.
    list.scrollTop = Math.max(0, row.offsetTop - (list.clientHeight - row.offsetHeight) / 2);
  }, [activeId, isPlaying, follow, markedOnly, rowRefs]);
  const button = "min-h-10 rounded-xl px-3 py-2 text-sm ring-1 ring-white/15 hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light disabled:opacity-40";
  const selectedIndex = segments.indexOf(selected);
  const previous = [...visible].reverse().find(line => segments.indexOf(line) < selectedIndex);
  const next = visible.find(line => segments.indexOf(line) > selectedIndex);
  const navigate = (line) => { if (line) onSelect(line); };
  return <section data-testid="campaign-line-workspace" className="grid min-w-0 gap-4 lg:grid-cols-[minmax(300px,400px)_minmax(0,1fr)] lg:items-start">
    <div className="min-w-0 rounded-2xl bg-surface-2 ring-1 ring-white/10">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 p-3">
        <h2 className="text-sm font-semibold">{t("review_workspace.lines") || "Líneas"} ({segments.length})</h2>
        <button className={button} aria-pressed={markedOnly} onClick={() => setMarkedOnly((value) => !value)}>{t("review_workspace.marked_only") || "Solo marcadas"}</button>
      </div>
      <div className="flex items-center justify-between gap-2 px-3 py-2 text-xs text-ink-secondary border-b border-white/10">
        <span>{t("review_workspace.time_range") || "Inicio → Fin"}</span>
        <button type="button" aria-pressed={follow} onClick={() => setFollow(value => !value)} className={`min-h-9 rounded-lg px-2 hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-brand-light ${follow ? "bg-emerald-300/10 text-emerald-200 ring-1 ring-emerald-300/30" : "text-ink-secondary"}`}>{t("review_workspace.follow_audio") || "Seguir reproducción"}</button>
      </div>
      <div ref={listRef} data-testid="review-line-list" className="relative max-h-[min(52dvh,560px)] overflow-y-auto overscroll-contain p-2" aria-label={t("review_workspace.lines") || "Líneas"}>
        {visible.map((line) => <button type="button" key={line._id} ref={(node) => { rowRefs.current[line._id] = node; }}
          data-testid={`review-line-${segments.indexOf(line) + 1}`} data-playing={isPlaying && line._id === activeId ? "true" : undefined} aria-current={selected?._id === line._id ? "true" : undefined}
          onClick={() => navigate(line)} onKeyDown={(event) => {
            if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
            event.preventDefault(); event.stopPropagation();
            const next = visible[visible.indexOf(line) + (event.key === "ArrowDown" ? 1 : -1)];
            if (next) { navigate(next); rowRefs.current[next._id]?.focus(); }
          }}
          className={`mb-1 flex w-full items-start gap-3 rounded-xl px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light ${selected?._id === line._id ? "bg-brand/20 ring-1 ring-brand/50" : "hover:bg-white/5"} ${isPlaying && line._id === activeId ? "border-l-4 border-emerald-300 bg-emerald-300/10" : "border-l-4 border-transparent"}`}>
          <span className="w-6 shrink-0 pt-0.5 text-xs tabular-nums text-ink-secondary">{segments.indexOf(line) + 1}</span>
          <span className="min-w-0 flex-1">
            <span className="block break-words text-base leading-snug">{line.text || (t("review_workspace.empty_line") || "Línea sin texto")}</span>
            <span className="mt-1 flex flex-wrap items-center gap-2 font-mono text-xs tabular-nums text-ink-secondary">
              <span>{timestamp(line.start)} → {timestamp(line.end)}</span>
              {isPlaying && line._id === activeId && <span className="font-sans text-emerald-200">{t("review_workspace.playing") || "Sonando"}</span>}
            </span>
          </span>
          {marked(line) && <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-amber-300" aria-label={t("review_workspace.marked") || "Marcada para revisar"} />}
        </button>)}
        {!visible.length && <p className="p-4 text-sm text-ink-secondary">{t("review_workspace.no_marks") || "No hay líneas marcadas."}</p>}
      </div>
      <div className="border-t border-white/10 p-3"><button className={`${button} w-full`} onClick={() => { focusAfterChange.current = true; onAdd(); }}>{t("editor.add_line") || "Agregar línea"}</button></div>
    </div>
    <div className="order-first min-w-0 self-start rounded-2xl bg-surface-2 p-4 ring-1 ring-white/10 lg:sticky lg:top-36 lg:order-none lg:max-h-[calc(100dvh-232px)] lg:overflow-y-auto" data-testid="selected-line-panel">
      {selected ? <>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-semibold">{(t("review_workspace.selected") || "Línea {n}").replace("{n}", segments.indexOf(selected) + 1)}</h2>
          <span className="font-mono text-sm text-ink-secondary">{timestamp(selected.start)} → {timestamp(selected.end)}</span>
        </div>
        {selected.timing_validation?.status === "unvalidated" && <p className="mb-3 text-sm text-amber-200">
          {selected.timing_validation.reason === "estimated_split"
            ? (t("review_workspace.split_review") || "Corte estimado · escuchá y revisá los tiempos de esta frase.")
            : (t("review_workspace.timing_unvalidated") || "Timing no validado · revisá este tramo contra el audio. No se cambiaron sus tiempos.")}
        </p>}
        {marked(selected) && <p className="mb-3 text-xs text-amber-200">{t("review_workspace.marked") || "Marcada para revisar"}</p>}
        <div className="mb-4 flex items-center justify-between gap-2">
          <button type="button" className={button} disabled={!previous} onClick={() => navigate(previous)}>{t("review_workspace.previous") || "← Anterior"}</button>
          <span className="text-xs tabular-nums text-ink-secondary">{segments.indexOf(selected) + 1} / {segments.length}</span>
          <button type="button" className={button} disabled={!next} onClick={() => navigate(next)}>{t("review_workspace.next") || "Siguiente →"}</button>
        </div>
        {markedOnly && !visible.includes(selected) && <p className="mb-3 text-xs text-ink-secondary">{t("review_workspace.outside_filter") || "La línea seleccionada está fuera del filtro de marcadas."}</p>}
        <label className="mb-2 block text-sm font-medium" htmlFor="selected-lyric-text">{t("review_workspace.text") || "Letra de esta línea"}</label>
        <input id="selected-lyric-text" ref={inputRef} value={selected.text || ""}
          aria-describedby="line-split-guidance"
          onChange={(event) => { captureSelection(event.currentTarget); onTextChange(selected._id, event.target.value); }}
          onSelect={(event) => captureSelection(event.currentTarget)}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={(event) => { setComposing(false); captureSelection(event.currentTarget); }}
          onFocus={(event) => { captureSelection(event.currentTarget); onTextFocus?.(selected); }}
          onBlur={(event) => { if (event.relatedTarget !== splitButtonRef.current) onTextBlur?.(selected._id, event.target.value); }}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing && !composing) {
            event.preventDefault();
            if (event.repeat) return;
            splitHere({ id: selected._id, text: event.currentTarget.value,
              start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd });
          } else if (event.key === "Backspace" && !event.nativeEvent.isComposing && !composing
            && event.currentTarget.value === "" && event.currentTarget.selectionStart === 0 && event.currentTarget.selectionEnd === 0) {
            event.preventDefault(); focusAfterChange.current = true; onMergePrevious?.(selected._id);
          } }}
          className="min-h-12 w-full rounded-xl border border-white/15 bg-surface-1 px-3 py-3 text-base text-white focus:border-brand-light focus:outline-none" />
        <p className="mt-2 text-xs text-ink-secondary">{t("review_workspace.autosave") || "Los cambios se guardan automáticamente."}</p>
        {preview && <div className="mt-3" data-testid="review-live-preview">{preview}</div>}
        <div className="mt-3 rounded-xl border border-white/10 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" ref={splitButtonRef} className={`${button} shrink-0 bg-brand/15 text-brand-light`}
              disabled={!splitPlan} aria-describedby="line-split-guidance"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => splitHere(selection)}>
              {t("review_workspace.split_here") || "Dividir aquí"}
            </button>
            <p id="line-split-guidance" className="min-w-0 flex-1 text-xs text-ink-secondary">
              {t("review_workspace.split_guidance") || "Colocá el cursor donde querés cortar. Revisá las dos frases y pulsá Dividir aquí o Enter."}
            </p>
          </div>
          <p role="status" aria-atomic="true" className={`mt-2 text-xs ${splitPlan?.timing === "char_ratio" ? "text-amber-200" : "text-ink-secondary"}`}>
            {splitPlan ? (splitPlan.timing === "words"
              ? (t("review_workspace.split_words") || "Usa los tiempos por palabra disponibles; conserva el inicio y el final de la línea.")
              : (t("review_workspace.split_estimated") || "Timing estimado: escuchá el corte y ajustalo si hace falta. Ambas frases quedarán marcadas para revisar."))
              : (t("review_workspace.split_invalid") || "Elegí un punto con texto a ambos lados, sin seleccionar un fragmento.")}
          </p>
          {splitPlan && <div role="group" aria-label={t("review_workspace.split_preview") || "Vista previa de la división"}
            className="mt-3 grid min-w-0 gap-2 sm:grid-cols-2">
            {[splitPlan.a, splitPlan.b].map((part, index) => <div key={index} className="min-w-0 rounded-lg bg-surface-1 p-3">
              <p className="text-xs font-medium text-ink-secondary">{(t("review_workspace.split_part") || "Frase {n}").replace("{n}", index + 1)}</p>
              <p className="mt-1 break-words text-sm">{part.text}</p>
              <p className="mt-2 font-mono text-xs text-ink-secondary">{timestamp(part.start)} → {timestamp(part.end)}</p>
            </div>)}
          </div>}
        </div>
        {suggestionsById[selected._id] && suggestionsById[selected._id] !== selected.text && <button
          type="button" className={`${button} mt-3`} onClick={() => onApplySuggestion?.(selected._id)}>
          {t("review_workspace.apply_suggestion") || "Aplicar sugerencia"}: {suggestionsById[selected._id]}
        </button>}
        {propagationPrompt && <div className="mt-3 rounded-xl bg-brand/10 p-3 text-sm ring-1 ring-brand/30">
          <p className="mb-2 break-words">{propagationPrompt.newText}</p>
          <p>{(t("editor.repeat_prompt") || "Esta línea se repite en otras {n}. ¿Aplicar el cambio a todas?").replace("{n}", propagationPrompt.matchIds.length)}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className={button} onClick={onApplyPropagation} disabled={!selectedRepeatIds(propagationPrompt).length}>{repeatApplyLabel(propagationPrompt, t)}</button>
            <button type="button" className={button} onClick={onDismissPropagation}>{t("editor.repeat_only_this") || "Solo esta"}</button>
          </div>
          <RepeatOccurrencesList prompt={propagationPrompt} segments={segments} onToggle={onTogglePropagation} t={t} />
        </div>}
        <div className="mt-4 flex flex-wrap gap-2">
          <button className={`${button} bg-brand text-white`} disabled={!audioAvailable} onClick={() => onListen(selected)}>{t("review_workspace.listen") || "Escuchar 2 s antes"}</button>
          <button className={button} onClick={() => onAdvanced(selected)}>{t("review_workspace.timing") || "Ajustar esta línea en la timeline"}</button>
        </div>
        <div className="mt-4 flex flex-wrap gap-2 border-t border-white/10 pt-4">
          <button className={button} onClick={() => { focusAfterChange.current = true; onDuplicate(selected._id); }}>{t("timeline.duplicate_line") || "Duplicar línea"}</button>
          {segments.indexOf(selected) < segments.length - 1 && <button className={button} title={t("review_workspace.merge") || "Unir con la línea siguiente"} onClick={() => onMerge(selected._id)}>{t("review_workspace.merge") || "Unir con la línea siguiente"}</button>}
          <button className={button} onClick={onUndo} disabled={!canUndo}>{t("review_workspace.undo") || "Deshacer"}</button>
          <button className={`${button} ml-auto text-red-200`} onClick={() => { focusAfterChange.current = true; onDelete(selected._id); }}>{t("timeline.delete_line") || "Eliminar línea"}</button>
        </div>
      </> : <p className="text-sm">{t("review_workspace.empty") || "Agregá una línea para empezar."}</p>}
    </div>
  </section>;
}
