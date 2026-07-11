// Tira de salud del sistema — full-width, live, refresh cada 15 s.
//
// Lee el objeto /health (defensivo con optional chaining porque el endpoint
// puede devolver formas parciales o fallar):
//   { status, db, redis, r2, workers_alive, queue_depth: {enterprise, default},
//     disk_free_gb, api_keys: {openai, vertex}, degraded_reason }
//
// El punto de estado: ok=accent, degraded=amber, error/otro=red.
function Pill({ ok, label, value }) {
  return (
    <div className="flex items-center gap-2 text-caption">
      <span className={`w-2 h-2 rounded-full shrink-0 ${ok ? "bg-accent" : "bg-red-400"}`} />
      <span className="text-gray-400">{label}</span>
      <span className="font-mono text-white">{value}</span>
    </div>
  );
}

export default function HealthStrip({ health, issueCount = 0, stuckCount = 0 }) {
  if (!health) {
    return (
      <div className="glass rounded-card px-5 py-4 text-caption text-gray-500">
        Cargando estado del sistema…
      </div>
    );
  }

  const serviceDown = health.status !== "ok" || health.db !== "up" || health.redis !== "up" || (health.workers_alive || 0) < 1;
  const status = serviceDown ? "incident" : (issueCount > 0 || stuckCount > 0) ? "degraded" : "operational";
  const statusLabel = status === "operational" ? "Operativo" : status === "degraded" ? "Degradado" : "Incidente";
  const dotColor = status === "operational" ? "bg-accent" : status === "degraded" ? "bg-amber-400" : "bg-red-400";
  const textColor = status === "operational" ? "text-accent" : status === "degraded" ? "text-amber-400" : "text-red-400";

  const queue = health.queue_depth || {};
  const totalQueue = (queue.enterprise ?? 0) + (queue.default ?? 0);

  return (
    <div className="glass rounded-card px-5 py-4">
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className={`w-2.5 h-2.5 rounded-full ${dotColor}`} />
          <span className={`text-section uppercase font-bold tracking-wider ${textColor}`}>
            Sistema {statusLabel}
          </span>
          {health.degraded_reason && (
            <span className="text-caption text-amber-300">· {health.degraded_reason}</span>
          )}
          {!health.degraded_reason && status === "degraded" && <span className="text-caption text-amber-300">· {issueCount} errores del período{stuckCount > 0 ? ` · ${stuckCount} atascados` : ""}</span>}
        </div>
        <span className="text-section text-gray-500">actualiza cada 15 s</span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2">
        <Pill ok={health.db === "up"} label="DB" value={health.db || "?"} />
        <Pill ok={health.redis === "up"} label="Redis" value={health.redis || "?"} />
        <Pill ok={["configured", "ready", "up"].includes(health.r2)} label="R2" value={health.r2 || "?"} />
        <Pill ok={(health.workers_alive || 0) > 0} label="Workers" value={health.workers_alive ?? "?"} />
        <Pill
          ok={totalQueue < 50}
          label="Cola"
          value={`${totalQueue} (ent ${queue.enterprise ?? 0} + def ${queue.default ?? 0})`}
        />
        <Pill ok={(health.disk_free_gb ?? 0) > 10} label="Disco libre" value={`${health.disk_free_gb ?? "?"} GB`} />
        <Pill ok={!!health.api_keys?.openai} label="OpenAI" value={health.api_keys?.openai ? "ok" : "missing"} />
        <Pill ok={!!health.api_keys?.vertex} label="Vertex" value={health.api_keys?.vertex ? "ok" : "missing"} />
      </div>
    </div>
  );
}
