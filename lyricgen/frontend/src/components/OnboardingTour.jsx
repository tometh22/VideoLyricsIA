import { useEffect, useState, useMemo, useCallback, Component } from "react";
import { Joyride } from "react-joyride";
import { useI18n } from "../i18n";

// Catches any error thrown by Joyride (e.g. internal React reconciliation
// errors in react-floater) so the crash stays scoped to the tour widget
// and doesn't unmount the entire app.
class TourErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(err) { console.error("[Tour] Joyride error caught:", err); }
  render() { return this.state.failed ? null : this.props.children; }
}

// localStorage flags. Each tour persists independently so a user
// who already saw the dashboard tour but hasn't been to the editor
// still gets the editor tour on first arrival.
const FLAGS = {
  dashboard: "genly_tour_dashboard_done",
  upload:    "genly_tour_upload_done",
  editor:    "genly_tour_editor_done",
  // jobdetail covers the approval workflow + ProRes download. Fires
  // when the user lands on a pending_review job — that's the moment
  // those affordances actually exist on screen and the explanation
  // (rejecting is free, ProRes is a separate file, etc.) lands best.
  jobdetail: "genly_tour_jobdetail_done",
};

export const TOUR_FLAG_KEYS = Object.values(FLAGS);

// sessionStorage key set by the Settings replay button. Lives for the
// browser session so the user can walk Home → Upload → Editor and each
// tour fires in turn, then dies on tab close.
const REPLAY_KEY = "genly_tour_replay";

// Age gate: if the user is more than 14 days old, don't auto-fire.
// Existing users on next deploy don't get tour-blasted; they can
// replay manually from Settings.
const AGE_GATE_DAYS = 14;

function daysSince(isoDate) {
  if (!isoDate) return Infinity;
  const t = Date.parse(isoDate);
  if (Number.isNaN(t)) return Infinity;
  return (Date.now() - t) / 86400000;
}

function isReplayActive() {
  if (typeof window === "undefined") return false;
  try { return sessionStorage.getItem(REPLAY_KEY) === "1"; } catch { return false; }
}

function shouldAutoRun(flagKey, user) {
  if (typeof window === "undefined") return false;
  if (localStorage.getItem(flagKey) === "1") return false;
  // No user yet (still loading) → defer
  if (!user) return false;
  // Replay session bypasses the age gate. Each tour still self-terminates
  // by setting its own done-flag after first play, so we don't loop.
  if (isReplayActive()) return true;
  return daysSince(user.created_at) < AGE_GATE_DAYS;
}

// Brand-matched styles for the Joyride tooltips. Matches the
// surface-2 + brand-purple palette used elsewhere.
const STYLES = {
  options: {
    primaryColor: "#8B5CF6",         // brand
    backgroundColor: "#1A1B2E",      // surface-2
    textColor: "#E5E5F0",            // ink-primary
    arrowColor: "#1A1B2E",
    overlayColor: "rgba(0, 0, 0, 0.55)",
    zIndex: 50,
    width: 360,
  },
  tooltip: { borderRadius: 12, padding: 16 },
  tooltipTitle: { fontSize: 15, fontWeight: 600 },
  tooltipContent: { fontSize: 13, lineHeight: 1.55, padding: "8px 0" },
  buttonNext: {
    backgroundColor: "#8B5CF6",
    borderRadius: 8,
    fontSize: 12,
    fontWeight: 600,
    padding: "8px 14px",
  },
  buttonBack: { color: "#9CA3AF", fontSize: 12, marginRight: 8 },
  buttonSkip: { color: "#6B7280", fontSize: 11 },
  buttonClose: { display: "none" },
  spotlight: { borderRadius: 12 },
};

// Shared Joyride wrapper: runs only when shouldAutoRun is true OR
// `forceRun` is set (used by the Settings replay button).
function TourRunner({ flagKey, steps, user, forceRun = false, onDone }) {
  const { t } = useI18n();
  const [run, setRun] = useState(false);

  // Decide once on mount whether to run. Avoid running mid-render or
  // on every re-render — Joyride doesn't like its `run` prop flipping
  // unexpectedly.
  useEffect(() => {
    setRun(forceRun || shouldAutoRun(flagKey, user));
  }, [flagKey, user, forceRun]);

  const handleCallback = useCallback((data) => {
    const { status, type } = data;
    // 'finished' = user reached the end. 'skipped' = user clicked Skip.
    // Both should mark the flag. Joyride emits 'tour:end' lifecycle
    // event for both terminal states.
    if (type === "tour:end" || status === "finished" || status === "skipped") {
      try { localStorage.setItem(flagKey, "1"); } catch {}
      setRun(false);
      onDone && onDone();
    }
  }, [flagKey, onDone]);

  // Memoize locale so Joyride never receives a new object reference on
  // re-render. Passing an inline literal causes Joyride v3 to setState on
  // every render, triggering an infinite loop (React #306 = black screen).
  const locale = useMemo(() => ({
    back:  t("tour.back")  || "Atrás",
    next:  t("tour.next")  || "Siguiente",
    skip:  t("tour.skip")  || "Saltar tour",
    last:  t("tour.finish")|| "Listo",
    close: "✕",
  }), [t]);

  if (!run) return null;

  return (
    <TourErrorBoundary>
      <Joyride
        steps={steps}
        run={run}
        continuous
        showProgress
        showSkipButton
        disableOverlayClose
        scrollToFirstStep
        styles={STYLES}
        callback={handleCallback}
        locale={locale}
      />
    </TourErrorBoundary>
  );
}

// ─── Tour 1: Dashboard ────────────────────────────────────────────
export function DashboardTour({ user, forceRun = false, onDone }) {
  const { t } = useI18n();
  const steps = useMemo(() => [
    {
      target: "body",
      placement: "center",
      title: t("tour.dashboard_welcome_title") || "Bienvenido a GenLy AI",
      content: t("tour.dashboard_welcome_body") ||
        "Te muestro cómo funciona en 30 segundos. Podés saltarlo en cualquier momento.",
      disableBeacon: true,
    },
    {
      target: '[data-tour="dashboard-usage"]',
      title: t("tour.dashboard_usage_title") || "Tu uso mensual",
      content: t("tour.dashboard_usage_body") ||
        "Acá ves cuántos videos te quedan del plan. Solo los aprobados cuentan.",
    },
    {
      target: '[data-tour="dashboard-new-batch"]',
      title: t("tour.dashboard_new_title") || "Crear video",
      content: t("tour.dashboard_new_body") ||
        "Empezás generando un lyric video desde acá. Subís un MP3 y elegís el fondo.",
    },
    {
      target: '[data-tour="dashboard-recent"]',
      title: t("tour.dashboard_recent_title") || "Tus últimos videos",
      content: t("tour.dashboard_recent_body") ||
        "Tus videos terminados aparecen acá. Click para verlos, descargarlos o aprobar.",
      placement: "top",
    },
    {
      target: '[data-tour="sidebar-nav"]',
      title: t("tour.dashboard_nav_title") || "Navegación",
      content: t("tour.dashboard_nav_body") ||
        "Desde el menú accedés a Crear, Historial y Configuración.",
      placement: "right",
    },
  ], [t]);
  return <TourRunner flagKey={FLAGS.dashboard} steps={steps} user={user} forceRun={forceRun} onDone={onDone} />;
}

// ─── Tour 2: Upload / Crear video ────────────────────────────────
export function UploadTour({ user, forceRun = false, onDone }) {
  const { t } = useI18n();
  const steps = useMemo(() => [
    {
      target: '[data-tour="upload-dropzone"]',
      title: t("tour.upload_drop_title") || "Subí tu audio",
      content: t("tour.upload_drop_body") ||
        "Arrastrá uno o varios MP3 acá, o click para elegirlos. Cada archivo será un video.",
      disableBeacon: true,
    },
    {
      target: '[data-tour="upload-row"]',
      title: t("tour.upload_row_title") || "Por archivo",
      content: t("tour.upload_row_body") ||
        "Completás artista e idioma. 'Más opciones' tiene fuente, género y concepto del fondo.",
      placement: "bottom",
    },
    {
      target: '[data-tour="upload-bg-tabs"]',
      title: t("tour.upload_bg_title") || "Fondo del video",
      content: t("tour.upload_bg_body") ||
        "La IA lo genera, lo elegís de la biblioteca de fondos pre-aprobados, o subís uno tuyo.",
    },
    {
      target: '[data-tour="upload-delivery"]',
      title: t("tour.upload_delivery_title") || "Formato de entrega",
      content: t("tour.upload_delivery_body") ||
        "MP4 H.264 1080p para YouTube, o ProRes 422 HQ + MP4 cuando el cliente pide máster broadcast (4K, DCI, etc.). Default es MP4.",
    },
    {
      target: '[data-tour="upload-cta-bar"]',
      title: t("tour.upload_cta_title") || "¿Revisar o generar directo?",
      content: t("tour.upload_cta_body") ||
        "'Revisar lyrics' te deja editar timestamps antes del render. 'Generar directo' salta esa edición.",
      placement: "top",
    },
  ], [t]);
  return <TourRunner flagKey={FLAGS.upload} steps={steps} user={user} forceRun={forceRun} onDone={onDone} />;
}

// ─── Tour 3: Lyrics Editor ───────────────────────────────────────
export function EditorTour({ user, forceRun = false, onDone }) {
  const { t } = useI18n();
  const steps = useMemo(() => [
    {
      target: '[data-tour="editor-playbar"]',
      title: t("tour.editor_playbar_title") || "Reproducción",
      content: t("tour.editor_playbar_body") ||
        "Apretá Play para escuchar. Espacio = play/pause. Mientras suena, la línea actual se resalta.",
      disableBeacon: true,
      placement: "bottom",
    },
    {
      target: '[data-tour="editor-list-row"]',
      title: t("tour.editor_list_title") || "Tus líneas",
      content: t("tour.editor_list_body") ||
        "Click un timestamp para saltar a ese momento. Doble click para editarlo a mano.",
    },
    {
      target: '[data-tour="editor-sync-entry"]',
      title: t("tour.editor_sync_title") || "Modo Sync",
      content: t("tour.editor_sync_body") ||
        "Si necesitás ajustar los tiempos, click acá. Apretás Espacio cuando arranca cada línea y se sincronizan en vivo.",
    },
    {
      target: '[data-tour="editor-row-sync"]',
      title: t("tour.editor_row_sync_title") || "Sync desde acá",
      content: t("tour.editor_row_sync_body") ||
        "Hover una línea, click el target 🎯 y arrancás Sync desde ahí. Las anteriores quedan intactas.",
    },
    {
      target: '[data-tour="editor-add-line"]',
      title: t("tour.editor_add_title") || "Líneas faltantes",
      content: t("tour.editor_add_body") ||
        "¿Faltó una repetición del estribillo? Duplicá una línea (📋 al hover) o agregá una vacía abajo y tipeá.",
      placement: "top",
    },
    {
      target: '[data-tour="editor-approve"]',
      title: t("tour.editor_approve_title") || "Aprobar y generar",
      content: t("tour.editor_approve_body") ||
        "Cuando esté listo, aprobás y se renderiza el video final. Listo para descargar.",
      placement: "left",
    },
  ], [t]);
  return <TourRunner flagKey={FLAGS.editor} steps={steps} user={user} forceRun={forceRun} onDone={onDone} />;
}

// ─── Tour 4: Job Detail (approval + delivery) ────────────────────
// Mounted by JobDetail.jsx and only auto-fires when the user is
// looking at a `pending_review` job for the first time. The
// `hasUmgMaster` flag drives whether the ProRes step is included —
// no point pointing at a button that isn't on screen for a
// YouTube-only job.
export function JobDetailTour({ user, hasUmgMaster = false, isPendingReview = false, forceRun = false, onDone }) {
  const { t } = useI18n();
  const steps = useMemo(() => {
    const s = [];
    if (isPendingReview || forceRun) {
      s.push({
        target: '[data-tour="jobdetail-status-badge"]',
        title: t("tour.jobdetail_pending_title") || "Pendiente de aprobación",
        content: t("tour.jobdetail_pending_body") ||
          "El video se renderizó OK pero todavía no se entrega. Vos lo revisás antes de habilitar la descarga — así nada sale a un cliente sin que lo hayas mirado.",
        disableBeacon: true,
      });
    }
    s.push({
      target: '[data-tour="jobdetail-preview"]',
      title: t("tour.jobdetail_preview_title") || "Mirá el video",
      content: t("tour.jobdetail_preview_body") ||
        "Reproducí acá el lyric video, el short vertical y el thumbnail. Si algo está mal (lyric mal sincronizado, error de tipeo, fondo raro), rechazá.",
      placement: "bottom",
    });
    if (isPendingReview || forceRun) {
      s.push({
        target: '[data-tour="jobdetail-approve-panel"]',
        title: t("tour.jobdetail_approve_title") || "Aprobar o rechazar",
        content: t("tour.jobdetail_approve_body") ||
          "Aprobar habilita la descarga y consume 1 video de tu cuota mensual. Rechazar es gratis y no cuenta — usalo sin culpa si algo no convence.",
        placement: "top",
      });
    }
    s.push({
      target: '[data-tour="jobdetail-download-all"]',
      title: t("tour.jobdetail_download_title") || "Descargar todo",
      content: t("tour.jobdetail_download_body") ||
        "Bajás MP4 + Short + Thumbnail empaquetados. Para entrega a YouTube alcanza con esto.",
    });
    if (hasUmgMaster) {
      s.push({
        target: '[data-tour="jobdetail-prores-master"]',
        title: t("tour.jobdetail_prores_title") || "Master ProRes",
        content: t("tour.jobdetail_prores_body") ||
          "Para entregas tipo UMG: descargás el .mov ProRes 422 HQ (BT.709, audio PCM 24-bit) que pasa QC manual. La primera vez tarda 1-2 min mientras se transcodea; las siguientes son instantáneas.",
        placement: "bottom",
      });
    }
    return s;
  }, [t, hasUmgMaster, isPendingReview, forceRun]);
  return <TourRunner flagKey={FLAGS.jobdetail} steps={steps} user={user} forceRun={forceRun} onDone={onDone} />;
}

// Helper for the Settings replay button.
export function clearAllTourFlags() {
  for (const k of TOUR_FLAG_KEYS) {
    try { localStorage.removeItem(k); } catch {}
  }
}

// Called by the Settings replay button. Clears done-flags and marks
// the session as a replay so shouldAutoRun bypasses the 14-day age
// gate. The flag clears on tab close — no manual teardown needed.
export function startReplaySession() {
  clearAllTourFlags();
  try { sessionStorage.setItem(REPLAY_KEY, "1"); } catch {}
}
