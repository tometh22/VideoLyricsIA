import { WORKFLOW_STEPS } from "./changeRequestWorkflow";

const TONES = {
  action: "bg-brand/10 text-brand-light ring-brand/20",
  attention: "bg-amber-500/10 text-amber-100 ring-amber-400/20",
  busy: "bg-sky-500/10 text-sky-100 ring-sky-400/20",
  done: "bg-emerald-500/10 text-emerald-100 ring-emerald-400/20",
  idle: "bg-surface-2/50 text-gray-300 ring-white/[0.06]",
};

export default function RequestWorkflowStepper({ workflow }) {
  const activeStep = Math.min(workflow?.activeStep || 0, WORKFLOW_STEPS.length);
  return (
    <div className="space-y-3" aria-label="Progreso del pedido">
      <div className={`rounded-xl px-4 py-3 ring-1 ${TONES[workflow?.tone] || TONES.idle}`}>
        <p className="text-caption font-semibold">{workflow?.label}</p>
        <p className="text-label opacity-75 mt-0.5">{workflow?.detail}</p>
      </div>
      <ol className="grid grid-cols-5 gap-1" aria-label="Etapas del pedido">
        {WORKFLOW_STEPS.map((step, index) => {
          const complete = activeStep > index;
          const active = activeStep === index;
          return (
            <li key={step.id} className="min-w-0">
              <div className={`h-1 rounded-full ${
                complete ? "bg-emerald-400" : active ? "bg-brand" : "bg-white/[0.08]"
              }`} />
              <p className={`mt-1.5 truncate text-[10px] font-medium ${
                complete ? "text-emerald-300" : active ? "text-white" : "text-gray-600"
              }`}>
                {step.label}
              </p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
