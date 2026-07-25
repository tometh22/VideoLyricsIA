/**
 * Ficha read-only de los ajustes con los que se renderizó el video.
 *
 * Por qué existe: hasta ahora el operador no tenía NINGUNA forma de ver con qué
 * se hizo un video. La única lista de ajustes vivía en el panel admin, como un
 * dump crudo de render_params con keys snake_case. En el reclamo que originó
 * esto, el operador regeneró el fondo siete veces sin poder ver que el video
 * tenía guardado `movement_style: "animado"` — la ficha convierte eso en dos
 * segundos de lectura.
 *
 * Colapsable y cerrada por defecto: es información de diagnóstico, no la acción
 * principal de la pantalla. El prompt completo va en un <details> aparte porque
 * es el único campo de texto largo.
 */
import { useState } from "react";
import { useI18n } from "../i18n";
import { buildSettingsSummary, describeSceneSource } from "../lib/renderSettingsSummary";
import {
  MOVEMENT_LABELS, EFFECT_LABELS, FONT_LABELS,
  AXIS_VALUE_LABELS, dynamicAxisLabel,
} from "../lib/optionLabels";

export default function JobSettingsCard({ renderParams, provenanceHref }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const params = renderParams || {};

  const groups = buildSettingsSummary(params, {
    t,
    // Los resolvers salen del MISMO catálogo que los pickers del wizard, así
    // que la ficha y el wizard no pueden nombrar distinto a la misma opción.
    movementLabel: (code) => MOVEMENT_LABELS(t)[code],
    effectLabel: (code) => EFFECT_LABELS(t)[code],
    fontLabel: (code) => FONT_LABELS(t)[code],
    // Ejes enum sin catálogo propio (case, contraste, formato, animación,
    // transición, portada) + los que se etiquetan por key derivada del código
    // (género, concepto). Sin esto la ficha mostraba "strong", "lower_third" o
    // "atmosferico" — códigos internos, varios en inglés.
    valueLabel: (axisKey, code) =>
      AXIS_VALUE_LABELS(t)[axisKey]?.[String(code).trim().toLowerCase()]
      || dynamicAxisLabel(t, axisKey, code),
  });
  const scene = describeSceneSource(params, t);
  const prompt = String(params.background_hint || "").trim();

  if (groups.length === 0 && !prompt) {
    // Un job sin ningún ajuste explícito (todo en Auto): no vale un panel.
    return null;
  }

  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="job-settings-toggle"
        className="w-full flex items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-white/[0.02] transition-colors"
      >
        <div className="min-w-0">
          <p className="text-[12px] font-medium text-gray-200">
            {t("detail.settings_title") || "Con qué se hizo este video"}
          </p>
          <p className="text-[10px] text-gray-600 mt-0.5 truncate">{scene}</p>
        </div>
        <svg
          className={`w-4 h-4 shrink-0 text-gray-500 transition-transform ${open ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div className="px-4 pb-3.5 space-y-3" data-testid="job-settings-body">
          {/* El prompt se renderiza aparte (abajo), así que un grupo cuyo único
              chip sea el prompt quedaba como un título "FONDO" sobre un
              contenedor vacío. Se filtra ANTES de decidir si el grupo existe. */}
          {groups
            .map((g) => ({ ...g, chips: g.chips.filter((c) => !c.isPrompt) }))
            .filter((g) => g.chips.length > 0)
            .map((group) => (
            <div key={group.id}>
              <p className="text-[10px] uppercase tracking-wide text-gray-600 mb-1.5">
                {group.label}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {group.chips.map((chip) => (
                  <span
                    key={chip.key}
                    data-setting={chip.key}
                    className="inline-flex items-baseline gap-1 rounded-full bg-surface-3/70 ring-1 ring-white/[0.06] px-2 py-0.5 text-[10.5px]"
                  >
                    <span className="text-gray-500">{chip.label}</span>
                    <span className="text-gray-200 font-medium">{chip.value}</span>
                  </span>
                ))}
              </div>
            </div>
          ))}

          {prompt && (
            <details className="group">
              <summary className="text-[10.5px] text-gray-500 hover:text-gray-300 cursor-pointer list-none">
                {t("detail.settings_prompt_show") || "Ver el prompt usado"}
              </summary>
              <p className="mt-1.5 text-[11px] text-gray-400 whitespace-pre-wrap leading-snug rounded-lg bg-surface-1/60 px-3 py-2">
                {prompt}
              </p>
            </details>
          )}

          {/* El prompt EXACTO que recibió Veo/Imagen (post-rails) ya se expone
              en el tab Provenance. La ficha muestra la intención del operador;
              el link lleva a lo que el modelo realmente recibió. */}
          {provenanceHref && (
            <button
              type="button"
              onClick={provenanceHref}
              className="text-[10.5px] text-gray-600 hover:text-gray-300 underline-offset-2 hover:underline transition-colors"
            >
              {t("detail.settings_see_provenance") || "Ver el prompt exacto que recibió la IA →"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
