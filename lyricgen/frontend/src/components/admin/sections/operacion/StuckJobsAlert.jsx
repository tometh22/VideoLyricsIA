// Alerta de jobs zombie. Solo se renderiza cuando hay zombies (count > 0).
// Es lo primero que ve el operador: jobs colgados en "processing" que el
// reaper marcará como error en el próximo ciclo, o se pueden matar a mano.
//
// Debajo va el toast de resultado del reaper — queda visible tras una corrida
// manual para confirmar qué hizo el runbook (descartable).
export default function StuckJobsAlert({
  stuckJobs,
  reaperRunning,
  runReaperNow,
  reaperResult,
  onDismissResult,
}) {
  const count = stuckJobs?.count || 0;
  const tenants = [...new Set((stuckJobs?.jobs || []).map((j) => j.tenant_id))]
    .filter(Boolean)
    .slice(0, 5)
    .join(", ");

  if (count <= 0) {
    // Aun sin zombies, el toast del reaper puede quedar visible tras una corrida.
    if (!reaperResult) return null;
    return <ReaperToast reaperResult={reaperResult} onDismiss={onDismissResult} />;
  }

  return (
    <div className="space-y-3">
      <div className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-5 py-4 flex items-center gap-3 flex-wrap">
        <svg className="w-5 h-5 text-red-300 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path d="M12 9v4M12 17h.01" /><circle cx="12" cy="12" r="10" />
        </svg>
        <div className="flex-1 min-w-0">
          <p className="text-ui font-semibold text-red-200">
            {count} {count === 1 ? "job zombie" : "jobs zombies"} detectado{count > 1 ? "s" : ""}
          </p>
          <p className="text-caption text-red-300/80 mt-0.5">
            En "processing" hace más de {stuckJobs?.threshold_min ?? 100} min sin avanzar. El reaper los va a marcar como error en el próximo ciclo (≤5 min), o forzá ahora ↓.
            {tenants && <> Tenants: {tenants}</>}
          </p>
        </div>
        <button
          onClick={runReaperNow}
          disabled={reaperRunning}
          className="text-caption px-3 py-1.5 rounded-button bg-red-500/20 hover:bg-red-500/30 text-red-100 font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-brand shrink-0"
          title="Marca todos los zombies como error inmediatamente"
        >
          {reaperRunning ? "Ejecutando…" : "Forzar reaper ahora"}
        </button>
      </div>

      {reaperResult && <ReaperToast reaperResult={reaperResult} onDismiss={onDismissResult} />}
    </div>
  );
}

function ReaperToast({ reaperResult, onDismiss }) {
  const isError = !!reaperResult.error;
  const killed = reaperResult.count || 0;
  const tone = isError
    ? "bg-red-500/[0.08] ring-red-500/30 text-red-200"
    : killed > 0
      ? "bg-amber-500/[0.06] ring-amber-500/25 text-amber-200"
      : "bg-accent/[0.06] ring-accent/20 text-accent";
  return (
    <div className={`rounded-card ring-1 px-5 py-3 flex items-center gap-3 text-ui ${tone}`}>
      <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
        {isError
          ? <><circle cx="12" cy="12" r="10" /><path d="M15 9l-6 6M9 9l6 6" /></>
          : <><path d="M22 11.08V12a10 10 0 11-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></>}
      </svg>
      <div className="flex-1">
        {isError
          ? <>Falló el runbook: {reaperResult.error}</>
          : killed > 0
            ? <>Se mataron {killed} job{killed > 1 ? "s" : ""} colgado{killed > 1 ? "s" : ""}.</>
            : <>Reaper ejecutado · ningún zombie encontrado.</>}
      </div>
      <button onClick={onDismiss} className="text-caption opacity-60 hover:opacity-100">×</button>
    </div>
  );
}
