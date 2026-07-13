import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BackgroundHintField from "./BackgroundHintField";
import ContentValidationToggle, { isUmgTenant } from "./ContentValidationToggle";
import { useAlert } from "./AlertProvider";

function _readTenant() {
  try {
    const u = JSON.parse(localStorage.getItem("genly_user") || "null");
    return u?.tenant_id || null;
  } catch {
    return null;
  }
}

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function EditRequestPanel({
  job,
  onEditTriggered,
  // Which edit modes the user can pick from. Defaults to all three so
  // the existing pending_review call sites keep working unchanged. When
  // a job is in done/rejected, JobDetail narrows this to ["lyrics"] so
  // the user can fix typos but can't trigger fresh Veo regens or
  // typography re-renders on already-approved/rejected videos.
  allowedModes = ["lyrics", "background"],
  // Callback que dispara el flow de edición de lyrics. JobDetail navega
  // al Studio Console (/videos/:id/edit-lyrics) — el modal interno fue
  // eliminado. El panel sigue siendo dueño del modo "background".
  onLyricsClick,
}) {
  const allowsLyrics = allowedModes.includes("lyrics");
  const allowsBackground = allowedModes.includes("background");
  const { t } = useI18n();
  const { alert } = useAlert();
  const editCount = job.edit_count ?? 0;
  const editsRemaining = job.edits_remaining ?? Math.max(0, 3 - editCount);
  // Admins have no edit cap (backend bypasses it); the panel shows
  // "sin límite" and never gates on editsRemaining.
  const editLimitExempt = job.edit_limit_exempt ?? false;
  const initialParams = job.render_params || {};

  // El modo "lyrics" se delegó al Studio Console (ruta /videos/:id/
  // edit-lyrics) — este panel ya no monta el editor de letras inline.
  // Mantenemos null | "background" para el flow de regeneración de fondo
  // (Veo/Imagen + hint + movement_style) que SÍ vive acá.
  const [mode, setMode] = useState(null); // null | "background"
  // Operator-typed background hint for edit_type="background". Empty
  // string when the operator hasn't typed anything (we send no field in
  // that case and the pipeline falls back to Gemini's lyrics-only
  // analysis with the debiased system prompt + 3 contrastive examples).
  const [backgroundHint, setBackgroundHint] = useState("");
  // Generation mode for the background regen. "veo" (default) = Veo 3.1
  // cinematic video; "imagen" = Imagen-4 still + local Ken Burns animation.
  // Operator picks via the segmented toggle inside the background panel.
  // Default "veo" preserves the prior behavior of every edit pre-2026-05-16.
  const [backgroundMode, setBackgroundMode] = useState("veo");
  // Camera/motion register for the background regen (incl. "estatico" =
  // locked camera). Pre-filled from the job's persisted choice so the editor
  // reflects what the wizard picked — the upload→edit flow shouldn't forget
  // the operator's decision. "" = Auto (system varies per song).
  const [movementStyle, setMovementStyle] = useState(initialParams.movement_style ?? "");
  // "Usar mi prompt tal cual" — bypass Gemini's rewrite, send the hint
  // straight to Veo. Pre-filled from render_params; only meaningful in
  // Veo mode (Imagen renders a still, no verbatim camera negatives).
  const [bgVerbatim, setBgVerbatim] = useState(!!initialParams.bg_verbatim);
  // Movement options mirror the wizard's MOVEMENT_STYLES (kept in sync).
  const MOVEMENT_OPTIONS = [
    { code: "",              label: t("upload.movement_auto") || "Auto" },
    { code: "estatico",      label: t("upload.movement_estatico") || "Estático (cámara fija)" },
    { code: "sutil",         label: t("upload.movement_sutil") || "Sutil" },
    { code: "estandar",      label: t("upload.movement_estandar") || "Estándar" },
    { code: "foto-parallax", label: t("upload.movement_foto_parallax") || "Foto + parallax" },
    { code: "animado",       label: t("upload.movement_animado") || "Animado" },
  ];
  // Tenant-aware content-validation toggle. Boolean semantics:
  // value=true  → operator wants validator to run
  // value=false → operator wants validator skipped
  // Default per tenant: UMG tenants validate, others skip.
  // Mapped to bypass_content_validation OR force_content_validation in
  // the payload depending on which direction departs from tenant default.
  const _tenantId = _readTenant();
  const _isUmg = isUmgTenant(_tenantId);
  const [validationEnabled, setValidationEnabled] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  // Synchronous guard against double-click. `submitting` is async (React
  // schedules the re-render after the click handler returns) so a rapid
  // second click can fire its handler before the disabled flag flips.
  // The ref is set BEFORE any await so the second handler sees
  // `current=true` immediately and bails. Mirrors the approveLockRef
  // pattern used in JobDetail.jsx.
  const submitLockRef = useRef(false);
  // The panel unmounts the instant submit() succeeds: onEditTriggered
  // flips job.status to "editing" upstream, the parent's isPendingReview
  // gate goes false, EditRequestPanel disappears from the tree. The
  // `finally` block below still runs setSubmitting(false) on an
  // unmounted component, which in prod React 18 manifests as Minified
  // Error #300 ("Maximum update depth exceeded") because the leftover
  // state update cascades through Suspense/StrictMode in unexpected
  // ways. Track mount state and skip leftover setState calls.
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  const limitReached = !editLimitExempt && editsRemaining <= 0;

  // Clear stale error banners when the job transitions into "editing" —
  // means the regen actually kicked off, so a previous failure message
  // should not linger above the in-progress UI.
  useEffect(() => {
    if (job.status === "editing" && error) setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.status]);

  // Map raw backend HTTPException details to friendly Spanish copy. If
  // the backend message doesn't match a known prefix we fall through to
  // the original `data.detail` so nothing gets swallowed silently.
  //
  // CRITICAL: this function MUST always return a string (or null). React
  // crashes with "Objects are not valid as a React child" (error #31)
  // when the returned value is rendered as `{error}` in JSX. Pydantic v2
  // returns `detail` as an array of {type, loc, msg, input} objects on
  // 422 — the prior version returned `raw` unchanged for non-strings,
  // which bombed the whole edit panel into the error boundary screen
  // (incident 2026-05-18, prod outage after #192 bump to 2000 chars).
  const translateBackendError = (raw) => {
    if (raw == null) return null;
    // Coerce any backend shape to a single user-facing string first.
    let str;
    if (typeof raw === "string") {
      str = raw;
    } else if (Array.isArray(raw)) {
      // Pydantic v2 422 shape — surface the msg(s) joined.
      str = raw
        .map((e) => (e && typeof e === "object" && e.msg) ? e.msg : String(e))
        .join("; ");
    } else if (typeof raw === "object") {
      str = raw.msg || raw.detail || JSON.stringify(raw);
    } else {
      str = String(raw);
    }
    if (str.startsWith("No cached background available")) {
      return t("edit.error_no_bg_cache") ||
        "Este video no tiene un fondo cacheado para reusar. Regenerá el fondo primero (cuesta ~US$0.90).";
    }
    if (str.startsWith("Job must be in pending_review")) {
      return t("edit.error_wrong_status") ||
        "Esta regeneración ya está en marcha o el video pasó a otro estado.";
    }
    if (str.startsWith("Maximum edit limit")) {
      return t("edit.error_limit_reached") ||
        "Alcanzaste el límite de 3 regeneraciones para este video.";
    }
    if (str.startsWith("Lyrics edit requires") || str.startsWith("Job has no persisted")) {
      return t("edit.error_no_segments") ||
        "Este video no tiene letras guardadas para editar. Subí la canción de nuevo.";
    }
    return str;
  };

  // Only send the fields the operator actually changed — the backend
  // treats missing fields as "keep the prior value". El path lyrics se
  // migró al Studio Console; este panel solo arma el payload de
  // background regen (Veo/Imagen + hint + movement_style + validation).
  const buildPayload = () => {
    const p = { edit_type: "background" };
    const hint = (backgroundHint || "").trim();
    if (hint) p.background_hint = hint;
    // Send mode explicitly only when non-default so older backends
    // that don't know the field still accept the payload.
    if (backgroundMode && backgroundMode !== "veo") {
      p.background_mode = backgroundMode;
    }
    // Camera/motion register. Always send (incl. "" = Auto) so the editor
    // can override a previously-persisted register, e.g. switch a drifting
    // background to a locked one.
    p.movement_style = movementStyle;
    // Verbatim only applies to Veo + a non-empty hint; never with Imagen.
    // ALWAYS send the boolean so unchecking the toggle clears a
    // previously-persisted True on the backend (symmetric with the backend
    // which always overwrites bg_verbatim for background edits).
    p.bg_verbatim = !!(bgVerbatim && hint && backgroundMode !== "imagen");
    // Always send one of the two flags based purely on operator intent.
    // The tenant-conditional version silently dropped BOTH flags when
    // frontend tenant detection failed (stale localStorage, old login)
    // and the backend defaulted to UMG-validate. See VariantCreateModal
    // for the full incident (2026-05-19).
    if (!validationEnabled) {
      p.bypass_content_validation = true;
    } else {
      p.force_content_validation = true;
    }
    return p;
  };

  // Single POST with 409 youtube_already_published retry handling.
  // Returns {ok, data, status, cancelled} so the caller can decide
  // what to do — setError on failure, propagate onEditTriggered on
  // success. cancelled=true means the operator declined the confirm.
  const postEditWithRetry = async (payload) => {
    let res = await fetch(`${API}/edit/${job.job_id}`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = await res.json().catch(() => ({}));
    if (
      res.status === 409 &&
      data?.detail?.code === "youtube_already_published"
    ) {
      const url = data.detail.youtube_url;
      const msg = (t("edit.youtube_drift_confirm") ||
        "Este video ya está publicado en YouTube. La re-sincronización actualizará el archivo en la plataforma pero NO reemplazará el video en YouTube (la API de YouTube no permite reemplazar archivos, solo metadata).\n\n¿Continuar igual?")
        + (url ? `\n\nYouTube: ${url}` : "");
      if (!window.confirm(msg)) {
        return { ok: false, cancelled: true };
      }
      res = await fetch(`${API}/edit/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, allow_youtube_drift: true }),
      });
      data = await res.json().catch(() => ({}));
    }
    return { ok: res.ok, status: res.status, data };
  };

  const submit = async () => {
    if (submitLockRef.current || limitReached) return;
    submitLockRef.current = true;

    const payload = buildPayload();
    if (mountedRef.current) setSubmitting(true);
    if (mountedRef.current) setError(null);
    let succeeded = false;
    try {
      const { ok, status, data, cancelled } = await postEditWithRetry(payload);
      if (cancelled) return;
      if (!ok) {
        const friendly = translateBackendError(data?.detail) || `Error ${status}`;
        if (mountedRef.current) setError(friendly);
        return;
      }
      succeeded = true;
      // IMPORTANT: clear UI state BEFORE notifying the parent.
      // onEditTriggered flips job.status="editing" upstream → parent
      // re-renders with isPendingReview=false → THIS component
      // unmounts. Any setState we'd queue after that lands on a dead
      // component and (in prod React 18) cascades into Minified Error
      // #300. We mutate refs (safe post-unmount) and SKIP the finally's
      // setSubmitting since mountedRef will be false by then.
      submitLockRef.current = false;
      if (mountedRef.current) {
        setMode(null);
        setSubmitting(false);
      }
      if (onEditTriggered) onEditTriggered(data);
    } catch (e) {
      if (mountedRef.current) setError(e?.message || "Network error");
    } finally {
      submitLockRef.current = false;
      // Only touch React state if we're still mounted. Success path
      // already cleared submitting above (and likely unmounted); error
      // path needs us to flip submitting back so the user can retry.
      if (!succeeded && mountedRef.current) {
        setSubmitting(false);
      }
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
          {editLimitExempt
            ? (t("edit.no_limit") || "sin límite")
            : editsRemaining === 1
            ? (t("edit.remaining_one") || "1 ed. restante")
            : `${editsRemaining} ${t("edit.remaining_many") || "ed. restantes"}`}
        </span>
      </div>

      {!mode && (
        <div className={`grid gap-3 ${
          allowedModes.length === 1 ? "" :
          allowedModes.length === 2 ? "sm:grid-cols-2" :
          "sm:grid-cols-3"
        }`}>
          {allowsLyrics && (
          <button
            type="button"
            onClick={() => { if (onLyricsClick) onLyricsClick(); }}
            className="text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-brand-light/30 transition-all"
          >
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 19V6l12-2v13M9 19a2 2 0 11-4 0 2 2 0 014 0zM21 17a2 2 0 11-4 0 2 2 0 014 0z" strokeLinecap="round" />
              </svg>
              <span className="text-sm font-medium text-white">
                {t("edit.wizard_title") || "Editar y re-renderizar"}
              </span>
            </div>
            <p className="text-[11px] text-ink-secondary">
              {t("edit.wizard_cost") ||
                "~5-10 min · sin costo extra · título, artista, letra, tipografía, timing — todo desde el wizard"}
            </p>
          </button>
          )}

          {allowsBackground && (
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
              {t("edit.background_cost") || "~10-15 min · ~US$0.90 · nuevo video cinemático manteniendo lyrics"}
            </p>
          </button>
          )}
        </div>
      )}

      {mode === "background" && (
        <div className="space-y-3 animate-fade-in">
          <div className="p-3 rounded-xl bg-accent/[0.06] ring-1 ring-accent/25">
            <p className="text-xs text-white font-medium mb-1">
              {t("edit.background_confirm_title") || "Confirmá regenerar el fondo"}
            </p>
            <p className="text-[11px] text-ink-secondary leading-relaxed">
              {backgroundMode === "imagen"
                ? (t("edit.background_confirm_desc_imagen") ||
                    "Genera un fondo nuevo de foto animada con zoom suave manteniendo las lyrics y los tiempos. Cuesta ~US$0.03 y tarda ~30 segundos. Sin riesgo de caras humanas en el fondo. La tipografía actual se mantiene.")
                : (t("edit.background_confirm_desc") ||
                    "Genera un fondo nuevo de video cinemático manteniendo las lyrics y los tiempos. Cuesta ~US$0.90 y tarda ~10-15 min. La tipografía actual se mantiene.")}
            </p>
          </div>

          {/* Segmented toggle for generation mode. Veo (default) gives
              cinematic camera moves; Imagen gives a controllable still
              + Ken Burns animation — cheaper, faster, no face-validation
              failures. Added 2026-05-16 after operator asked "qué pasa
              si en vez de video quiero foto + parallax". */}
          <div className="rounded-xl bg-surface-2/40 ring-1 ring-white/[0.05] p-1 flex gap-1">
            <button
              type="button"
              onClick={() => setBackgroundMode("veo")}
              disabled={submitting}
              className={`flex-1 px-3 py-2 rounded-lg text-label transition-colors flex flex-col items-center gap-0.5 ${
                backgroundMode === "veo"
                  ? "bg-brand/20 text-brand-light ring-1 ring-brand/30"
                  : "text-ink-secondary hover:text-white hover:bg-white/[0.04]"
              }`}
            >
              <span className="flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <rect x="2" y="6" width="14" height="12" rx="2" />
                  <path d="M16 10l5-3v10l-5-3z" />
                </svg>
                {t("edit.bg_mode_veo") || "Video cinematográfico"}
              </span>
              <span className="text-[9px] opacity-70">
                {t("edit.bg_mode_veo_hint") || "~15 min · cámaras y motion"}
              </span>
            </button>
            <button
              type="button"
              onClick={() => setBackgroundMode("imagen")}
              disabled={submitting}
              className={`flex-1 px-3 py-2 rounded-lg text-label transition-colors flex flex-col items-center gap-0.5 ${
                backgroundMode === "imagen"
                  ? "bg-brand/20 text-brand-light ring-1 ring-brand/30"
                  : "text-ink-secondary hover:text-white hover:bg-white/[0.04]"
              }`}
            >
              <span className="flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <circle cx="8.5" cy="8.5" r="1.5" />
                  <path d="M21 15l-5-5L5 21" />
                </svg>
                {t("edit.bg_mode_imagen") || "Foto animada"}
              </span>
              <span className="text-[9px] opacity-70">
                {t("edit.bg_mode_imagen_hint") || "~30s · zoom suave"}
              </span>
            </button>
          </div>

          {/* Camera/motion register — lets the operator change how the new
              background moves (incl. Estático = locked camera) without prose.
              Closes the gap where movement was only selectable in the wizard. */}
          <div>
            <p className="text-[10px] uppercase tracking-[0.18em] text-ink-secondary mb-1.5">
              {t("edit.movement_label") || "Movimiento de cámara"}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {MOVEMENT_OPTIONS.map((m) => (
                <button
                  key={m.code || "auto"}
                  type="button"
                  disabled={submitting}
                  onClick={() => setMovementStyle(m.code)}
                  className={`px-2.5 py-1.5 rounded-lg text-label transition-colors ${
                    movementStyle === m.code
                      ? "bg-brand/20 text-brand-light ring-1 ring-brand/30"
                      : "text-ink-secondary hover:text-white hover:bg-white/[0.04]"
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          <BackgroundHintField
            value={backgroundHint}
            onChange={setBackgroundHint}
            disabled={submitting}
          />

          {/* Verbatim toggle — only when there's a hint and we're in Veo mode.
              Imagen renders a still, so verbatim camera control is moot there. */}
          {(backgroundHint || "").trim() && backgroundMode !== "imagen" && (
            <label className="flex items-center gap-2.5 cursor-pointer px-1">
              <input
                type="checkbox"
                checked={!!bgVerbatim}
                onChange={(e) => setBgVerbatim(e.target.checked)}
                disabled={submitting}
                className="peer sr-only"
              />
              <div className="relative w-9 h-5 rounded-full bg-surface-3 peer-checked:bg-brand transition-colors duration-200 shrink-0">
                <div className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 peer-checked:translate-x-4" />
              </div>
              <span className="text-[11px] text-ink-secondary">
                {t("edit.bg_verbatim_label") || "Usar mi prompt tal cual (sin reescritura de IA)"}
              </span>
            </label>
          )}

          <ContentValidationToggle
            value={validationEnabled}
            onChange={setValidationEnabled}
            tenantId={_tenantId}
            disabled={submitting}
          />


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
              onClick={() => submit()}
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
