import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BackgroundHintField from "./BackgroundHintField";
import ContentValidationToggle, { isUmgTenant } from "./ContentValidationToggle";
import LyricsEditor from "./LyricsEditor";
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

  const [mode, setMode] = useState(null); // null | "typography" | "background" | "lyrics"
  // Audio source URL for the full-editor modal. Fetched lazily when the
  // operator opens "Lyrics" — same MP3 the worker would use for a re-
  // render. Null while loading; non-null once /jobs/{id}/source-audio-url
  // responds; we surface the error inline if R2 has lost the input.
  const [lyricsAudioUrl, setLyricsAudioUrl] = useState(null);
  const [lyricsAudioError, setLyricsAudioError] = useState(null);
  // Audio peak envelope for the timeline waveform. Best-effort: null →
  // timeline renders without a waveform.
  const [lyricsWaveform, setLyricsWaveform] = useState(null);
  // Signed URL of the cached background video for the live preview.
  // Best-effort: null → preview uses a style gradient.
  const [lyricsBgUrl, setLyricsBgUrl] = useState(null);
  // Typography chosen live in the editor's preview (null = unchanged).
  const [lyricsFont, setLyricsFont] = useState(null);
  const [lyricsCase, setLyricsCase] = useState(null);
  const [lyricsContrast, setLyricsContrast] = useState(null);
  // 2026-05-23: lyricsAnimation + lineTransition reemplazan al legacy
  // lyricsTransition (Corte/Fade). Se eligen en el LyricsEditor y van al
  // backend como lyrics_animation / line_transition.
  const [lyricsAnimation, setLyricsAnimation] = useState(null);
  const [lineTransition, setLineTransition] = useState(null);
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
  const [validationEnabled, setValidationEnabled] = useState(_isUmg);
  // Latest segments from the nested LyricsEditor (updated synchronously
  // on every edit via `onEditedChange`). Held in a ref so buildPayload
  // can read it without re-renders. Used to include the operator's
  // pending text corrections in a background-regen POST, closing the
  // 3 s autosave race documented in the LyricsEditor comment above.
  // Null until the operator touches the editor; we keep it null when
  // untouched so we don't accidentally overwrite segments_json with a
  // mirror of itself on a no-op edit.
  const latestEditedSegments = useRef(null);
  // Snapshot of the segments array as last persisted by /save-segments.
  // We pass THIS (instead of the parent's job.segments_json) to the
  // LyricsEditModal so reopen-after-edit shows what was saved, not the
  // stale array the parent still holds. JobDetail's /status polling does
  // not return segments_json (intentional — too heavy), so without this
  // local mirror, close → reopen surfaces pre-edit text and the next
  // autosave overwrites the just-saved version with the stale one.
  // Bug found in 2026-05-19 QA pass. Reset when job changes (below).
  const [lastSavedSegments, setLastSavedSegments] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  // When a lyrics re-render is requested but the editor's segments match the
  // persisted segments_json exactly, we don't hard-error: the rendered video
  // can still be stale (lyrics fixed out-of-band, prior render failed). Hold
  // the attempted segments here so the modal can offer "re-render anyway".
  const [staleSegments, setStaleSegments] = useState(null);
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

  // Frozen snapshot of the segments AS RENDERED, captured when the lyrics
  // editor OPENS. The "unchanged" guard must compare against this, NOT the
  // live job.segments_json: the editor autosaves layout (pos/scale/rot) back
  // to segments_json, so by the time the operator clicks Approve the persisted
  // array already equals the payload → the guard would think "nothing changed"
  // and force a pointless second "re-render anyway" click. Comparing against
  // the open-time snapshot makes any in-session edit count on the FIRST submit.
  const openSegmentsRef = useRef(null);
  useEffect(() => {
    if (mode === "lyrics" && openSegmentsRef.current == null) {
      openSegmentsRef.current = Array.isArray(job.segments_json)
        ? JSON.parse(JSON.stringify(job.segments_json))
        : [];
    } else if (mode == null) {
      openSegmentsRef.current = null; // reset for the next open
    }
  }, [mode, job.segments_json]);

  // Reset the local saved-segments mirror when the panel is reused across
  // a different job (JobDetail keeps EditRequestPanel mounted while you
  // navigate within the SPA). Without this, opening job B's editor would
  // show job A's last-saved snapshot — worse than the stale-prop bug we
  // are fixing.
  useEffect(() => {
    setLastSavedSegments(null);
  }, [job.job_id]);

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
  // Only the background regen builds via this function now. Typography +
  // lyrics live in the editor (edit_type="lyrics" via submitLyricsWithSegments,
  // which gets segments from the modal LyricsEditor's onApprove callback).
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
    setLyricsWaveform(null);
    setLyricsBgUrl(null);
    // Waveform for the timeline — best-effort, never blocks the editor.
    (async () => {
      try {
        const r = await fetch(`${API}/jobs/${job.job_id}/waveform`, { headers: authHeaders() });
        if (!cancelled && r.ok) setLyricsWaveform(await r.json());
      } catch { /* timeline just renders without a waveform */ }
    })();
    // Cached background for the live preview — best-effort.
    (async () => {
      try {
        const r = await fetch(`${API}/jobs/${job.job_id}/background-url`, { headers: authHeaders() });
        if (!cancelled && r.ok) setLyricsBgUrl((await r.json()).url);
      } catch { /* preview falls back to a gradient */ }
    })();
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
  const submitLyricsWithSegments = async (segments, force = false) => {
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
      // Preserve manual timing locks AND per-line layout (pos/scale/rot)
      // set in the timeline/preview — stripping them here would silently
      // discard the operator's layout on re-render.
      segments: segments.map((s) => ({
        start: Number(s.start) || 0,
        end: Number(s.end) || 0,
        text: String(s.text || ""),
        ...(s.locked ? { locked: true } : {}),
        ...(s.pos && typeof s.pos.x === "number" ? { pos: { x: s.pos.x, y: s.pos.y } } : {}),
        ...(typeof s.scale === "number" && s.scale !== 1 ? { scale: s.scale } : {}),
        ...(typeof s.rot === "number" && s.rot !== 0 ? { rot: s.rot } : {}),
      })),
      // Typography picked live in the preview (omit when unchanged).
      ...(lyricsFont != null ? { font: lyricsFont } : {}),
      ...(lyricsCase != null ? { text_case: lyricsCase } : {}),
      ...(lyricsContrast != null ? { text_contrast: lyricsContrast } : {}),
      // Nuevos 2026-05-23 (reemplazan al legacy lyric_transition).
      ...(lyricsAnimation != null ? { lyrics_animation: lyricsAnimation } : {}),
      ...(lineTransition != null ? { line_transition: lineTransition } : {}),
    };
    // No-change short-circuit: compare against the OPEN-TIME snapshot (what
    // the current video was rendered from), NOT the live job.segments_json —
    // the editor autosaves layout into segments_json, which would mask the
    // operator's edits and force a second "re-render anyway" click.
    // A live font change OR any per-line layout change (pos/scale/rot) also
    // counts as a real change so it doesn't fall into the stale-rerender path.
    const original = Array.isArray(openSegmentsRef.current)
      ? openSegmentsRef.current
      : (Array.isArray(job.segments_json) ? job.segments_json : []);
    const layoutChanged = original.length === payload.segments.length &&
      original.some((s, i) => {
        const p = payload.segments[i];
        const o = s.pos || {}, np = p.pos || {};
        return (o.x ?? null) !== (np.x ?? null) || (o.y ?? null) !== (np.y ?? null)
          || (s.scale ?? 1) !== (p.scale ?? 1) || (s.rot ?? 0) !== (p.rot ?? 0);
      });
    const unchanged = lyricsFont == null && lyricsCase == null &&
      lyricsContrast == null && lyricsAnimation == null && lineTransition == null && !layoutChanged &&
      original.length === payload.segments.length &&
      original.every((s, i) =>
        s.text === payload.segments[i].text &&
        Math.abs((s.start ?? 0) - payload.segments[i].start) < 0.001 &&
        Math.abs((s.end ?? 0) - payload.segments[i].end) < 0.001
      );
    if (unchanged && !force) {
      // Not a dead end: the rendered video can be stale relative to
      // segments_json (lyrics corrected out-of-band, or a prior render
      // failed). Surface an explicit "re-render anyway" escape instead of a
      // hard error that traps the operator with a correct-looking editor and
      // an outdated video.
      if (mountedRef.current) setStaleSegments(segments);
      submitLockRef.current = false;
      return;
    }
    if (mountedRef.current) setSubmitting(true);
    if (mountedRef.current) setError(null);
    if (mountedRef.current) setStaleSegments(null);
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
            onClick={() => setMode("lyrics")}
            className="text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-brand-light/30 transition-all"
          >
            <div className="flex items-center gap-2 mb-1">
              <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 19V6l12-2v13M9 19a2 2 0 11-4 0 2 2 0 014 0zM21 17a2 2 0 11-4 0 2 2 0 014 0z" strokeLinecap="round" />
              </svg>
              <span className="text-sm font-medium text-white">
                {t("edit.lyrics_title") || "Editar letras y estilo"}
              </span>
            </div>
            <p className="text-[11px] text-ink-secondary">
              {t("edit.lyrics_cost") || "~5-10 min · sin costo extra · letra, tipografía, tamaño y posición — en vivo sobre el video"}
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

      {/* Lyrics mode renders as a full-screen modal overlay at the very
          end of this component — the inline panel collapses while it's
          open. See the {mode === "lyrics" && ...} block below the
          background section. */}

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
                {t("edit.bg_mode_veo_hint") || "~15 min · cámaras y motion"}
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
                  className={`px-2.5 py-1.5 rounded-lg text-[11px] font-medium transition-colors ${
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
        staleRerender={!!staleSegments}
        onForceRerender={() => submitLyricsWithSegments(staleSegments, true)}
        job={job}
        segments={lastSavedSegments || job.segments_json}
        onSavedSegments={(segs) => {
          // Spread to a fresh array so LyricsEditor's B7
          // reference-change useEffect re-syncs on next mount.
          if (mountedRef.current) setLastSavedSegments(segs.slice());
        }}
        onClose={() => { setMode(null); setError(null); setStaleSegments(null); }}
        audioUrl={lyricsAudioUrl}
        waveform={lyricsWaveform}
        previewBgUrl={lyricsBgUrl}
        onFontChange={(code) => setLyricsFont(code)}
        onCaseChange={(c) => setLyricsCase(c)}
        onContrastChange={(c) => setLyricsContrast(c)}
        onAnimationChange={(c) => setLyricsAnimation(c)}
        onLineTransitionChange={(c) => setLineTransition(c)}
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
  open, audioError, error, staleRerender, onForceRerender, job, segments,
  onSavedSegments, onClose, audioUrl, waveform, previewBgUrl, onFontChange,
  // 2026-05-23: onTransitionChange (legacy lyric_transition) eliminado;
  // entran onAnimationChange + onLineTransitionChange.
  onCaseChange, onContrastChange, onAnimationChange, onLineTransitionChange,
  onApprove, submitting, initialParams, t,
}) {
  // Scroll the modal back to the top whenever a banner appears, so the
  // error / stale-rerender notice is never stranded above the fold while the
  // operator is scrolled down the (long) segment list. Without this the
  // message renders at the top, out of view, and looks "cut off".
  const scrollRef = useRef(null);
  useEffect(() => {
    if ((error || audioError || staleRerender) && typeof scrollRef.current?.scrollTo === "function") {
      scrollRef.current.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [error, audioError, staleRerender]);
  // Lock the underlying body scroll while the modal is open. Without
  // this, scrolling on the modal's segment list bleeds through to the
  // JobDetail page underneath on macOS/Safari, which felt broken.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [open]);

  // Close on Escape — standard modal behavior. defaultPrevented check:
  // LyricsEditor binds its own window-level Escape handler that exits
  // sync mode (and calls preventDefault). React attaches child effects
  // before parent effects, so the child's listener runs first. Without
  // this guard, hitting Escape while in sync mode tore down the whole
  // modal — the operator lost their editing context just trying to
  // leave a sub-mode. Bug found in 2026-05-19 QA pass.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (e.defaultPrevented) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  // Prefer the local mirror from EditRequestPanel (post-autosave) over
  // job.segments_json, which lags because /status polling intentionally
  // omits the heavy field. Falling back to job is correct for the very
  // first open (before any save lands).
  const effectiveSegments = Array.isArray(segments) ? segments : job.segments_json;
  const noSegments = !Array.isArray(effectiveSegments) || effectiveSegments.length === 0;

  return (
    <div ref={scrollRef} className="fixed inset-0 z-[60] bg-surface overflow-y-auto">
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
          {staleRerender && (
            <div className="mb-4 rounded-2xl ring-1 ring-amber-500/30 bg-amber-500/[0.08] px-4 py-3
              flex flex-col sm:flex-row sm:items-center gap-2">
              <p className="text-xs text-amber-200 flex-1">
                {t("edit.stale_rerender_prompt") ||
                  "Las letras ya están guardadas y no detectamos cambios nuevos. Si el video todavía muestra la versión vieja, podés re-renderizarlo igual."}
              </p>
              <button
                type="button"
                onClick={onForceRerender}
                disabled={submitting}
                className="btn-primary h-9 px-4 text-xs whitespace-nowrap disabled:opacity-50"
              >
                {t("edit.stale_rerender_button") || "Re-renderizar igual"}
              </button>
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
              segments={effectiveSegments}
              filename={job.filename || job.artist || "lyrics"}
              audioFile={null}
              audioUrl={audioUrl}
              waveform={waveform}
              previewBgUrl={previewBgUrl}
              onFontChange={onFontChange}
              onCaseChange={onCaseChange}
              onContrastChange={onContrastChange}
              onAnimationChange={onAnimationChange}
              onLineTransitionChange={onLineTransitionChange}
              referenceLyrics=""
              onApprove={onApprove}
              onBack={onClose}
              isBatch={false}
              user={null}
              font={initialParams.font || ""}
              textCase={initialParams.text_case || "upper"}
              fontScale={initialParams.font_scale || 1.0}
              // 2026-05-23: lyricTransition (Cut/Fade) y textMotion (Sutil
              // drift) eliminados — los reemplazan lyrics_animation +
              // line_transition (libass, paridad con el wizard).
              lyricsAnimation={initialParams.lyrics_animation || "none"}
              lineTransition={initialParams.line_transition || "none"}
              disableAutoSplit
              disableBeforeUnload
              // Autosave ON inside the /edit modal: backend's
              // /save-segments now accepts pending_review (incident
              // 2026-05-15 — without this, text corrections made
              // here lived only in component state and got dropped
              // on any subsequent background re-render).
              transcribeJobId={job.job_id}
              onPersistSegments={async (jobId, segs) => {
                // Surface non-OK responses (e.g. 409 when the job's
                // status falls outside the save-segments whitelist —
                // status=done after approval is a common trap that
                // silently dropped edits before this fix, 2026-05-18).
                // We don't block the operator's UI on a transient
                // network blip, but we DO let them know if the server
                // rejected the save so they don't refresh and lose
                // their work thinking it persisted.
                try {
                  const resp = await fetch(`${API}/jobs/${jobId}/save-segments`, {
                    method: "POST",
                    headers: {
                      "Content-Type": "application/json",
                      ...authHeaders(),
                    },
                    body: JSON.stringify({ segments: segs }),
                  });
                  if (!resp.ok) {
                    let detail = `HTTP ${resp.status}`;
                    try {
                      const body = await resp.json();
                      if (body && body.detail) detail = body.detail;
                    } catch (_) { /* body wasn't JSON */ }
                    // eslint-disable-next-line no-console
                    console.error("[EditRequestPanel] autosave rejected:", detail);
                    // One alert per session-failure-mode so we don't
                    // spam every 3 s while the failure persists.
                    if (window.__genlyAutosaveAlerted !== detail) {
                      window.__genlyAutosaveAlerted = detail;
                      alert({
                        title: "No pudimos guardar tus cambios",
                        description:
                          "Recargá la página y volvé a editar. Si el problema persiste, contactanos.",
                        tone: "error",
                      });
                    }
                  } else {
                    // Reset the alert sentinel on the first success
                    // after a failure, so a later failure re-alerts.
                    if (window.__genlyAutosaveAlerted) {
                      window.__genlyAutosaveAlerted = null;
                    }
                    // Bubble the saved segments up to EditRequestPanel
                    // so its local mirror replaces the parent's stale
                    // job.segments_json on the next modal reopen.
                    if (typeof onSavedSegments === "function") {
                      onSavedSegments(segs);
                    }
                  }
                } catch (e) {
                  // Network error / fetch threw. Don't alert on the
                  // first one (could be a transient blip while saving
                  // — the next 3 s tick retries). Log so the operator
                  // can see it in DevTools if they go looking.
                  // eslint-disable-next-line no-console
                  console.warn("[EditRequestPanel] autosave network error", e);
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
