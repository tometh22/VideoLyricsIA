// Funnel del wizard (ui_events): hasta qué paso llega cada sesión de
// creación, dónde se abandona y conversión a generate. Vacío hasta que la
// telemetría acumule eventos post-deploy — el EmptyState lo dice
// explícitamente para que no parezca un bug.
import EmptyState from "../../primitives/EmptyState";

const STEP_LABELS = {
  1: "1 · Subir audio",
  2: "2 · Modo de fondo",
  3: "3 · Movimiento",
  4: "4 · Tipografía & animación",
  5: "5 · Entrega",
  6: "6 · Review de lyrics",
};

const SCENE_MODE_LABELS = {
  auto: "Auto (IA)",
  lyrics: "Inspirado en letra",
  prompt: "Prompt propio",
  library: "Biblioteca",
  custom: "Archivo propio",
};

function fmtMins(seconds) {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

export default function WizardFunnelPanel({ wizard }) {
  if (!wizard) return null;

  if (wizard.empty) {
    return (
      <div className="glass rounded-card p-5">
        <p className="text-section uppercase text-gray-500 mb-3">Funnel del wizard</p>
        <EmptyState
          title="Recolectando datos"
          message={
            wizard.telemetry_enabled
              ? "El tracking del wizard arranca con este deploy — los primeros funnels aparecen cuando los usuarios creen videos."
              : "El tracking está apagado (TELEMETRY_ENABLED). Prendelo en el server para empezar a registrar el comportamiento del wizard."
          }
        />
      </div>
    );
  }

  const max = Math.max(wizard.sessions_total, 1);
  const abandons = Object.entries(wizard.abandon_by_step || {});

  return (
    <div className="glass rounded-card p-5">
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <p className="text-section uppercase text-gray-500">
          Funnel del wizard · {wizard.sessions_total} sesiones
        </p>
        <span className="text-label text-gray-400">
          {wizard.sessions_generated} generaron
          {wizard.conversion != null && (
            <span className="text-white font-medium"> · {Math.round(wizard.conversion * 100)}% conversión</span>
          )}
          {wizard.p50_to_generate_s != null && (
            <span className="text-gray-500"> · p50 hasta generar {fmtMins(wizard.p50_to_generate_s)}</span>
          )}
        </span>
      </div>

      <div className="space-y-2">
        {(wizard.funnel || []).map((f) => (
          <div key={f.step} className="flex items-center gap-3">
            <span className="text-label text-gray-400 w-44 shrink-0">{STEP_LABELS[f.step] || f.step}</span>
            <div className="flex-1 h-5 bg-white/[0.04] rounded overflow-hidden">
              <div
                className="h-full bg-brand/60 rounded flex items-center px-2"
                style={{ width: `${Math.max(4, Math.round((f.reached / max) * 100))}%` }}
              >
                <span className="text-label text-white tabular-nums">{f.reached}</span>
              </div>
            </div>
            {wizard.abandon_by_step?.[String(f.step)] > 0 && (
              <span className="text-label text-amber-400 w-24 shrink-0 text-right">
                {wizard.abandon_by_step[String(f.step)]} abandonos
              </span>
            )}
          </div>
        ))}
      </div>

      {(Object.keys(wizard.scene_modes || {}).length > 0 || abandons.length > 0) && (
        <div className="mt-4 pt-3 border-t border-white/[0.04] flex flex-wrap gap-1.5">
          {Object.entries(wizard.scene_modes || {})
            .sort((a, b) => b[1] - a[1])
            .map(([mode, n]) => (
              <span key={mode} className="px-2 py-0.5 rounded-full bg-surface-3/50 ring-1 ring-white/[0.06] text-label text-gray-400">
                Fondo: {SCENE_MODE_LABELS[mode] || mode} × {n}
              </span>
            ))}
          {wizard.library?.selects > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-surface-3/50 ring-1 ring-white/[0.06] text-label text-gray-400">
              Assets de biblioteca elegidos: {wizard.library.selects}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
