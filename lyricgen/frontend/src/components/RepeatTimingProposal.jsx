import { useI18n } from "../i18n";
import { useState } from "react";
export default function RepeatTimingProposal({ proposal, segments, onApply, onDismiss }) {
  const { t } = useI18n();
  const [selected, setSelected] = useState([]);
  return <section className="space-y-2 border-b border-white/10 bg-brand/10 p-3 text-xs text-white" aria-label={t("timeline.repeat_duration_region")}>
    <p>{t("timeline.repeat_duration_prompt").replace("{seconds}", proposal.length.toFixed(2))}</p>
    <p>{t("timeline.repeat_duration_hint")}</p>
    <div className="flex flex-wrap gap-3">{proposal.candidates.map(item => {
      const row = segments.find(segment => segment._id === item.id);
      return <label key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={event => setSelected(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} /> {Math.floor(item.start / 60)}:{String(Math.floor(item.start % 60)).padStart(2, "0")} · {row?.text}</label>;
    })}</div>
    <button type="button" className="mr-3 rounded bg-brand px-3 py-2 disabled:opacity-40" disabled={!selected.length} onClick={() => onApply(selected)}>{t("timeline.repeat_duration_apply")}</button>
    <button type="button" onClick={onDismiss}>{t("timeline.repeat_only_this")}</button>
  </section>;
}
