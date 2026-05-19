import { useState } from "react";
import { useI18n } from "../i18n";

// Hardcoded list. Mirror of backend pipeline.py UMG_TENANTS — if you
// add a B2B partner here, update the backend constant too. Single source
// of truth is the backend (which actually enforces the gate); this
// frontend list only controls UI copy + default toggle position.
const UMG_TENANTS = new Set(["umg", "omg"]);

function isUmgTenant(tenantId) {
  return UMG_TENANTS.has(String(tenantId || "").toLowerCase());
}

/**
 * Operator-facing toggle for content validation behavior. The wire
 * representation is a boolean: `value=true` means "validation runs",
 * `value=false` means "validation skipped". The parent component is
 * responsible for translating that boolean into the right backend flag
 * for the tenant:
 *
 *   UMG tenant (default behavior: validate):
 *     - value=true  → no payload field (matches tenant default)
 *     - value=false → send `bypass_content_validation: true`
 *
 *   Non-UMG tenant (default behavior: skip):
 *     - value=true  → send `force_content_validation: true`
 *     - value=false → no payload field (matches tenant default)
 *
 * UI copy differs per tenant so each operator sees their choice framed
 * in the way that matches the default they're departing from:
 *
 *   UMG:     "Activa (default)" vs "Asumir el riesgo" (amber warning)
 *   non-UMG: "Sin verificación (default)" vs "Activar verificación"
 *
 * Props:
 *   value      — boolean. true = validate, false = skip.
 *   onChange   — fn(newValue: boolean)
 *   tenantId   — string | undefined. Determines copy + default state.
 *                Falls back to UMG semantics if missing (safer default).
 *   disabled   — boolean
 *   initialOpen — optional override; defaults to expanded when the
 *                operator's choice differs from the tenant default
 *                (so the warning/info is always visible at glance).
 */
export default function ContentValidationToggle({
  value,
  onChange,
  tenantId,
  disabled = false,
  initialOpen,
}) {
  const { t } = useI18n();
  const isUmg = isUmgTenant(tenantId);
  // Tenant default for `value`. The parent should initialize state to
  // this; if it didn't (value is undefined), treat as the default.
  const tenantDefault = isUmg; // UMG defaults to validate=true
  const effectiveValue = typeof value === "boolean" ? value : tenantDefault;
  // Operator is "departing from default" when their choice doesn't match
  // the tenant default. That's when we want the amber warning visible.
  const isDeparting = effectiveValue !== tenantDefault;

  const [expanded, setExpanded] = useState(
    typeof initialOpen === "boolean" ? initialOpen : isDeparting
  );

  // Per-tenant copy. V3 framing: state the question, then both options as
  // explicit "Sí / No" answers tied to "apto para UMG" vs "fondo libre"
  // so the operator sees the tradeoff in plain language. The default
  // (recommended) and alt sides swap per tenant, but the wording for each
  // side stays the same — only its position changes.
  const copy = isUmg
    ? {
        sectionLabel: t("validation.section_label") || "¿Restringir el contenido del fondo?",
        defaultLabel: t("validation.umg_default_label") || "Sí — apto para UMG",
        defaultRecommended: t("validation.umg_recommended") || "recomendado · default",
        defaultDesc:
          t("validation.umg_default_desc") ||
          "Sin caras, sin manos, sin logos detectables.",
        altLabel: t("validation.umg_alt_label") || "No — fondo libre",
        altDesc:
          t("validation.umg_alt_desc") ||
          "Cualquier escena. Riesgo de rechazo por UMG.",
        badge: t("validation.umg_badge") || "FONDO LIBRE",
        // amber when departing (operator is opting OUT of the safer default)
        departingTone: "amber",
      }
    : {
        sectionLabel: t("validation.section_label") || "¿Restringir el contenido del fondo?",
        defaultLabel: t("validation.nonumg_default_label") || "No — fondo libre",
        defaultRecommended: t("validation.nonumg_default_recommended") || "default",
        defaultDesc:
          t("validation.nonumg_default_desc") ||
          "Cualquier escena, sin restricciones.",
        // Non-UMG operators don't know what UMG is. Describe the behavior
        // in concrete terms instead of namedropping the vendor's rule set.
        altLabel: t("validation.nonumg_alt_label") || "Sí — sin personas ni marcas",
        altDesc:
          t("validation.nonumg_alt_desc") ||
          "Bloquea caras, manos y logos detectables en el fondo. Útil para entrega a clientes con restricciones de imagen.",
        badge: t("validation.nonumg_badge") || "MODO RESTRINGIDO",
        // brand color when departing (operator is opting INTO the stricter mode)
        departingTone: "brand",
      };

  // Tone-driven classes (amber for UMG-bypass, brand for non-UMG-enable).
  const departingRingClass =
    copy.departingTone === "amber"
      ? "ring-amber-500/40 bg-amber-500/[0.04]"
      : "ring-brand/40 bg-brand/[0.04]";
  const departingBadgeClass =
    copy.departingTone === "amber"
      ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
      : "bg-brand/15 text-brand-light ring-brand/30";
  const departingOptionClass =
    copy.departingTone === "amber"
      ? "ring-amber-500/50 bg-amber-500/[0.08]"
      : "ring-brand/50 bg-brand/[0.08]";
  const departingRadioAccent =
    copy.departingTone === "amber" ? "accent-amber-500" : "accent-brand";

  // Map to the two radio buttons. "default" radio corresponds to the
  // tenant's default behavior; "alt" is the departure.
  const defaultRadioValue = tenantDefault; // boolean for `value` when this radio is selected
  const altRadioValue = !tenantDefault;
  const defaultSelected = effectiveValue === defaultRadioValue;
  const altSelected = effectiveValue === altRadioValue;

  return (
    <div className={
      "rounded-md ring-1 transition-colors " +
      (isDeparting
        ? departingRingClass
        : "ring-white/[0.06] bg-surface-3/40")
    }>
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        disabled={disabled}
        className="w-full flex items-center justify-between px-3 py-2 text-[11px] text-ink-secondary tracking-wide disabled:opacity-50"
      >
        <span className="flex items-center gap-2">
          <span>{copy.sectionLabel}</span>
          {isDeparting && (
            <span className={
              "text-[10px] px-1.5 py-0.5 rounded ring-1 font-mono " +
              departingBadgeClass
            }>
              {copy.badge}
            </span>
          )}
        </span>
        <span className="text-ink-tertiary">{expanded ? "▴" : "▾"}</span>
      </button>

      {expanded && (
        <div className="px-3 pb-3 space-y-2">
          {/* Default (tenant-matching) option */}
          <label
            className={
              "flex items-start gap-2 cursor-pointer p-2 rounded ring-1 transition-colors " +
              (defaultSelected
                ? "ring-brand/40 bg-brand/[0.06]"
                : "ring-white/[0.04] hover:ring-white/[0.10]")
            }
          >
            <input
              type="radio"
              checked={defaultSelected}
              onChange={() => onChange?.(defaultRadioValue)}
              disabled={disabled}
              className="mt-0.5 accent-brand"
            />
            <div className="flex-1">
              <div className="text-xs text-white font-medium">
                {copy.defaultLabel}{" "}
                <span className="text-[10px] text-ink-tertiary font-normal">
                  ({copy.defaultRecommended})
                </span>
              </div>
              <p className="text-[10px] text-ink-tertiary mt-0.5 leading-relaxed">
                {copy.defaultDesc}
              </p>
            </div>
          </label>

          {/* Alt (departure) option */}
          <label
            className={
              "flex items-start gap-2 cursor-pointer p-2 rounded ring-1 transition-colors " +
              (altSelected
                ? departingOptionClass
                : "ring-white/[0.04] hover:ring-white/[0.10]")
            }
          >
            <input
              type="radio"
              checked={altSelected}
              onChange={() => onChange?.(altRadioValue)}
              disabled={disabled}
              className={"mt-0.5 " + departingRadioAccent}
            />
            <div className="flex-1">
              <div className="text-xs text-white font-medium">
                {copy.altLabel}
              </div>
              <p className="text-[10px] text-ink-tertiary mt-0.5 leading-relaxed">
                {copy.altDesc}
              </p>
            </div>
          </label>

          {/* Warning when "people allowed" is selected. value=false here
              means "no restriction" → AI is free to generate faces / hands.
              Veo's quality on people is uneven (deformed hands, weird
              eyes). Surface that honestly so the operator knows what
              they're committing to. */}
          {effectiveValue === false && (
            <p className="text-[10px] text-ink-tertiary leading-relaxed px-1 pt-1 border-t border-white/[0.05]">
              ⚠ {t("validation.people_quality_warning") ||
                  "El modelo a veces genera personas con artefactos (caras deformadas, manos con dedos extras). Probá un render antes de aprobar — si vas a entregar a un cliente, revisá el resultado."}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// Re-exported helpers so parents can share the same tenant logic
// without duplicating the hardcoded list.
export { isUmgTenant, UMG_TENANTS };
