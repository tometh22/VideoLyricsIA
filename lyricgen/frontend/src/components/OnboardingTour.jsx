import { useEffect, useRef, useState, useMemo, useCallback, Component } from "react";
import { Joyride } from "react-joyride";
import { useI18n } from "../i18n";

// ─── Error boundary ───────────────────────────────────────────────
// Catches any error thrown by Joyride so the crash stays scoped to the
// tour widget and doesn't unmount the entire app.
class TourErrorBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(err) { console.error("[Tour] Joyride error caught:", err); }
  render() { return this.state.failed ? null : this.props.children; }
}

// ─── Custom beacon ────────────────────────────────────────────────
// Replaces Joyride's default blue dot with a pulsing brand-violet ring.
function TourBeacon({ continuous, index, isLastStep, size, step, ...rest }) {
  return (
    <button
      {...rest}
      aria-label="Abrir tour guiado"
      className="relative flex h-5 w-5 items-center justify-center focus:outline-none"
    >
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand opacity-60" />
      <span className="relative inline-flex h-3 w-3 rounded-full bg-brand shadow-glow" />
    </button>
  );
}

// ─── Custom tooltip ───────────────────────────────────────────────
// Fully glass-styled tooltip that matches the app's design system.
// Layout: left accent bar | title + body | dot progress + nav buttons
function TourTooltip({
  continuous,
  index,
  isLastStep,
  size,
  step,
  backProps,
  primaryProps,
  skipProps,
  tooltipProps,
}) {
  return (
    <div
      {...tooltipProps}
      style={{ maxWidth: 340, ...tooltipProps?.style }}
      className="relative flex gap-3 rounded-2xl border border-white/[0.06] bg-[#181821]/90 p-4 shadow-depth-lg backdrop-blur-xl animate-fade-in"
    >
      {/* Brand accent bar */}
      <div className="mt-0.5 w-[3px] shrink-0 self-stretch rounded-full bg-brand" />

      <div className="flex min-w-0 flex-col gap-3">
        {/* Title */}
        {step.title && (
          <p className="text-[13px] font-semibold leading-snug text-[#F5F7FA]">
            {step.title}
          </p>
        )}

        {/* Content */}
        <div className="text-[12px] leading-relaxed text-[#A0A3B1]">
          {step.content}
        </div>

        {/* Footer: pill-dots progress + nav buttons */}
        <div className="flex items-center justify-between gap-3 pt-0.5">
          {/* Dot progress indicator — active step becomes a pill */}
          <div className="flex items-center gap-1">
            {Array.from({ length: size }).map((_, i) => (
              <span
                key={i}
                className={`inline-block h-1.5 rounded-full transition-all duration-240 ${
                  i === index
                    ? "w-4 bg-brand"
                    : i < index
                    ? "w-1.5 bg-brand/40"
                    : "w-1.5 bg-white/20"
                }`}
              />
            ))}
          </div>

          {/* Navigation buttons */}
          <div className="flex shrink-0 items-center gap-2">
            {index > 0 && (
              <button
                {...backProps}
                className="text-[11px] text-[#A0A3B1] transition-colors duration-240 hover:text-[#F5F7FA]"
              >
                ← Atrás
              </button>
            )}
            {!isLastStep && (
              <button
                {...skipProps}
                className="text-[11px] text-[#A0A3B1]/50 transition-colors duration-240 hover:text-[#A0A3B1]"
              >
                Saltar
              </button>
            )}
            <button
              {...primaryProps}
              className="rounded-[10px] bg-brand px-3.5 py-1.5 text-[11px] font-semibold text-white shadow-glow transition-all duration-240 hover:bg-brand-light active:scale-95"
            >
              {isLastStep ? "Listo ✓" : "Siguiente →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Joyride styles ───────────────────────────────────────────────
// Minimal — layout and colors are fully handled by the custom components.
const STYLES = {
  options: {
    arrowColor: "transparent",       // custom tooltip has no arrow
    overlayColor: "rgba(5,5,10,0.6)",
    zIndex: 9000,                    // beats sidebar (z-20), modals (z-50), popovers (z-1000)
  },
  spotlight: { borderRadius: 12 },
};

// ─── Persistence flags ────────────────────────────────────────────
// Each tour persists independently so a user who already saw the dashboard
// tour but hasn't been to the editor still gets the editor tour on arrival.
const FLAGS = {
  dashboard: "genly_tour_dashboard_done",
  upload:    "genly_tour_upload_done",
  editor:    "genly_tour_editor_done",
  // jobdetail covers the approval workflow + ProRes download. Fires
  // when the user lands on a pending_review job — that's the moment
  // those affordances actually exist on screen.
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
  if (!user) return false;
  if (isReplayActive()) return true;
  return daysSince(user.created_at) < AGE_GATE_DAYS;
}

// ─── Shared Joyride runner ────────────────────────────────────────
function TourRunner({ flagKey, steps, user, forceRun = false, onDone }) {
  const { t } = useI18n();
  const [run, setRun] = useState(false);
  const helpersRef = useRef(null);

  useEffect(() => {
    setRun(forceRun || shouldAutoRun(flagKey, user));
  }, [flagKey, user, forceRun]);

  // When TourRunner unmounts mid-tour (e.g. user navigates to another page),
  // Joyride's overlay and beacon are rendered into document.body via its own
  // portals and won't be removed by React's teardown. reset(true) forces
  // Joyride to clean up those DOM nodes before we disappear.
  useEffect(() => {
    return () => { helpersRef.current?.reset(true); };
  }, []);

  const handleCallback = useCallback((data) => {
    const { status, type } = data;
    if (type === "tour:end" || status === "finished" || status === "skipped") {
      try { localStorage.setItem(flagKey, "1"); } catch {}
      setRun(false);
      onDone?.();
    }
  }, [flagKey, onDone]);

  const locale = useMemo(() => ({
    back:  t("tour.back")  || "Atrás",
    next:  t("tour.next")  || "Siguiente",
    skip:  t("tour.skip")  || "Saltar",
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
        showSkipButton
        scrollToFirstStep
        disableOverlayClose={false}
        spotlightClicks={false}
        beaconComponent={TourBeacon}
        tooltipComponent={TourTooltip}
        getHelpers={(h) => { helpersRef.current = h; }}
        styles={STYLES}
        callback={handleCallback}
        locale={locale}
        floaterProps={{
          styles: {
            floater: {
              transition: "opacity 200ms ease, transform 200ms cubic-bezier(.2,.8,.2,1)",
            },
          },
        }}
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
        "MP4 H.264 1080p para YouTube, o ProRes 422 HQ + MP4 cuando el cliente pide máster broadcast. Default es MP4.",
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
// Only auto-fires on a `pending_review` job for the first time.
// `hasUmgMaster` controls whether the ProRes step is shown.
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
        "Reproducí acá el lyric video, el short vertical y el thumbnail. Si algo está mal, rechazá.",
      placement: "bottom",
    });
    if (isPendingReview || forceRun) {
      s.push({
        target: '[data-tour="jobdetail-approve-panel"]',
        title: t("tour.jobdetail_approve_title") || "Aprobar o rechazar",
        content: t("tour.jobdetail_approve_body") ||
          "Aprobar habilita la descarga y consume 1 video de tu cuota mensual. Rechazar es gratis — usalo sin culpa si algo no convence.",
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
          "Para entregas broadcast: descargás el .mov ProRes 422 HQ que pasa QC manual. La primera vez tarda 1-2 min; las siguientes son instantáneas.",
        placement: "bottom",
      });
    }
    return s;
  }, [t, hasUmgMaster, isPendingReview, forceRun]);
  return <TourRunner flagKey={FLAGS.jobdetail} steps={steps} user={user} forceRun={forceRun} onDone={onDone} />;
}

// ─── Settings helpers ─────────────────────────────────────────────
export function clearAllTourFlags() {
  for (const k of TOUR_FLAG_KEYS) {
    try { localStorage.removeItem(k); } catch {}
  }
}

// Called by the Settings replay button. Clears done-flags and marks
// the session as a replay so shouldAutoRun bypasses the 14-day age gate.
export function startReplaySession() {
  clearAllTourFlags();
  try { sessionStorage.setItem(REPLAY_KEY, "1"); } catch {}
}
