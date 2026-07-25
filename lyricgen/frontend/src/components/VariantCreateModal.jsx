import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BackgroundHintField from "./BackgroundHintField";
import ContentValidationToggle from "./ContentValidationToggle";
import useDialogA11y from "../hooks/useDialogA11y";

function _readAccount() {
  try {
    const u = JSON.parse(localStorage.getItem("genly_user") || "null");
    return {
      tenantId: u?.tenant_id || null,
      billingGroup: u?.billing_group || null,
    };
  } catch {
    return { tenantId: null, billingGroup: null };
  }
}

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Modal para crear una variante de un job aprobado. La variante reusa
 * audio + segments (lyrics) del padre y re-genera SOLO el background con
 * un Veo prompt fresco. Cuenta como un video nuevo del plan.
 *
 * UX:
 *  - Muestra el contexto del padre (artista - canción) para que el
 *    operador sepa de qué video está creando variante.
 *  - El campo `background_hint` es el principal driver creativo (reusa
 *    el sub-componente compartido del PR #116).
 *  - El `concept` aparece pre-filleado con el del padre, editable.
 *  - Warning de costo explícito antes del CTA — el operador firma
 *    "cuesta 1 video del plan" antes de gatillar la regen.
 *
 * Props:
 *   job        — el job padre (done/approved) cuyo segments_json se reusará
 *   onClose    — fn() al cerrar modal sin crear
 *   onCreated  — fn(newJobId) tras crear con éxito; el caller navega
 */
export default function VariantCreateModal({ job, onClose, onCreated }) {
  const { t } = useI18n();
  const initialConcept = job?.render_params?.concept || "";
  // Pre-fill background_hint with the last prompt the operator tried for
  // a variant of THIS parent. Use case: the previous variant failed
  // validation_failed (e.g. "rock guitarist hands" blocked by Guideline
  // 15). Operator closes the modal, comes back to "Crear variante",
  // expects to see their work; previously we reset to "". Now we read
  // localStorage keyed by parent_job_id.
  //
  // Saved on every submit attempt (even before fetch resolves) so the
  // prompt survives a fail. Cleared after success.
  const _hintCacheKey = `genly_variant_hint_${job?.job_id || ""}`;
  const _initialHint = (() => {
    try {
      return localStorage.getItem(_hintCacheKey) || "";
    } catch {
      return "";
    }
  })();
  const [backgroundHint, setBackgroundHint] = useState(_initialHint);
  const [concept, setConcept] = useState(initialConcept);
  // Tenant-aware content-validation choice. See EditRequestPanel for
  // the full rationale. value=true means "run validator". Universal is fixed;
  // common accounts can explicitly choose the existing fondo-libre mode.
  const { tenantId: _tenantId, billingGroup: _billingGroup } = _readAccount();
  const [validationEnabled, setValidationEnabled] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  // Overage confirmation gate. The backend replies 402
  // `variant_overage_unconfirmed` when this song already has
  // VARIANT_INCLUDED_PER_SONG (3) rendered versions — the 4th+ costs
  // extra. Instead of dumping the raw JSON error, we stash the detail
  // here and render an explicit confirm step; confirming re-submits with
  // `acknowledge_variant_overage: true`. See backend main.py create_variant.
  const [overage, setOverage] = useState(null);
  // Guard sincrónico contra doble click (mismo patrón que EditRequestPanel
  // que aprendimos en PR #106 — sin esto un click rápido crea 2 variantes
  // y quema 2 videos del plan).
  const submitLockRef = useRef(false);
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);
  const dialogRef = useDialogA11y({ onClose, closeOnEscape: !submitting });

  // opts.acknowledgeOverage — set true when the operator confirms the
  // paid-overage step, so we re-send with the ack flag the backend wants.
  const submit = async (opts = {}) => {
    if (submitLockRef.current || submitting) return;
    submitLockRef.current = true;
    setError(null);
    setOverage(null);
    setSubmitting(true);

    const payload = {};
    if (opts.acknowledgeOverage) {
      payload.acknowledge_variant_overage = true;
    }
    const hint = backgroundHint.trim();
    if (hint) payload.background_hint = hint;
    // Stash the hint as the operator submits so it survives a failed
    // variant (validation_failed, server restart, network blip). Cleared
    // after success in the .ok branch below. We persist BEFORE the
    // fetch so even a network error can't lose the work.
    try {
      if (hint) {
        localStorage.setItem(_hintCacheKey, hint);
      } else {
        localStorage.removeItem(_hintCacheKey);
      }
    } catch {
      // localStorage disabled (private window, quota, etc.) — non-fatal.
    }
    // concept: solo lo mandamos si el operador lo cambió. Si dejó el
    // del padre tal cual, no lo mandamos — el backend hereda.
    const conceptTrimmed = concept.trim();
    if (conceptTrimmed && conceptTrimmed !== initialConcept.trim()) {
      payload.concept = conceptTrimmed;
    }
    // Translate validation choice → backend flag. Send the flag based
    // purely on the operator's intent (validationEnabled), NOT on the
    // frontend's tenant detection. If `_readTenant()` returns null/stale
    // (e.g. localStorage was wiped, old login pre-tenant-schema), the
    // tenant-conditional version silently dropped BOTH flags and the
    // backend defaulted to UMG-validate. Real incident 2026-05-19:
    // operator picked "fondo libre", job validated and failed anyway.
    //
    // Backend gating (pipeline.py) remains authoritative: Universal always
    // validates; elsewhere, bypass only permits people when this operation's
    // prompt explicitly requests them. Sending one flag here records the
    // current intent and prevents a stale opposite choice from surviving.
    if (!validationEnabled) {
      payload.bypass_content_validation = true;
    } else {
      payload.force_content_validation = true;
    }

    try {
      const res = await fetch(`${API}/jobs/${job.job_id}/variant`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        let detail = body.detail;
        // Paid-overage gate: the song already has the plan's included
        // versions, so the 4th+ costs extra. This is NOT a hard error —
        // surface an explicit confirm step (rendered below) instead of the
        // red error box, and let the operator proceed. On confirm we
        // re-submit with acknowledge_variant_overage: true.
        if (
          res.status === 402 &&
          detail && typeof detail === "object" &&
          detail.code === "variant_overage_unconfirmed"
        ) {
          if (mountedRef.current) {
            setOverage(detail);
            submitLockRef.current = false;
            setSubmitting(false);
          }
          return;
        }
        // Pydantic v2 422 returns `detail` as an array of error objects
        // ([{type, loc, msg, input}, ...]). String-coercing that array
        // gives "[object Object]" — render the msg(s) instead. Structured
        // dict errors carry human text under `message` (FastAPI) or `msg`;
        // prefer those over dumping the whole JSON blob. Other shapes
        // (string, missing) fall through.
        if (Array.isArray(detail)) {
          detail = detail
            .map((e) => (e && typeof e === "object" && e.msg) ? e.msg : String(e))
            .join("; ");
        } else if (detail && typeof detail === "object") {
          detail = detail.message || detail.msg || JSON.stringify(detail);
        }
        throw new Error(detail || `Error ${res.status}`);
      }
      const data = await res.json();
      // Variant enqueued successfully — clear the cached prompt so the
      // next "Crear variante" opens fresh. We don't wait for the variant
      // to actually finish rendering; queueing is success enough for UX
      // purposes (operator already committed to this prompt).
      try {
        localStorage.removeItem(_hintCacheKey);
      } catch { /* ignore */ }
      if (mountedRef.current) {
        onCreated?.(data.job_id);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message || String(err));
        submitLockRef.current = false;
        setSubmitting(false);
      }
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={(e) => { if (e.target === e.currentTarget && !submitting) onClose?.(); }}
    >
      <div ref={dialogRef} tabIndex={-1} className="w-full max-w-lg bg-surface-2 rounded-card ring-1 ring-white/[0.08] p-6 space-y-4" role="dialog" aria-modal="true" aria-labelledby="variant-modal-title">
        <div>
          <h2 id="variant-modal-title" className="text-lg font-semibold text-white">
            {t("variant.title") || "Crear variante"}
          </h2>
          <p className="text-xs text-ink-secondary mt-1">
            {t("variant.subtitle") ||
              "Mismo audio y lyrics, nuevo background. Cuenta como un video del plan."}
          </p>
        </div>

        <div className="text-[11px] text-ink-tertiary px-3 py-2 rounded-md bg-surface-3/40 ring-1 ring-white/[0.04]">
          <span className="text-ink-secondary">
            {t("variant.source_label") || "Variante de:"}
          </span>{" "}
          <span className="text-white font-medium">
            {job.artist} {job.song_title ? `— ${job.song_title}` : ""}
          </span>
        </div>

        <BackgroundHintField
          value={backgroundHint}
          onChange={setBackgroundHint}
          disabled={submitting}
        />

        <div>
          <label className="block text-[11px] text-ink-secondary mb-1.5 tracking-wide">
            {t("variant.concept_label") || "Concept (opcional, editable)"}
          </label>
          <textarea
            value={concept}
            onChange={(e) => setConcept(e.target.value.slice(0, 2000))}
            placeholder={t("variant.concept_placeholder") ||
              "Si dejás vacío, el sistema arma el concept desde lyrics y género."}
            rows={2}
            disabled={submitting}
            className="w-full text-xs px-3 py-2 rounded-md bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none resize-none text-white placeholder:text-ink-tertiary disabled:opacity-50"
          />
          <p className="text-[10px] text-ink-tertiary mt-1 font-mono tabular-nums text-right">
            {concept.length}/2000
          </p>
        </div>

        <ContentValidationToggle
          value={validationEnabled}
          onChange={setValidationEnabled}
          tenantId={_tenantId}
          billingGroup={_billingGroup}
          disabled={submitting}
        />

        <div className="p-3 rounded-xl bg-accent/[0.06] ring-1 ring-accent/25">
          <p className="text-xs text-white font-medium mb-1">
            {t("variant.cost_title") || "Cuesta 1 video de tu plan"}
          </p>
          <p className="text-[11px] text-ink-secondary leading-relaxed">
            {t("variant.cost_desc") ||
              "La variante es un job nuevo: pasa por review como cualquier upload. Se cobra 1 video al plan. Las lyrics aprobadas se mantienen idénticas."}
          </p>
        </div>

        {error && (
          <div className="text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
            {error}
          </div>
        )}

        {overage && (
          <div className="p-3 rounded-xl bg-amber-500/[0.08] ring-1 ring-amber-500/30 space-y-1">
            <p className="text-xs text-amber-200 font-medium">
              {t("variant.overage_title") || "Versión adicional — se factura extra"}
            </p>
            <p className="text-[11px] text-ink-secondary leading-relaxed">
              {(t("variant.overage_desc") ||
                "Esta canción ya tiene {existing} versiones (incluida la original). El plan incluye {included} por canción; a partir de la próxima se factura ${cost} adicional al cierre del mes.")
                .replace("{existing}", overage.existing_renders ?? "—")
                .replace("{included}", overage.included_per_song ?? "—")
                .replace("{cost}", typeof overage.cost_extra_usd === "number"
                  ? overage.cost_extra_usd.toFixed(2)
                  : (overage.cost_extra_usd ?? "—"))}
            </p>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 rounded-md text-sm text-ink-secondary hover:text-white hover:bg-surface-3/40 disabled:opacity-50 transition-colors"
          >
            {t("variant.cancel") || "Cancelar"}
          </button>
          {overage ? (
            <button
              type="button"
              onClick={() => submit({ acknowledgeOverage: true })}
              disabled={submitting}
              className="px-4 py-2 rounded-md text-sm font-medium text-white bg-amber-600 hover:bg-amber-500 ring-1 ring-amber-500/40 transition-colors disabled:opacity-60"
            >
              {submitting
                ? (t("variant.creating") || "Creando…")
                : (t("variant.overage_confirm") || "Confirmar y crear (extra)")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => submit()}
              disabled={submitting}
              className="px-4 py-2 rounded-md text-sm font-medium text-white bg-accent hover:bg-accent/90 ring-1 ring-accent/30 transition-colors disabled:opacity-60"
            >
              {submitting
                ? (t("variant.creating") || "Creando…")
                : (t("variant.create") || "Crear variante")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
