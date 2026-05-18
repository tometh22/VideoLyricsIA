import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BackgroundHintField from "./BackgroundHintField";
import LyricsEditor from "./LyricsEditor";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Same font list the operator chose during upload — keeps the cached
// background usable (we only render new typography over the existing video).
const FONTS = [
  { code: "",                label: "Auto",                      css: "'Montserrat', sans-serif" },
  { code: "jost-bold",       label: "Jost (estilo Futura)",      css: "'Jost', sans-serif" },
  { code: "montserrat-bold", label: "Montserrat",                css: "'Montserrat', sans-serif" },
  { code: "poppins-bold",    label: "Poppins",                   css: "'Poppins', sans-serif" },
  { code: "outfit-bold",     label: "Outfit (estilo Gilroy)",    css: "'Outfit', sans-serif" },
  { code: "roboto-bold",     label: "Roboto",                    css: "'Roboto', sans-serif" },
  { code: "bebas-neue",      label: "Bebas Neue",                css: "'Bebas Neue', sans-serif" },
  { code: "oswald-bold",     label: "Oswald",                    css: "'Oswald', sans-serif" },
  { code: "anton",           label: "Anton",                     css: "'Anton', sans-serif" },
];

const FONT_CSS_BY_CODE = FONTS.reduce((acc, f) => { acc[f.code] = f.css; return acc; }, {});

function applyCaseToPreview(text, caseCode) {
  if (caseCode === "upper") return text.toUpperCase();
  if (caseCode === "lower") return text.toLowerCase();
  if (caseCode === "title") return text.replace(/\b\w/g, (c) => c.toUpperCase());
  return text;
}

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
  // "float" temporarily hidden — per-frame position callable in moviepy
  // (pipeline.py:_text_position_func) blocks compositing optimizations,
  // making long songs hit the 20-min RQ timeout. Backend aliases any
  // float requests to "subtle" for safety. Will re-enable once we
  // refactor the text layer to ffmpeg overlay filters.
];

// Motion picker hidden hasta decidir qué animación implementar.
// Backend default queda en text_motion="none". Cambiar a true para
// re-mostrar el dropdown sin tocar nada más.
const SHOW_MOTION_PICKER = false;

const SCALE_STEPS = [0.8, 1.0, 1.2, 1.5, 1.8, 2.0];

export default function EditRequestPanel({
  job,
  onEditTriggered,
  // Which edit modes the user can pick from. Defaults to all three so
  // the existing pending_review call sites keep working unchanged. When
  // a job is in done/rejected, JobDetail narrows this to ["lyrics"] so
  // the user can fix typos but can't trigger fresh Veo regens or
  // typography re-renders on already-approved/rejected videos.
  allowedModes = ["typography", "lyrics", "background"],
}) {
  const allowsTypography = allowedModes.includes("typography");
  const allowsLyrics = allowedModes.includes("lyrics");
  const allowsBackground = allowedModes.includes("background");
  const { t } = useI18n();
  const editCount = job.edit_count ?? 0;
  const editsRemaining = job.edits_remaining ?? Math.max(0, 3 - editCount);
  const initialParams = job.render_params || {};

  const [mode, setMode] = useState(null); // null | "typography" | "background" | "lyrics"
  // Audio source URL for the full-editor modal. Fetched lazily when the
  // operator opens "Lyrics" — same MP3 the worker would use for a re-
  // render. Null while loading; non-null once /jobs/{id}/source-audio-url
  // responds; we surface the error inline if R2 has lost the input.
  const [lyricsAudioUrl, setLyricsAudioUrl] = useState(null);
  const [lyricsAudioError, setLyricsAudioError] = useState(null);
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
  // Latest segments from the nested LyricsEditor (updated synchronously
  // on every edit via `onEditedChange`). Held in a ref so buildPayload
  // can read it without re-renders. Used to include the operator's
  // pending text corrections in a background-regen POST, closing the
  // 3 s autosave race documented in the LyricsEditor comment above.
  // Null until the operator touches the editor; we keep it null when
  // untouched so we don't accidentally overwrite segments_json with a
  // mirror of itself on a no-op edit.
  const latestEditedSegments = useRef(null);
  const [form, setForm] = useState({
    font:             initialParams.font             ?? "",
    font_scale:       initialParams.font_scale       ?? 1.0,
    text_case:        initialParams.text_case        ?? "upper",
    lyric_transition: initialParams.lyric_transition ?? "cut",
    // Si el picker está oculto, forzamos "none" en lugar de heredar de
    // initialParams. Sin esto, un job viejo que se renderizó con
    // text_motion="subtle" mantendría motion al ser re-editado, y volvería
    // a pegar contra el timeout de moviepy. Con SHOW_MOTION_PICKER=false
    // el diff calcula form="none" vs initial="subtle" → manda
    // text_motion:"none" al backend → re-render rápido y estable.
    text_motion:      SHOW_MOTION_PICKER ? (initialParams.text_motion ?? "none") : "none",
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

  const limitReached = editsRemaining <= 0;
  // Typography reuses the cached bg from R2 to skip Veo. Without a
  // cached key the backend rejects the edit. Disable the button up-front
  // instead of letting the user fill the form and getting a raw English
  // 400 in the face.
  const typographyAvailable = Boolean(job.bg_r2_key_cached);

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
  // treats missing fields as "keep the prior value".
  //
  // Both `background` and `typography` paths now optionally include
  // `segments` when the operator was editing lyric text inside the
  // modal's LyricsEditor and clicked the regen button before the 3 s
  // autosave debounce fired. The backend persists these segments to
  // segments_json before enqueueing, so the worker reads the corrected
  // text regardless of edit_type. Incident 2026-05-15: Bersuit lyric
  // "de la amor" → "del amor" was silently dropped on background
  // re-renders because of this race.
  const buildPayload = (type) => {
    if (type === "background") {
      const p = { edit_type: "background" };
      const hint = (backgroundHint || "").trim();
      if (hint) p.background_hint = hint;
      // Send mode explicitly only when non-default so older backends
      // that don't know the field still accept the payload. After PR
      // ships, dropping the check is safe but adds zero value.
      if (backgroundMode && backgroundMode !== "veo") {
        p.background_mode = backgroundMode;
      }
      if (latestEditedSegments.current && latestEditedSegments.current.length > 0) {
        p.segments = latestEditedSegments.current;
      }
      return p;
    }
    // edit_type === "lyrics" no longer builds via this function — it's
    // owned by submitLyricsWithSegments() which receives segments from
    // the modal LyricsEditor's onApprove callback.
    const p = { edit_type: "typography" };
    if (form.font             !== (initialParams.font             ?? "")) p.font = form.font;
    if (form.font_scale       !== (initialParams.font_scale       ?? 1.0)) p.font_scale = form.font_scale;
    if (form.text_case        !== (initialParams.text_case        ?? "upper")) p.text_case = form.text_case;
    if (form.lyric_transition !== (initialParams.lyric_transition ?? "cut")) p.lyric_transition = form.lyric_transition;
    if (form.text_motion      !== (initialParams.text_motion      ?? "none")) p.text_motion = form.text_motion;
    if (latestEditedSegments.current && latestEditedSegments.current.length > 0) {
      p.segments = latestEditedSegments.current;
    }
    return p;
  };

  // When the operator enters lyrics mode, hydrate the draft from the
  // job's persisted segments (or an empty array if none — the UI shows
  // a banner in that case).
  //
  // CRITICAL: deps must be [mode] only. Adding `job.segments_json` here
  // causes React error #300 (Maximum update depth exceeded) because
  // /status polling (every 5 s while a JobDetail tab is open) returns
  // segments_json as a FRESH array reference on every response — same
  // content, new object. React compares deps by reference, so the
  // effect re-fires every poll, setLyricsDraft re-runs, the component
  // re-renders, and the loop never settles. Confirmed in prod 2026-
  // 05-13 right after PR #121 started exposing segments_json in /status.
  // Pre-#121 the field was always undefined → reference stable → no
  // loop. The exhaustive-deps lint warning is intentional.
  useEffect(() => {
    if (mode !== "lyrics") return;
    let cancelled = false;
    setLyricsAudioError(null);
    setLyricsAudioUrl(null);
    (async () => {
      try {
        const res = await fetch(`${API}/jobs/${job.job_id}/source-audio-url`, {
          headers: authHeaders(),
        });
        if (cancelled) return;
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          setLyricsAudioError(
            (typeof data.detail === "string" ? data.detail : null)
            || (t("edit.lyrics_audio_unavailable") ||
              "El audio fuente no está disponible — solo podrás editar texto sin escuchar playback.")
          );
          return;
        }
        const data = await res.json();
        if (!cancelled) setLyricsAudioUrl(data.url);
      } catch (e) {
        if (!cancelled) setLyricsAudioError(e?.message || "Network error");
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, job.job_id]);

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

  // Lyrics submit path used by the full-editor modal: the operator
  // edits segments in LyricsEditor and on Approve we receive them
  // directly (no internal draft state). Validates non-empty, short-
  // circuits the "nothing actually changed" case, fires POST + 409
  // retry, and notifies the parent on success.
  const submitLyricsWithSegments = async (segments) => {
    if (submitLockRef.current || limitReached) return;
    submitLockRef.current = true;
    if (!Array.isArray(segments) || segments.length === 0) {
      if (mountedRef.current) {
        setError(t("edit.lyrics_empty") || "Las letras quedaron vacías — no hay nada que renderizar.");
      }
      submitLockRef.current = false;
      return;
    }
    const payload = {
      edit_type: "lyrics",
      segments: segments.map((s) => ({
        start: Number(s.start) || 0,
        end: Number(s.end) || 0,
        text: String(s.text || ""),
      })),
    };
    // No-change short-circuit: same exact array as the persisted one.
    const original = Array.isArray(job.segments_json) ? job.segments_json : [];
    const unchanged = original.length === payload.segments.length &&
      original.every((s, i) =>
        s.text === payload.segments[i].text &&
        Math.abs((s.start ?? 0) - payload.segments[i].start) < 0.001 &&
        Math.abs((s.end ?? 0) - payload.segments[i].end) < 0.001
      );
    if (unchanged) {
      if (mountedRef.current) {
        setError(t("edit.no_changes") || "No cambiaste ninguna opción — no hay nada que re-renderizar.");
      }
      submitLockRef.current = false;
      return;
    }
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
      if (!succeeded && mountedRef.current) {
        setSubmitting(false);
      }
    }
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
      if (mountedRef.current) {
        setError(t("edit.no_changes") || "No cambiaste ninguna opción — no hay nada que re-renderizar.");
      }
      submitLockRef.current = false;
      return;
    }

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
          {editsRemaining === 1
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
          {allowsTypography && typographyAvailable && (
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
          )}
          {allowsTypography && !typographyAvailable && (
          <div className="text-left p-4 rounded-xl bg-surface-3/20 ring-1 ring-white/[0.03] opacity-60 cursor-not-allowed">
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-ink-secondary" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M4 7V4h16v3M9 20h6M12 4v16" strokeLinecap="round" />
              </svg>
              <span className="text-sm font-medium text-ink-secondary">
                {t("edit.typography_title") || "Cambiar tipografía"}
              </span>
            </div>
            <p className="text-[11px] text-amber-300/80">
              {t("edit.typography_needs_bg") ||
                "Este video no tiene fondo cacheado. Regenerá el fondo primero para poder cambiar la tipografía."}
            </p>
          </div>
          )}

          {allowsLyrics && (
          <button
            type="button"
            onClick={() => setMode("lyrics")}
            className="text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-brand-light/30 transition-all"
          >
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 19V6l12-2v13M9 19a2 2 0 11-4 0 2 2 0 014 0zM21 17a2 2 0 11-4 0 2 2 0 014 0z" strokeLinecap="round" />
              </svg>
              <span className="text-sm font-medium text-white">
                {t("edit.lyrics_title") || "Corregir letras"}
              </span>
            </div>
            <p className="text-[11px] text-ink-secondary">
              {t("edit.lyrics_cost") || "~5-10 min · sin costo extra · cambiá palabras o frases mal transcriptas"}
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
              {t("edit.background_cost") || "~10-15 min · ~US$0.90 · nuevo Veo manteniendo lyrics"}
            </p>
          </button>
          )}
        </div>
      )}

      {/* Lyrics mode renders as a full-screen modal overlay at the very
          end of this component — the inline panel collapses while it's
          open. See the {mode === "lyrics" && ...} block below the
          background section. */}

      {mode === "typography" && (
        <div className="space-y-3 animate-fade-in">
          {/* Live preview — renders the sample lyric with the controls
              the operator is touching so they can see the result before
              firing the ~5min re-render. The 16:9 frame is the same
              aspect the worker outputs. AUTO falls back to Montserrat
              with a note so the operator knows the final font won't
              actually be Montserrat at render time. */}
          {(() => {
            const sample = t("edit.sample_lyric") || "Como el viento que se va";
            const previewText = applyCaseToPreview(sample, form.text_case);
            const fontCss = FONT_CSS_BY_CODE[form.font] || FONT_CSS_BY_CODE[""];
            const isAuto = !form.font;
            // Preview is ~480px wide vs 1920px video, so font scales down ~4×
            const basePx = 70;
            const scaledPx = Math.max(14, Math.round(basePx * form.font_scale * (480 / 1920)));
            return (
              <div>
                <label className="text-[11px] text-ink-secondary uppercase tracking-wider block mb-1">
                  {t("edit.preview_label") || "Vista previa"}
                </label>
                <div className="rounded-xl overflow-hidden ring-1 ring-white/[0.06]">
                  <div
                    className="relative w-full flex items-center justify-center bg-gradient-to-b from-gray-900 to-black"
                    style={{ aspectRatio: "16/9", maxHeight: "140px" }}
                  >
                    <p
                      style={{
                        fontFamily: fontCss,
                        fontSize: `${scaledPx}px`,
                        fontWeight: 700,
                        color: "white",
                        opacity: isAuto ? 0.65 : 1,
                        textShadow: "0 0 4px rgba(0,0,0,0.9), 1px 1px 3px rgba(0,0,0,0.8)",
                        textAlign: "center",
                        lineHeight: 1.2,
                        padding: "0 12px",
                        wordBreak: "break-word",
                        margin: 0,
                      }}
                    >
                      {previewText}
                    </p>
                  </div>
                  {isAuto && (
                    <div className="px-3 py-1.5 bg-amber-500/[0.08] border-t border-amber-500/20 text-[10px] text-amber-200/90">
                      {t("editor.auto_font_badge") || "Tipografía: Auto"}
                      {" · "}
                      {t("editor.auto_font_explainer") || "el render va a elegir una de 8 fuentes al azar."}
                    </div>
                  )}
                </div>
              </div>
            );
          })()}

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
          {SHOW_MOTION_PICKER && (
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
          )}

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
              {backgroundMode === "imagen"
                ? (t("edit.background_confirm_desc_imagen") ||
                    "Genera un fondo nuevo con Imagen-4 (foto + zoom suave) manteniendo las lyrics y los tiempos. Cuesta ~US$0.03 y tarda ~30 segundos. Sin riesgo de caras humanas en el fondo. La tipografía actual se mantiene.")
                : (t("edit.background_confirm_desc") ||
                    "Genera un fondo nuevo con Veo (video cinematográfico) manteniendo las lyrics y los tiempos. Cuesta ~US$0.90 y tarda ~10-15 min. La tipografía actual se mantiene.")}
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
              className={`flex-1 px-3 py-2 rounded-lg text-[11px] font-medium transition-colors flex flex-col items-center gap-0.5 ${
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
                {t("edit.bg_mode_veo_hint") || "Veo · ~15 min · cámaras y motion"}
              </span>
            </button>
            <button
              type="button"
              onClick={() => setBackgroundMode("imagen")}
              disabled={submitting}
              className={`flex-1 px-3 py-2 rounded-lg text-[11px] font-medium transition-colors flex flex-col items-center gap-0.5 ${
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
                {t("edit.bg_mode_imagen_hint") || "Imagen-4 · ~30s · zoom suave"}
              </span>
            </button>
          </div>

          <BackgroundHintField
            value={backgroundHint}
            onChange={setBackgroundHint}
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

      {/* Lyrics re-sync modal. Full-screen overlay that mounts the same
          LyricsEditor used by the wizard, but in "post-approval" mode:
          audio streams from a signed R2 URL, no auto-split / autosave /
          beforeunload, submit button renamed to communicate the re-render
          intent. The operator's Approve callback hands us the cleaned
          segments which we POST to /edit (with the 409 YouTube guard).

          Layout notes:
          - flex justify-center wraps the editor — its root is
            max-w-3xl without mx-auto (the wizard parent does that
            centering), so without this wrapper the editor renders
            flush-left and leaves a huge dead space on the right.
          - Body scroll lock so the underlying JobDetail content
            doesn't scroll around behind the overlay when the operator
            scrolls the editor's long segment list.
          - Solid bg (no /95 + backdrop-blur) — the blur effect
            distorted text legibility on the underlying job preview. */}
      <LyricsEditModal
        open={mode === "lyrics"}
        audioError={lyricsAudioError}
        error={error}
        job={job}
        onClose={() => { setMode(null); setError(null); }}
        audioUrl={lyricsAudioUrl}
        onApprove={submitLyricsWithSegments}
        submitting={submitting}
        initialParams={initialParams}
        t={t}
      />
    </div>
  );
}

/** Full-screen modal that hosts the LyricsEditor for post-approval re-sync.
 *
 * Split out from EditRequestPanel's render tree so the body-scroll-lock
 * useEffect can hook on `open` without polluting the parent component's
 * concerns, and so the heavy LyricsEditor only mounts when the modal is
 * actually opened (preserves the editor's "fresh start on each open"
 * behavior — editHistory, syncMode, etc are reset).
 */
function LyricsEditModal({
  open, audioError, error, job, onClose, audioUrl, onApprove,
  submitting, initialParams, t,
}) {
  // Lock the underlying body scroll while the modal is open. Without
  // this, scrolling on the modal's segment list bleeds through to the
  // JobDetail page underneath on macOS/Safari, which felt broken.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [open]);

  // Close on Escape — standard modal behavior.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const noSegments = !Array.isArray(job.segments_json) || job.segments_json.length === 0;

  return (
    <div className="fixed inset-0 z-[60] bg-surface overflow-y-auto">
      <div className="min-h-screen px-4 py-8 sm:px-8">
        <div className="max-w-3xl mx-auto">
          {audioError && (
            <div className="mb-4 rounded-2xl ring-1 ring-amber-500/30 bg-amber-500/[0.08] px-4 py-3">
              <p className="text-xs text-amber-200">{audioError}</p>
            </div>
          )}
          {error && (
            <div className="mb-4 rounded-2xl ring-1 ring-red-500/30 bg-red-500/[0.08] px-4 py-3">
              <p className="text-xs text-red-300">{error}</p>
            </div>
          )}
        </div>
        <div className="flex justify-center">
          {noSegments ? (
            <div className="max-w-3xl w-full rounded-2xl ring-1 ring-amber-500/30 bg-amber-500/[0.08] px-4 py-4">
              <p className="text-sm text-amber-200 mb-3">
                {t("edit.lyrics_no_segments") ||
                  "Este job no tiene letras guardadas. Esto pasa con jobs muy viejos. Subí la canción de nuevo para editar letras."}
              </p>
              <button
                type="button"
                onClick={onClose}
                className="btn-secondary text-xs h-9 px-4"
              >
                {t("edit.cancel") || "Cancelar"}
              </button>
            </div>
          ) : (
            <LyricsEditor
              segments={job.segments_json}
              filename={job.filename || job.artist || "lyrics"}
              audioFile={null}
              audioUrl={audioUrl}
              referenceLyrics=""
              onApprove={onApprove}
              onBack={onClose}
              isBatch={false}
              user={null}
              font={initialParams.font || ""}
              textCase={initialParams.text_case || "upper"}
              fontScale={initialParams.font_scale || 1.0}
              lyricTransition={initialParams.lyric_transition || "cut"}
              textMotion={initialParams.text_motion || "none"}
              disableAutoSplit
              disableBeforeUnload
              // Autosave ON inside the /edit modal: backend's
              // /save-segments now accepts pending_review (incident
              // 2026-05-15 — without this, text corrections made
              // here lived only in component state and got dropped
              // on any subsequent background re-render).
              transcribeJobId={job.job_id}
              onPersistSegments={async (jobId, segs) => {
                try {
                  await fetch(`${API}/jobs/${jobId}/save-segments`, {
                    method: "POST",
                    headers: {
                      "Content-Type": "application/json",
                      ...authHeaders(),
                    },
                    body: JSON.stringify({ segments: segs }),
                  });
                } catch (e) {
                  // Best-effort autosave; if it fails the operator can
                  // still click "Apply lyrics" to commit segments via
                  // the explicit /edit?edit_type=lyrics path.
                  // eslint-disable-next-line no-console
                  console.warn("[EditRequestPanel] autosave failed", e);
                }
              }}
              // Note: NO onEditedChange wired here. The ref it would
              // mirror into (latestEditedSegments) is scoped to the
              // EditRequestPanel component above, not to this
              // LyricsEditModal. Referencing it here throws
              // "Can't find variable: latestEditedSegments" the moment
              // the operator opens the modal (incident 2026-05-17:
              // prod admin couldn't open /edit lyrics at all). The
              // ref isn't needed here anyway — LyricsEditModal submits
              // via the explicit `onApprove(segments)` callback which
              // receives the latest segments synchronously from the
              // editor at click time; there is no race window like the
              // 3 s autosave debounce path the ref was built for.
              submitLabel={
                submitting
                  ? (t("edit.submitting") || "Aplicando...")
                  : (t("edit.lyrics_resync_submit") ||
                    "Re-renderizar con letras y tiempos corregidos")
              }
            />
          )}
        </div>
      </div>
    </div>
  );
}
