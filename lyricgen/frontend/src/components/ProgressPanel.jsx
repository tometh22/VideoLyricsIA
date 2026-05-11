import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import {
  stepLabel,
  stepHint,
  stepExpectedSeconds,
  formatElapsed,
  elapsedSinceLabel,
} from "../progressSteps";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Single source of truth for the "your job is still cooking" UX.
 *
 * Replaces the old in-place `if (isEditing) { return <div>...` block in
 * JobDetail. Now covers BOTH:
 *   - status="processing": fresh render from scratch (used to dead-end
 *     to "Todavía no se puede previsualizar" — the bug that prompted
 *     this whole feature).
 *   - status="editing":   partial re-render with edited lyrics/font.
 *
 * Key UX behaviours:
 *   1. Real step name in Spanish from progressSteps.STEP_META, not
 *      the raw machine key.
 *   2. "Empezó hace X" so the user can see at a glance whether the
 *      render is fresh or been running an hour.
 *   3. Soft "this is taking longer than usual" banner once we cross
 *      2× the step's p75 ETA without progress, with a hard
 *      "Mark as error" button.
 *   4. Force-fail flow uses POST /jobs/{id}/force-fail (added in this
 *      same PR backend-side). Flips the row to status=error so the
 *      Reintentar button appears in the parent JobDetail.
 *
 * Render contract: the parent decides when to show this — pass a job
 * where status is one of {processing, editing}. We don't gate that
 * here so the parent keeps full control over routing.
 */
export default function ProgressPanel({ job, onBack, onForceFail }) {
  const { t } = useI18n();
  const isEditing = job.status === "editing";

  // Track when progress LAST changed so we can detect "stuck". The
  // worker writes step+progress updates as it advances; if nothing
  // changes for 2× the step's expected duration, surface the banner.
  // We compare against the live `current_step + progress` so a "stuck"
  // streak resets the moment the next update lands.
  const lastSig = useRef(`${job.current_step}::${job.progress}`);
  const lastChangeAt = useRef(Date.now());
  const sig = `${job.current_step}::${job.progress}`;
  if (sig !== lastSig.current) {
    lastSig.current = sig;
    lastChangeAt.current = Date.now();
  }

  // Re-render every 5 s so the elapsed/stuck timers update without
  // requiring a parent re-render. Cheap — just bumps a counter.
  const [, setTick] = useState(0);
  useEffect(() => {
    const iv = setInterval(() => setTick((n) => n + 1), 5000);
    return () => clearInterval(iv);
  }, []);

  const idleSecs = (Date.now() - lastChangeAt.current) / 1000;
  const expected = stepExpectedSeconds(job.current_step);
  // 2× expected = "this is taking longer than usual" threshold. Avoids
  // false alarms on the first minute of any step.
  const isStalled = idleSecs > Math.max(60, 2 * expected);

  // Force-fail action — the escape hatch when the user doesn't want
  // to wait for the reaper. Calls back to the parent so it can update
  // state + show the retry path.
  const [forcing, setForcing] = useState(false);
  const handleForceFail = async () => {
    if (forcing) return;
    if (!window.confirm(
      "Esto va a marcar el job como error para que aparezca el botón " +
      "Reintentar. ¿Seguro?"
    )) return;
    setForcing(true);
    try {
      const res = await fetch(`${API}/jobs/${job.job_id}/force-fail`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
      });
      if (!res.ok) {
        alert(`No se pudo marcar como error (HTTP ${res.status}).`);
        setForcing(false);
        return;
      }
      const updated = await res.json();
      if (onForceFail) onForceFail(updated);
    } catch {
      alert("Error de red al marcar como error.");
      setForcing(false);
    }
  };

  // Title varies by flow: editing keeps the existing wording (it's a
  // re-render with semantic context), processing gets a generic
  // "your video is being made" line.
  const title = isEditing
    ? (t("edit.in_progress_title") || "Aplicando tus cambios...")
    : (t("progress.title") || "Generando tu video...");

  // For editing we keep the existing per-edit hint (background vs
  // typography re-render); for processing we use the step-derived hint.
  const hint = isEditing
    ? (job.current_step === "background"
        ? (t("edit.in_progress_bg") || "Generando nuevo fondo con Veo · mantiene lyrics y tiempos · ~10-15 min")
        : (t("edit.in_progress_typo") || "Re-renderizando con la tipografía nueva · usa el fondo cacheado · ~5-10 min"))
    : stepHint(job.current_step);

  const name = (job.filename || "").replace(/\.mp3$/i, "");

  return (
    <div className="w-full max-w-2xl animate-fade-in">
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={onBack}
          className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors"
          aria-label="Volver"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h2 className="text-xl font-bold">{name}</h2>
          <p className="text-sm text-gray-500">
            {job.artist}
            {job.created_at && <span className="ml-2 text-gray-600">· empezó {elapsedSinceLabel(job.created_at)}</span>}
          </p>
        </div>
      </div>

      {/* Main progress card */}
      <div className="rounded-card p-5 bg-brand/[0.08] ring-1 ring-brand/25">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-brand/15 ring-1 ring-brand/30 flex items-center justify-center shrink-0">
            <span className="w-4 h-4 border-2 border-brand-light border-t-transparent rounded-full animate-spin" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-white">{title}</p>
            <p className="text-xs text-ink-secondary mt-0.5">{hint}</p>

            <div className="mt-3 h-1.5 rounded-full bg-surface-3/60 overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
                style={{ width: `${Math.min(100, Math.max(3, job.progress || 0))}%` }}
              />
            </div>
            <p className="text-[10px] text-gray-500 mt-1 font-mono">
              {stepLabel(job.current_step)} · {job.progress || 0}%
              {idleSecs > 30 && (
                <span className="ml-2 text-gray-600">
                  · sin avance hace {formatElapsed(idleSecs)}
                </span>
              )}
            </p>

            <p className="text-[11px] text-gray-500 mt-3 leading-relaxed">
              {isEditing
                ? (t("edit.no_video_during_editing") || "El video viejo se está reemplazando con tus cambios. Cuando termine vas a poder verlo acá.")
                : (t("progress.live_update_hint") || "Esta pantalla se actualiza sola — no hace falta refrescar.")}
            </p>
          </div>
        </div>
      </div>

      {/* Stalled banner — appears only after 2× expected step duration */}
      {isStalled && (
        <div className="mt-4 rounded-card p-4 bg-amber-500/[0.08] ring-1 ring-amber-500/25">
          <div className="flex items-start gap-3">
            <div className="w-7 h-7 rounded-lg bg-amber-500/15 ring-1 ring-amber-500/30 flex items-center justify-center shrink-0">
              <svg className="w-3.5 h-3.5 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-amber-200">
                Esto está tardando más de lo usual
              </p>
              <p className="text-xs text-amber-200/70 mt-0.5">
                El paso <span className="font-mono">{stepLabel(job.current_step)}</span> suele tardar
                ~{formatElapsed(expected)} y lleva {formatElapsed(idleSecs)} sin avanzar.
                Si pasa de 15 min sin movimiento, el sistema lo marca como error solo.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  onClick={handleForceFail}
                  disabled={forcing}
                  className="text-xs px-3 h-8 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 ring-1 ring-amber-500/40 text-amber-100 disabled:opacity-50 transition-colors"
                >
                  {forcing ? "Marcando..." : "Marcar como error ahora"}
                </button>
                <p className="text-[11px] text-amber-200/50 self-center">
                  El MP3 queda guardado, podés Reintentar después
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
