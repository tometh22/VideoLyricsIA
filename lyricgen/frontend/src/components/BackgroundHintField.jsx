import { useI18n } from "../i18n";

/**
 * Textarea + label + char counter + helper text para el campo
 * `background_hint` que se manda a Gemini con header `[OPERATOR OVERRIDE]`.
 *
 * Lo usan dos call sites para que la copia + max length + visual estén
 * sincronizados:
 *  - EditRequestPanel.jsx, mode "background" (PR #116)
 *  - VariantCreateModal.jsx (variantes de jobs aprobados)
 *
 * Props:
 *   value      — string (controlled)
 *   onChange   — fn(newValue)
 *   disabled   — boolean
 *   maxLength  — opcional, default 300 (mismo que EditJobRequest backend)
 */
export default function BackgroundHintField({
  value,
  onChange,
  disabled = false,
  maxLength = 300,
}) {
  const { t } = useI18n();
  return (
    <div>
      <label className="block text-[11px] text-ink-secondary mb-1.5 tracking-wide">
        {t("edit.background_hint_label") || "¿Querés aclarar qué tipo de fondo? (opcional)"}
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value.slice(0, maxLength))}
        placeholder={t("edit.background_hint_placeholder") ||
          "ej: 'paisaje romántico al atardecer con tonos cálidos' · 'abstracto con ondas de luz suave' · 'interior cálido tipo café íntimo'"}
        rows={3}
        disabled={disabled}
        className="w-full text-xs px-3 py-2 rounded-md bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none resize-none text-white placeholder:text-ink-tertiary disabled:opacity-50"
      />
      <div className="flex items-center justify-between mt-1">
        <p className="text-[10px] text-ink-tertiary">
          {t("edit.background_hint_help") ||
            "Sirve cuando los fondos anteriores no captaron el tono. Dejá vacío para que el sistema decida."}
        </p>
        <p className="text-[10px] text-ink-tertiary font-mono tabular-nums">
          {value.length}/{maxLength}
        </p>
      </div>
    </div>
  );
}
