const STEPS = [
  { key: "whisper", label: "Transcribiendo audio con Whisper" },
  { key: "background", label: "Generando fondo con IA" },
  { key: "video", label: "Generando lyric video Full HD" },
  { key: "short", label: "Creando YouTube Short" },
  { key: "thumbnail", label: "Generando thumbnail" },
];

const STEP_ORDER = STEPS.map((s) => s.key);

function getStepState(stepKey, currentStep) {
  const currentIdx = STEP_ORDER.indexOf(currentStep);
  const stepIdx = STEP_ORDER.indexOf(stepKey);
  if (stepIdx < currentIdx) return "done";
  if (stepIdx === currentIdx) return "active";
  return "pending";
}

export default function ProgressPanel({ status }) {
  const { current_step, progress } = status;

  return (
    <div className="w-full max-w-2xl mt-10 space-y-6">
      {/* Progress bar */}
      <div className="w-full bg-surface-light rounded-full h-3 overflow-hidden">
        <div
          className="h-full bg-brand rounded-full transition-all duration-500"
          style={{ width: `${progress}%` }}
        />
      </div>
      <p className="text-center text-gray-400 text-sm">{progress}% completado</p>

      {/* Steps */}
      <div className="space-y-3">
        {STEPS.map((step) => {
          const state = getStepState(step.key, current_step);
          return (
            <div
              key={step.key}
              className={`flex items-center gap-3 p-4 rounded-xl transition
                ${state === "active" ? "bg-brand/10 border border-brand/30" : "bg-surface-light"}`}
            >
              {state === "done" && (
                <span className="text-green-400 text-xl">&#10003;</span>
              )}
              {state === "active" && (
                <span className="inline-block w-5 h-5 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              )}
              {state === "pending" && (
                <span className="w-5 h-5 rounded-full border-2 border-gray-600" />
              )}
              <span
                className={
                  state === "active"
                    ? "text-white"
                    : state === "done"
                    ? "text-gray-400"
                    : "text-gray-600"
                }
              >
                {step.label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
