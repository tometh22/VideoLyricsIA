import { useEffect, useRef, useState } from "react";
import { useI18n } from "../../i18n";

const DISMISS_KEY = "genly_dash_stepper_dismissed_v1";
const AUTO_ADVANCE_MS = 6000;

// Per-step CSS-only mini animations. Each renders a small scene tied to
// the active step. Visuals are bigger and more polished than the Help
// Center demos: this is the home page hero educator, not an article inline.
function StepAnim({ step }) {
  if (step === 0) {
    // Upload: file flies into dropzone, progress, check.
    return (
      <div className="dr-step-anim dr-step-anim-upload">
        <div className="dr-step-upload-zone">
          <div className="dr-step-upload-icon">⤴</div>
        </div>
        <div className="dr-step-upload-file">
          <span className="dr-step-upload-file-icon">♪</span>
          <span className="dr-step-upload-file-name">cancion.mp3</span>
        </div>
        <div className="dr-step-upload-bar"><div className="dr-step-upload-bar-fill" /></div>
        <div className="dr-step-upload-check">✓</div>
      </div>
    );
  }
  if (step === 1) {
    // Transcribe: waveform pulsing + lyrics appearing line by line.
    return (
      <div className="dr-step-anim dr-step-anim-transcribe">
        <div className="dr-step-wave">
          {Array.from({ length: 36 }).map((_, i) => (
            <span key={i} className="dr-step-wave-bar" style={{ animationDelay: `${i * 40}ms` }} />
          ))}
        </div>
        <div className="dr-step-lyrics">
          <div className="dr-step-lyric-line dr-step-lyric-line1">Si el cielo se nubla</div>
          <div className="dr-step-lyric-line dr-step-lyric-line2">Vuelvo a empezar</div>
          <div className="dr-step-lyric-line dr-step-lyric-line3">Sin mirar atrás</div>
        </div>
      </div>
    );
  }
  if (step === 2) {
    // Sync: playbar marker advances along timeline + active row pulses.
    return (
      <div className="dr-step-anim dr-step-anim-sync">
        <div className="dr-step-timeline">
          <div className="dr-step-timeline-marker" />
          <div className="dr-step-timeline-track" />
        </div>
        <div className="dr-step-sync-rows">
          <div className="dr-step-sync-row dr-step-sync-row1">
            <span className="dr-step-sync-ts">0:10</span>
            <span>Si el cielo se nubla</span>
          </div>
          <div className="dr-step-sync-row dr-step-sync-row2">
            <span className="dr-step-sync-ts">0:24</span>
            <span>Vuelvo a empezar</span>
          </div>
          <div className="dr-step-sync-row dr-step-sync-row3">
            <span className="dr-step-sync-ts">0:38</span>
            <span>Sin mirar atrás</span>
          </div>
        </div>
      </div>
    );
  }
  if (step === 3) {
    // Render: frames flipping then final preview emerges.
    return (
      <div className="dr-step-anim dr-step-anim-render">
        <div className="dr-step-render-frames">
          <div className="dr-step-render-frame dr-step-render-frame1" />
          <div className="dr-step-render-frame dr-step-render-frame2" />
          <div className="dr-step-render-frame dr-step-render-frame3" />
        </div>
        <div className="dr-step-render-final">
          <div className="dr-step-render-final-play">▶</div>
          <div className="dr-step-render-final-lyric">Vuelvo a empezar</div>
        </div>
        <div className="dr-step-render-badge">MP4 · Short · Thumbnail</div>
      </div>
    );
  }
  return null;
}

export default function DashboardStepper({ onPrimaryAction }) {
  const { t } = useI18n();
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem(DISMISS_KEY) === "1"; } catch { return false; }
  });
  // Pause auto-advance after the first user click so they read at their pace.
  const userInteractedRef = useRef(false);

  useEffect(() => {
    if (dismissed || userInteractedRef.current) return;
    const id = setTimeout(() => {
      setActive((cur) => (cur + 1) % 4);
    }, AUTO_ADVANCE_MS);
    return () => clearTimeout(id);
  }, [active, dismissed]);

  if (dismissed) return null;

  const handleStepClick = (i) => {
    userInteractedRef.current = true;
    setActive(i);
  };

  const dismiss = () => {
    try { localStorage.setItem(DISMISS_KEY, "1"); } catch {}
    setDismissed(true);
  };

  const steps = [
    {
      label: t("dash.stepper.s1.label") || "Subir",
      sub: t("dash.stepper.s1.sub") || "audio",
      body: t("dash.stepper.s1.body") || "Arrastrás tu MP3 o WAV. Procesamos hasta 5 simultáneos.",
      cta: t("dash.stepper.s1.cta") || "Probá vos →",
    },
    {
      label: t("dash.stepper.s2.label") || "Transcribir",
      sub: t("dash.stepper.s2.sub") || "con tecnología propia",
      body: t("dash.stepper.s2.body") ||
        "Motor GenLy transcribe con precisión palabra-por-palabra en 6 idiomas. Corrector automático incluido.",
      cta: t("dash.stepper.s2.cta") || "Ver demo →",
    },
    {
      label: t("dash.stepper.s3.label") || "Sincronizar",
      sub: t("dash.stepper.s3.sub") || "timestamps",
      body: t("dash.stepper.s3.body") ||
        "Sync automático con corrector inteligente. Editá a mano cualquier línea si querés.",
      cta: t("dash.stepper.s3.cta") || "Ver editor →",
    },
    {
      label: t("dash.stepper.s4.label") || "Renderizar",
      sub: t("dash.stepper.s4.sub") || "el video final",
      body: t("dash.stepper.s4.body") ||
        "Render en 1-3 minutos. Sale MP4, Short vertical y Thumbnail listos para subir.",
      cta: t("dash.stepper.s4.cta") || "Crear el primero →",
    },
  ];

  return (
    <section
      className="dr-stepper rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 md:p-6 mb-6"
      aria-labelledby="dr-stepper-title"
    >
      <header className="flex items-center justify-between mb-4 md:mb-5">
        <h3 id="dr-stepper-title" className="text-section text-gray-500 uppercase tracking-[0.18em]">
          {t("dash.stepper.title") || "Tu lyric video en 4 pasos"}
        </h3>
        <button
          type="button"
          onClick={dismiss}
          className="text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
          aria-label={t("dash.stepper.dismiss") || "Ocultar guía"}
        >
          {t("dash.stepper.dismiss") || "Ocultar"} ✕
        </button>
      </header>

      {/* Progress: 4 clickable steps connected by line */}
      <ol className="dr-stepper-progress" role="tablist">
        {steps.map((s, i) => (
          <li key={i} className="dr-stepper-progress-item">
            <button
              type="button"
              role="tab"
              aria-selected={active === i}
              onClick={() => handleStepClick(i)}
              className={`dr-stepper-step ${active === i ? "dr-stepper-step-active" : ""} ${i < active ? "dr-stepper-step-done" : ""}`}
            >
              <span className="dr-stepper-step-circle">{i + 1}</span>
              <span className="dr-stepper-step-label">
                <span className="dr-stepper-step-label-main">{s.label}</span>
                <span className="dr-stepper-step-label-sub">{s.sub}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>

      {/* Active panel: animation + body + CTA, keyed so it remounts on step
          change (forces CSS keyframes to restart for each step). */}
      <div className="dr-stepper-panel" key={active}>
        <div className="dr-stepper-panel-anim">
          <StepAnim step={active} />
        </div>
        <div className="dr-stepper-panel-body">
          <p>{steps[active].body}</p>
          {onPrimaryAction && (
            <button
              type="button"
              onClick={() => onPrimaryAction(active)}
              className="dr-stepper-cta"
            >
              {steps[active].cta}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
