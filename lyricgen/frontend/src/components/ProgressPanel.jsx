const STEPS = [
  { key: "whisper", label: "Transcribiendo audio", icon: "waveform" },
  { key: "background", label: "Generando fondo", icon: "image" },
  { key: "video", label: "Creando lyric video", icon: "film" },
  { key: "short", label: "Creando YouTube Short", icon: "smartphone" },
  { key: "thumbnail", label: "Generando thumbnail", icon: "photo" },
];

const STEP_ORDER = STEPS.map((s) => s.key);

function getStepState(stepKey, currentStep) {
  const ci = STEP_ORDER.indexOf(currentStep);
  const si = STEP_ORDER.indexOf(stepKey);
  if (si < ci) return "done";
  if (si === ci) return "active";
  return "pending";
}

function StepIcon({ type, state }) {
  if (state === "done") {
    return (
      <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center">
        <svg className="w-5 h-5 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </div>
    );
  }
  if (state === "active") {
    return (
      <div className="w-10 h-10 rounded-xl bg-brand/15 flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }
  return (
    <div className="w-10 h-10 rounded-xl bg-surface-3/50 flex items-center justify-center">
      <div className="w-2.5 h-2.5 rounded-full bg-gray-600" />
    </div>
  );
}

export default function ProgressPanel({ status }) {
  const { current_step, progress } = status;

  return (
    <div className="w-full max-w-lg mt-16 animate-fade-in">
      <div className="text-center mb-10">
        <h2 className="text-2xl font-bold mb-2">Procesando</h2>
        <p className="text-gray-500">Esto puede tomar unos minutos</p>
      </div>

      {/* Progress bar */}
      <div className="mb-8">
        <div className="flex justify-between text-xs text-gray-500 mb-2">
          <span>Progreso</span>
          <span className="text-brand font-medium">{progress}%</span>
        </div>
        <div className="w-full h-2 bg-surface-2 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-700 ease-out"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Steps */}
      <div className="space-y-2">
        {STEPS.map((step) => {
          const state = getStepState(step.key, current_step);
          return (
            <div
              key={step.key}
              className={`flex items-center gap-4 px-4 py-3 rounded-2xl transition-all duration-300
                ${state === "active" ? "glass border border-brand/20" : ""}
              `}
            >
              <StepIcon type={step.icon} state={state} />
              <span className={`text-sm font-medium transition-colors duration-300 ${
                state === "active" ? "text-white" :
                state === "done" ? "text-gray-400" : "text-gray-600"
              }`}>
                {step.label}
              </span>
              {state === "active" && (
                <span className="ml-auto text-[11px] text-brand font-medium animate-pulse-slow">
                  En progreso
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
