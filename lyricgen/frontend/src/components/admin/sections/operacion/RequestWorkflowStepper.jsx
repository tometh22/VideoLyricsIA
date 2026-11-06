import {
  STEP_STATE_LABELS,
  correctionSentence,
  correctionStepList,
} from "./changeRequestWorkflow";

const TONES = {
  action: "bg-brand/10 text-brand-light ring-brand/20",
  attention: "bg-amber-500/10 text-amber-100 ring-amber-400/20",
  busy: "bg-sky-500/10 text-sky-100 ring-sky-400/20",
  done: "bg-emerald-500/10 text-emerald-100 ring-emerald-400/20",
  idle: "bg-surface-2/50 text-gray-300 ring-white/[0.06]",
};

// Tres pasos (Corregir, Generar el video nuevo, Publicar) y UNA frase que
// explica en qué punto está el pedido. `steps` y `sentence` se pueden pasar ya
// calculados; si no, se derivan del `workflow` del servidor.
export default function RequestWorkflowStepper({ workflow, steps, sentence }) {
  const list = steps || correctionStepList(workflow);
  const text = sentence || correctionSentence(workflow);
  return (
    <div className="space-y-3" role="group" aria-label="Progreso del pedido">
      <ol className="grid grid-cols-3 gap-2" aria-label="Pasos del pedido">
        {list.map((step, index) => (
          <li key={step.key} className="min-w-0" aria-current={step.state === "active" ? "step" : undefined}>
            <div className={`h-1 rounded-full ${
              step.state === "done" ? "bg-emerald-400" : step.state === "active" ? "bg-brand" : "bg-white/[0.08]"
            }`} />
            <p className={`mt-1.5 text-label font-medium leading-tight ${
              step.state === "done" ? "text-emerald-300" : step.state === "active" ? "text-white" : "text-gray-500"
            }`}>
              <span aria-hidden="true">{step.state === "done" ? "✓" : index + 1}. </span>
              {step.label}
              <span className="sr-only"> ({STEP_STATE_LABELS[step.state]})</span>
            </p>
          </li>
        ))}
      </ol>
      <p className={`rounded-xl px-4 py-3 text-caption ring-1 ${TONES[workflow?.tone] || TONES.idle}`}>
        {text}
      </p>
    </div>
  );
}
