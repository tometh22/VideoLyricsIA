import { useRef, useState } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Same font list the operator chose during upload — keeps the cached
// background usable (we only render new typography over the existing video).
const FONTS = [
  { code: "",                label: "Auto" },
  { code: "jost-bold",       label: "Jost (estilo Futura)" },
  { code: "montserrat-bold", label: "Montserrat" },
  { code: "poppins-bold",    label: "Poppins" },
  { code: "outfit-bold",     label: "Outfit (estilo Gilroy)" },
  { code: "roboto-bold",     label: "Roboto" },
  { code: "bebas-neue",      label: "Bebas Neue" },
  { code: "oswald-bold",     label: "Oswald" },
  { code: "anton",           label: "Anton" },
];

const CASE_OPTS = [
  { code: "upper",    d: "MAY", label: "Todo en MAYÚSCULAS" },
  { code: "title",    d: "Aa",  label: "Primera letra de Cada Palabra" },
  { code: "lower",    d: "min", label: "todo en minúsculas" },
  { code: "original", d: "ori", label: "Sin cambios" },
];

const TRANSITION_OPTS = [
  { code: "cut",  label: "Corte (instantáneo)" },
  { code: "fade", label: "Fade" },
  { code: "slow", label: "Fade lento" },
];

const MOTION_OPTS = [
  { code: "none",   label: "Estático" },
  { code: "subtle", label: "Movimiento sutil" },
  { code: "float",  label: "Flotante" },
];

const SCALE_STEPS = [0.8, 1.0, 1.2, 1.5, 1.8, 2.0];

export default function EditRequestPanel({ job, onEditTriggered }) {
  const { t } = useI18n();
  const editCount = job.edit_count ?? 0;
  const editsRemaining = job.edits_remaining ?? Math.max(0, 3 - editCount);
  const initialParams = job.render_params || {};

  const [mode, setMode] = useState(null); // null | "typography" | "background"
  const [form, setForm] = useState({
    font:             initialParams.font             ?? "",
    font_scale:       initialParams.font_scale       ?? 1.0,
    text_case:        initialParams.text_case        ?? "upper",
    lyric_transition: initialParams.lyric_transition ?? "cut",
    text_motion:      initialParams.text_motion      ?? "none",
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  // Synchronous guard against double-click. `submitting` is async (React
  // schedules the re-render after the click handler returns) so a rapid
  // second click can fire its handler before the disabled flag flips.
  // The ref is set BEFORE any await so the second handler sees
  // `current=true` immediately and bails. Mirrors the approveLockRef
  // pattern used in JobDetail.jsx.
  const submitLockRef = useRef(false);

  const limitReached = editsRemaining <= 0;

  // Only send the fields the operator actually changed — the backend
  // treats missing fields as "keep the prior value".
  const buildPayload = (type) => {
    if (type === "background") {
      return { edit_type: "background" };
    }
    const p = { edit_type: "typography" };
    if (form.font             !== (initialParams.font             ?? "")) p.font = form.font;
    if (form.font_scale       !== (initialParams.font_scale       ?? 1.0)) p.font_scale = form.font_scale;
    if (form.text_case        !== (initialParams.text_case        ?? "upper")) p.text_case = form.text_case;
    if (form.lyric_transition !== (initialParams.lyric_transition ?? "cut")) p.lyric_transition = form.lyric_transition;
    if (form.text_motion      !== (initialParams.text_motion      ?? "none")) p.text_motion = form.text_motion;
    return p;
  };

  const submit = async (type) => {
    if (submitLockRef.current || limitReached) return;
    submitLockRef.current = true;

    // Defensive: catch the "user clicked submit without changing
    // anything" case BEFORE hitting the API. Otherwise the backend
    // happily re-renders with identical params, the user waits ~5min
    // for the same video, and burns one of their 3 edits.
    const payload = buildPayload(type);
    if (type === "typography" && Object.keys(payload).length === 1) {
      setError(t("edit.no_changes") || "No cambiaste ninguna opción — no hay nada que re-renderizar.");
      submitLockRef.current = false;
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${API}/edit/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data.detail || `Error ${res.status}`);
        return;
      }
      // Server flipped status to "editing" — close the panel, let parent
      // pick up the new state via its /status poll.
      setMode(null);
      if (onEditTriggered) onEditTriggered(data);
    } catch (e) {
      setError(e?.message || "Network error");
    } finally {
      submitLockRef.current = false;
      setSubmitting(false);
    }
  };

  if (limitReached) {
    return (
      <div className="rounded-card p-4 mb-4 bg-surface-2/40 ring-1 ring-white/[0.04] animate-fade-in">
        <div className="flex items-start gap-3">
          <div className="w-8 h-8 rounded-lg bg-amber-500/15 ring-1 ring-amber-500/30 flex items-center justify-center shrink-0">
            <svg className="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-white">
              {t("edit.limit_reached_title") || "Ya pediste 3 ediciones"}
            </p>
            <p className="text-xs text-ink-secondary mt-0.5">
              {t("edit.limit_reached_desc") || "Aprobá o rechazá el video. Si todavía no estás conforme, rechazá y empezá un nuevo job."}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-card p-5 mb-4 bg-surface-2/40 ring-1 ring-white/[0.05] animate-fade-in" data-tour="jobdetail-edit-panel">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">
            {t("edit.panel_title") || "¿Necesitás ajustes?"}
          </h3>
          <p className="text-xs text-ink-secondary mt-0.5">
            {t("edit.panel_desc") || "Cambiá tipografía o regenerá el fondo sin volver a transcribir."}
          </p>
        </div>
        <span className="text-[11px] font-mono text-ink-secondary px-2 py-1 rounded-md bg-surface-3/60 ring-1 ring-white/[0.04] shrink-0">
          {editsRemaining === 1
            ? (t("edit.remaining_one") || "1 ed. restante")
            : `${editsRemaining} ${t("edit.remaining_many") || "ed. restantes"}`}
        </span>
      </div>

      {!mode && (
        <div className="grid sm:grid-cols-2 gap-3">
          <button
            type="button"
            onClick={() => setMode("typography")}
            className="text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-brand/30 transition-all"
          >
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M4 7V4h16v3M9 20h6M12 4v16" strokeLinecap="round" />
              </svg>
              <span className="text-sm font-medium text-white">
                {t("edit.typography_title") || "Cambiar tipografía"}
              </span>
            </div>
            <p className="text-[11px] text-ink-secondary">
              {t("edit.typography_cost") || "~5-10 min · sin costo extra · reutiliza el fondo actual"}
            </p>
          </button>

          <button
            type="button"
            onClick={() => setMode("background")}
            className="text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-accent/30 transition-all"
          >
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 16l5-5 4 4 5-5 4 4" />
              </svg>
              <span className="text-sm font-medium text-white">
                {t("edit.background_title") || "Regenerar fondo"}
              </span>
            </div>
            <p className="text-[11px] text-ink-secondary">
              {t("edit.background_cost") || "~10-15 min · ~US$0.90 · nuevo Veo manteniendo lyrics"}
            </p>
          </button>
        </div>
      )}

      {mode === "typography" && (
        <div className="space-y-3 animate-fade-in">
          {/* Font */}
          <div>
            <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
              {t("upload.font_label") || "Fuente"}
            </label>
            <select
              value={form.font}
              onChange={(e) => setForm({ ...form, font: e.target.value })}
              className="input-field text-sm w-full"
            >
              {FONTS.map((f) => (
                <option key={f.code} value={f.code}>{f.label}</option>
              ))}
            </select>
          </div>

          {/* Font scale */}
          <div>
            <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
              {t("upload.font_scale_label") || "Tamaño"} · <span className="font-mono text-white">{form.font_scale.toFixed(1)}×</span>
            </label>
            <div className="flex gap-1">
              {SCALE_STEPS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setForm({ ...form, font_scale: s })}
                  className={`flex-1 py-2 rounded-md text-[11px] font-mono font-bold transition-all
                    ${form.font_scale === s
                      ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                      : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                    }`}
                >{s.toFixed(1)}×</button>
              ))}
            </div>
          </div>

          {/* Case */}
          <div>
            <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
              {t("upload.text_case_label") || "Caja"}
            </label>
            <div className="flex gap-1">
              {CASE_OPTS.map((o) => (
                <button
                  key={o.code}
                  type="button"
                  title={o.label}
                  onClick={() => setForm({ ...form, text_case: o.code })}
                  className={`flex-1 py-2 rounded-md text-[11px] font-mono font-bold transition-all
                    ${form.text_case === o.code
                      ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                      : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                    }`}
                >{o.d}</button>
              ))}
            </div>
          </div>

          {/* Transition */}
          <div>
            <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
              {t("upload.transition_label") || "Transición entre líneas"}
            </label>
            <div className="flex gap-1">
              {TRANSITION_OPTS.map((o) => (
                <button
                  key={o.code}
                  type="button"
                  onClick={() => setForm({ ...form, lyric_transition: o.code })}
                  className={`flex-1 py-2 rounded-md text-[11px] font-medium transition-all
                    ${form.lyric_transition === o.code
                      ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                      : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                    }`}
                >{o.label}</button>
              ))}
            </div>
          </div>

          {/* Motion */}
          <div>
            <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
              {t("upload.motion_label") || "Movimiento del texto"}
            </label>
            <div className="flex gap-1">
              {MOTION_OPTS.map((o) => (
                <button
                  key={o.code}
                  type="button"
                  onClick={() => setForm({ ...form, text_motion: o.code })}
                  className={`flex-1 py-2 rounded-md text-[11px] font-medium transition-all
                    ${form.text_motion === o.code
                      ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                      : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                    }`}
                >{o.label}</button>
              ))}
            </div>
          </div>

          {error && (
            <div className="text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
              {error}
            </div>
          )}

          <div className="flex gap-2 pt-1">
            <button
              type="button"
              onClick={() => { setMode(null); setError(null); }}
              disabled={submitting}
              className="btn-secondary h-10 px-4 text-xs disabled:opacity-50"
            >
              {t("edit.cancel") || "Cancelar"}
            </button>
            <button
              type="button"
              onClick={() => submit("typography")}
              disabled={submitting}
              className="flex-1 btn-primary h-10 px-4 text-xs disabled:opacity-50"
            >
              {submitting ? (
                <span className="inline-flex items-center gap-2">
                  <span className="w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  {t("edit.submitting") || "Enviando..."}
                </span>
              ) : (t("edit.typography_submit") || "Pedir re-render con estos cambios")}
            </button>
          </div>
        </div>
      )}

      {mode === "background" && (
        <div className="space-y-3 animate-fade-in">
          <div className="p-3 rounded-xl bg-accent/[0.06] ring-1 ring-accent/25">
            <p className="text-xs text-white font-medium mb-1">
              {t("edit.background_confirm_title") || "Confirmá regenerar el fondo"}
            </p>
            <p className="text-[11px] text-ink-secondary leading-relaxed">
              {t("edit.background_confirm_desc") || "Genera un fondo nuevo con Veo manteniendo las lyrics y los tiempos. Cuesta ~US$0.90 (Veo) y tarda ~10-15 min. La tipografía actual se mantiene."}
            </p>
          </div>

          {error && (
            <div className="text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
              {error}
            </div>
          )}

          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => { setMode(null); setError(null); }}
              disabled={submitting}
              className="btn-secondary h-10 px-4 text-xs disabled:opacity-50"
            >
              {t("edit.cancel") || "Cancelar"}
            </button>
            <button
              type="button"
              onClick={() => submit("background")}
              disabled={submitting}
              className="flex-1 btn-primary h-10 px-4 text-xs disabled:opacity-50 !bg-accent hover:!bg-accent/90"
            >
              {submitting ? (
                <span className="inline-flex items-center gap-2">
                  <span className="w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  {t("edit.submitting") || "Enviando..."}
                </span>
              ) : (t("edit.background_submit") || "Regenerar fondo (~US$0.90)")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
