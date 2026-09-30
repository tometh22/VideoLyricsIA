import { useI18n } from "../../i18n";
import { AXIS_VALUE_LABELS, EFFECT_LABELS, FONT_LABELS, MOVEMENT_LABELS, dynamicAxisLabel } from "../../lib/optionLabels";
import useBackgroundPreviewTokens, { backgroundPreviewUrl } from "../../hooks/useBackgroundPreviewTokens";
import WizardLivePreview from "../WizardLivePreview";
import { campaignApiBase } from "../../lib/campaignApi";
import { inputClass } from "./ui";

export const SOURCE_LABELS = {
  lyrics: "Inspirado en la letra", auto: "Automático", prompt_literal: "Prompt exacto",
  prompt_improved: "Prompt mejorado con IA", as_is: "Usar tal cual", variation: "Crear variación",
};

function initialValue(field) {
  if (field.kind === "boolean") return false;
  if (field.kind === "number") return 1;
  if (field.kind === "asset") return null;
  if (field.kind === "color") return "#FFFFFF";
  return field.options?.[0] ?? "";
}

/**
 * Visual override editor: every campaign creative field grouped as in the
 * wizard. Only toggled fields are sent; the rest inherit the base style.
 */
export default function StyleControls({ fields = {}, values = {}, update, assets = [], label, disabled = false }) {
  const { t } = useI18n();
  const optionLabel = (key, value) => (key === "effect" ? EFFECT_LABELS(t)[value] : key === "movement_style" ? MOVEMENT_LABELS(t)[value]
    : key.includes("font") ? FONT_LABELS(t)[value] : AXIS_VALUE_LABELS(t)[key]?.[value] || SOURCE_LABELS[value] || dynamicAxisLabel(t, key, value)) || value || "Auto / ninguno";
  const groups = [...new Set(Object.values(fields).map((field) => field.group))];
  return <div className="grid gap-2">
    {groups.map((group) => {
      const entries = Object.entries(fields).filter(([, field]) => field.group === group);
      const active = entries.filter(([key]) => Object.hasOwn(values, key)).length;
      return <details key={group} className="group rounded-xl bg-black/20 ring-1 ring-white/[0.06] open:ring-white/10">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-semibold [&::-webkit-details-marker]:hidden">
          <span>{group}</span>
          <span className="flex items-center gap-2 text-xs font-normal text-ink-secondary">{active ? `${active} ${active === 1 ? "ajuste" : "ajustes"}` : "Heredado"}<span aria-hidden="true" className="transition group-open:rotate-90">›</span></span>
        </summary>
        <div className="grid gap-3 border-t border-white/[0.06] p-4 md:grid-cols-2">
          {entries.map(([key, field]) => {
            const enabled = Object.hasOwn(values, key);
            return <div key={key} className={`rounded-lg p-2 ${enabled ? "bg-brand/[0.06] ring-1 ring-brand/20" : ""}`}>
              <label className="mb-2 flex items-center gap-2 text-xs text-ink-secondary">
                <input type="checkbox" disabled={disabled} checked={enabled} className="accent-[#7557FF]"
                  onChange={(event) => update(key, event.target.checked ? initialValue(field) : undefined)} />
                Cambiar {field.label}
              </label>
              {enabled && (field.kind === "select" || field.kind === "asset"
                ? <select aria-label={`${label}: ${field.label}`} disabled={disabled} className={inputClass} value={values[key] ?? ""}
                  onChange={(event) => update(key, field.kind === "asset" ? (event.target.value ? Number(event.target.value) : null) : event.target.value)}>
                  {field.kind === "asset"
                    ? <><option value="">Generar con IA</option>{assets.map((asset) => <option key={asset.id} value={asset.id}>{asset.name} · {asset.file_type}</option>)}</>
                    : field.options.map((value) => <option key={value} value={value}>{optionLabel(key, value)}</option>)}
                </select>
                : field.kind === "boolean"
                  ? <label className="flex items-center gap-2 text-sm"><input aria-label={`${label}: ${field.label}`} disabled={disabled} type="checkbox" className="accent-[#7557FF]" checked={Boolean(values[key])} onChange={(event) => update(key, event.target.checked)} />Activado</label>
                  : field.kind === "textarea"
                    ? <textarea aria-label={`${label}: ${field.label}`} disabled={disabled} rows={3} className={inputClass} maxLength={field.max_length} value={values[key] ?? ""} onChange={(event) => update(key, event.target.value)} />
                    : <input aria-label={`${label}: ${field.label}`} disabled={disabled} className={field.kind === "color" ? "h-10 w-20 cursor-pointer rounded-lg bg-transparent" : inputClass}
                      type={field.kind === "color" ? "color" : field.kind === "number" ? "number" : "text"} min={field.min} max={field.max} step={field.step} maxLength={field.max_length}
                      value={values[key] ?? ""} onChange={(event) => update(key, field.kind === "number" ? Number(event.target.value) : event.target.value)} />)}
            </div>;
          })}
        </div>
      </details>;
    })}
  </div>;
}

export function StylePreview({ settings: s, assets }) {
  const API = campaignApiBase();
  const asset = assets.find((item) => item.id === s.background_id);
  const tokens = useBackgroundPreviewTokens(asset ? [asset.id] : [], API);
  const photo = asset ? asset.file_type !== "mp4" : s.movement_style === "foto-parallax";
  const source = asset ? backgroundPreviewUrl(API, asset.id, tokens[asset.id]) : photo ? "/movement_samples/foto-fija.jpg" : `/movement_samples/${s.movement_style || "estandar"}.mp4`;
  return <div className="max-w-xl">
    <WizardLivePreview placeholderBg={!asset} clipSrc={source || ""} clipIsVideo={!photo}
      operatorPhoto={!!asset && photo} photoAnimated={s.animate_image} style={s.style} customColors={s.custom_colors}
      movementStyle={s.movement_style} effect={s.effect} font={s.font} fontScale={s.font_scale} textCase={s.text_case}
      textContrast={s.text_contrast} lyricsAnimation={s.lyrics_animation} lineTransition={s.line_transition}
      lyricColor={s.lyric_color} lyricSungColor={s.lyric_sung_color} frameFormat={s.frame_format}
      lyric="Así se verá la letra" />
    <p className="mt-2 text-xs text-ink-secondary">Muestra del estilo. El fondo IA definitivo se genera después de aprobar letra y tiempos.</p>
  </div>;
}
