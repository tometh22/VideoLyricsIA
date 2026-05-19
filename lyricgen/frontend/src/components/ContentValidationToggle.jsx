import { useState } from "react";
import { useI18n } from "../i18n";

/**
 * Operator-facing toggle for `bypass_content_validation` flag.
 * Used in EditRequestPanel + VariantCreateModal (background regen mode).
 *
 * Two states with radio semantics:
 *   - ACTIVE (default, recommended)  → backend runs content_validator.
 *     Veo/Imagen outputs with face/hands/logos as subject get rejected
 *     BEFORE the expensive render, sparing CPU and avoiding downstream
 *     UMG rejection.
 *   - BYPASS (operator opts in)      → validator is skipped entirely.
 *     For concepts where the flagged content IS the song's identity
 *     (rock guitarist hands, neon city street with figures, etc.).
 *     UMG can still reject downstream — operator owns that decision.
 *
 * Wrapped in a collapsible disclosure so it's hidden by default and
 * doesn't clutter the main "regenerate background" flow.
 *
 * Props:
 *   value      — boolean. true = bypass ON. false (default) = validator ON.
 *   onChange   — fn(newValue: boolean)
 *   disabled   — boolean (true while a request is in flight)
 */
export default function ContentValidationToggle({
  value = false,
  onChange,
  disabled = false,
}) {
  const { t } = useI18n();
  // Start expanded ONLY when the operator already chose bypass — that
  // way the warning state is always visible. Default collapsed (saves
  // vertical space and matches "recommended default" intent).
  const [expanded, setExpanded] = useState(Boolean(value));

  const isBypass = Boolean(value);

  return (
    <div className={
      "rounded-md ring-1 transition-colors " +
      (isBypass
        ? "ring-amber-500/40 bg-amber-500/[0.04]"
        : "ring-white/[0.06] bg-surface-3/40")
    }>
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        disabled={disabled}
        className="w-full flex items-center justify-between px-3 py-2 text-[11px] text-ink-secondary tracking-wide disabled:opacity-50"
      >
        <span className="flex items-center gap-2">
          <span>{t("validation.section_label") || "Verificación de contenido"}</span>
          {isBypass && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30 font-mono">
              {t("validation.bypass_badge") || "RIESGO ASUMIDO"}
            </span>
          )}
        </span>
        <span className="text-ink-tertiary">{expanded ? "▴" : "▾"}</span>
      </button>

      {expanded && (
        <div className="px-3 pb-3 space-y-2">
          <label
            className={
              "flex items-start gap-2 cursor-pointer p-2 rounded ring-1 transition-colors " +
              (!isBypass
                ? "ring-brand/40 bg-brand/[0.06]"
                : "ring-white/[0.04] hover:ring-white/[0.10]")
            }
          >
            <input
              type="radio"
              checked={!isBypass}
              onChange={() => onChange?.(false)}
              disabled={disabled}
              className="mt-0.5 accent-brand"
            />
            <div className="flex-1">
              <div className="text-xs text-white font-medium">
                {t("validation.active_label") || "Activa"}{" "}
                <span className="text-[10px] text-ink-tertiary font-normal">
                  ({t("validation.recommended") || "recomendado · default"})
                </span>
              </div>
              <p className="text-[10px] text-ink-tertiary mt-0.5 leading-relaxed">
                {t("validation.active_desc") ||
                  "Bloquea fondos con caras / manos / logos detectables antes del render. Lo que UMG normalmente rechazaría."}
              </p>
            </div>
          </label>

          <label
            className={
              "flex items-start gap-2 cursor-pointer p-2 rounded ring-1 transition-colors " +
              (isBypass
                ? "ring-amber-500/50 bg-amber-500/[0.08]"
                : "ring-white/[0.04] hover:ring-white/[0.10]")
            }
          >
            <input
              type="radio"
              checked={isBypass}
              onChange={() => onChange?.(true)}
              disabled={disabled}
              className="mt-0.5 accent-amber-500"
            />
            <div className="flex-1">
              <div className="text-xs text-white font-medium">
                {t("validation.bypass_label") || "Asumir el riesgo"}
              </div>
              <p className="text-[10px] text-ink-tertiary mt-0.5 leading-relaxed">
                {t("validation.bypass_desc") ||
                  "El render sale aunque la AI genere caras / manos / logos. UMG puede rechazar el video después — vos asumís esa decisión."}
              </p>
            </div>
          </label>
        </div>
      )}
    </div>
  );
}
