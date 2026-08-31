// Bloque "A quién se le fue" — `/admin/margin`.
//
// Este bloque NO es la factura
// ----------------------------
// Es el costo MODELADO: filas de `ai_provenance` multiplicadas por una tabla
// de tarifas nuestra. Existe porque una factura no puede decir a qué cliente
// pertenece un dólar — Google cobra por proyecto, no por tenant. Es el único
// que sabe repartir, y por eso vale aunque no cuadre.
//
// Y no cuadra: la reconciliación de jun-2026 dio $163 modelado contra $199,53
// facturado (-18%). Poner los dos números en la misma página sin decir esto
// sería presentar una estimación con cara de factura. De ahí el cartel de
// arriba y el porcentaje de desvío, que es lo que avisa cuando las tarifas se
// quedaron viejas.
import { useState } from "react";

import { fmtMoneyOrDash as dinero } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import KpiCard from "../../primitives/KpiCard";

const pct = (r) => (r === null || r === undefined ? "—" : `${(r * 100).toFixed(1)}%`);

const num = (cls) => (v) => (
  <span className={`tabular-nums ${cls}`}>{v ?? 0}</span>
);

export default function CostoAtribuidoView({ data, loading, facturado }) {
  const [verUsuarios, setVerUsuarios] = useState(false);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }
  if (!data) {
    return (
      <EmptyState
        title="Sin atribución para este período"
        message="No hay llamadas a proveedores de IA registradas en la ventana."
      />
    );
  }

  const modelado = data.total_cost || 0;
  // Desvío contra la factura del bloque 1. Es el único chequeo automático de
  // si la tabla de tarifas sigue siendo válida: una tarifa silenciosamente
  // vieja envenena TODA la atribución por tenant, porque el error se
  // reparte proporcionalmente y no se nota en ninguna fila.
  const desvio = facturado > 0 ? (modelado - facturado) / facturado : null;
  const desviaMucho = desvio !== null && Math.abs(desvio) > 0.25;

  const tenantCols = [
    { key: "tenant", header: "Tenant",
      render: (t) => <span className="font-mono text-white">{t.tenant_id || "—"}</span> },
    { key: "calls", header: "Calls", align: "right",
      render: (t) => num("text-gray-300")(t.calls) },
    { key: "cost", header: "Gasto modelado", align: "right",
      render: (t) => <span className="tabular-nums font-mono text-white">{dinero(t.cost)}</span> },
    { key: "share", header: "% del total", align: "right",
      render: (t) => (
        <span className="tabular-nums text-gray-400">
          {modelado > 0 ? `${((t.cost / modelado) * 100).toFixed(1)}%` : "—"}
        </span>
      ) },
    // Done / Pending / Rejected van a la tabla, no sólo el agregado:
    // un tenant con 40% de rechazos paga el doble por cada entregable y
    // eso no se ve en ninguna otra columna.
    { key: "done", header: "Done", align: "right",
      render: (t) => num("text-accent")(t.done) },
    { key: "pending", header: "Pending", align: "right",
      render: (t) => num("text-amber-400")(t.pending_review) },
    { key: "rejected", header: "Rejected", align: "right",
      render: (t) => num("text-red-400")(t.rejected) },
    { key: "cpd", header: "$/entregable", align: "right",
      render: (t) => <span className="tabular-nums font-mono text-gray-300">{dinero(t.cost_per_deliverable)}</span> },
    { key: "rejrate", header: "% rechazo", align: "right",
      render: (t) => <span className="tabular-nums text-gray-400">{pct(t.rejection_rate)}</span> },
  ];

  const userCols = [
    { key: "user", header: "Usuario",
      render: (u) => (
        <span className="text-white">
          {u.username || <span className="text-gray-500 italic">user #{u.user_id ?? "—"}</span>}
        </span>
      ) },
    { key: "tenant", header: "Tenant",
      render: (u) => <span className="font-mono text-gray-400">{u.tenant_id || "—"}</span> },
    { key: "calls", header: "Calls", align: "right",
      render: (u) => num("text-gray-300")(u.calls) },
    { key: "cost", header: "Gasto", align: "right",
      render: (u) => <span className="tabular-nums font-mono text-white">{dinero(u.cost)}</span> },
    { key: "done", header: "Done", align: "right",
      render: (u) => num("text-accent")(u.done) },
    { key: "pending", header: "Pending", align: "right",
      render: (u) => num("text-amber-400")(u.pending_review) },
    { key: "rejected", header: "Rejected", align: "right",
      render: (u) => num("text-red-400")(u.rejected) },
    { key: "cpd", header: "$/entregable", align: "right",
      render: (u) => <span className="tabular-nums font-mono text-gray-300">{dinero(u.cost_per_deliverable)}</span> },
    { key: "rejrate", header: "% rechazo", align: "right",
      render: (u) => <span className="tabular-nums text-gray-400">{pct(u.rejection_rate)}</span> },
  ];

  return (
    <div className="space-y-4">
      {/* El cartel va PRIMERO. Después de leer un total facturado, el ojo
          toma cualquier otro total de la misma página como si fuera igual
          de firme. */}
      <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.06] p-3">
        <p className="text-caption text-gray-300">
          Esto es una <b>estimación</b>, no la factura: llamadas registradas ×
          tarifas nuestras. Es el único corte que sabe decir de quién fue el
          gasto — el proveedor cobra por proyecto, no por cliente.
        </p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          value={dinero(modelado)}
          label="Gasto IA modelado"
          hint={`${data.total_calls ?? 0} llamadas`}
        />
        <KpiCard
          value={desvio === null ? "—" : `${desvio > 0 ? "+" : ""}${(desvio * 100).toFixed(0)}%`}
          label="Desvío vs facturado"
          tone={desviaMucho ? "warn" : "default"}
          hint={facturado > 0 ? `factura ${dinero(facturado)}` : "sin factura para comparar"}
        />
        <KpiCard
          value={data.video_counts?.deliverable ?? 0}
          label="Entregables"
          hint={`${data.video_counts?.done ?? 0} done · ${data.video_counts?.pending_review ?? 0} pending`}
        />
        <KpiCard
          value={dinero(data.cost_per_deliverable)}
          label="Costo IA / entregable"
          hint="incluye rechazos y reintentos"
        />
      </div>

      {desviaMucho && (
        <div className="rounded-card bg-amber-400/[0.08] ring-1 ring-amber-400/25 p-3">
          <p className="text-caption text-amber-200">
            El modelo se despegó <b>{Math.abs(desvio * 100).toFixed(0)}%</b> de
            la factura. La tabla de tarifas quedó vieja, y ese error se reparte
            proporcionalmente entre TODOS los tenants de abajo: ninguna fila se
            ve mal por su cuenta.
          </p>
        </div>
      )}

      {/* Salud del pipeline y calidad del número.
          `waste` y `row_quality` ya venían en la respuesta y no se
          mostraban en ninguna pantalla: son la respuesta a "¿cuánto de este
          total es incierto?" y a "¿cuánto no produjo nada?". */}
      <div className="glass-elevated rounded-card p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold">Salud del pipeline</h3>
          <span className="text-label text-gray-500">
            {pct(data.rejection_rate)} de rechazo
          </span>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {[
            ["Done", data.video_counts?.done, "text-accent"],
            ["Pending", data.video_counts?.pending_review, "text-amber-400"],
            ["Rejected", data.video_counts?.rejected, "text-red-400"],
            ["Error", data.video_counts?.error, "text-red-500"],
            ["% rechazo", pct(data.rejection_rate), "text-white"],
          ].map(([label, valor, color]) => (
            <div key={label}>
              <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">
                {label}
              </p>
              <p className={`text-base font-bold tabular-nums ${color}`}>
                {valor ?? 0}
              </p>
            </div>
          ))}
        </div>

        {(data.waste || data.row_quality) && (
          <div className="mt-4 pt-4 border-t border-white/[0.06] grid grid-cols-1 sm:grid-cols-3 gap-3">
            {data.waste?.waste_ratio !== undefined && (
              <div>
                <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">
                  Gasto que no entregó
                </p>
                <p className="text-base font-bold tabular-nums text-amber-400">
                  {pct(data.waste.waste_ratio)}
                </p>
                <p className="text-label text-gray-500 leading-snug">
                  previews descartados, rechazos y reintentos
                </p>
              </div>
            )}
            {data.row_quality && (
              <>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">
                    Cache hits (no cobrados)
                  </p>
                  <p className="text-base font-bold tabular-nums text-gray-300">
                    {data.row_quality.cache_hits_excluded ?? 0}
                  </p>
                  <p className="text-label text-gray-500 leading-snug">
                    excluidos del total de arriba
                  </p>
                </div>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">
                    Llamadas inciertas
                  </p>
                  <p className="text-base font-bold tabular-nums text-gray-300">
                    {(data.row_quality.errored_included ?? 0)
                      + (data.row_quality.in_flight_included ?? 0)}
                  </p>
                  <p className="text-label text-gray-500 leading-snug">
                    con error o en vuelo — SÍ se cuentan: puede que las cobren
                  </p>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Por proveedor */}
      <div className="glass-elevated rounded-card p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold">Por proveedor</h3>
          <span className="text-label text-gray-500">
            {(data.by_provider || []).length} proveedores
          </span>
        </div>
        <div className="space-y-2">
          {(data.by_provider || []).map((p) => {
            const share = modelado > 0 ? (p.cost / modelado) * 100 : 0;
            return (
              <div key={p.provider} className="flex items-center gap-3">
                <span className="w-20 text-caption font-medium capitalize">{p.provider}</span>
                <div className="flex-1 h-2 rounded-full bg-surface-3/40 overflow-hidden">
                  <div className="h-full bg-brand/60" style={{ width: `${Math.min(100, share)}%` }} />
                </div>
                <span className="w-20 text-label text-gray-400 tabular-nums text-right">{p.calls} calls</span>
                <span className="w-20 text-caption font-mono tabular-nums text-right">{dinero(p.cost)}</span>
                <span className="w-12 text-label text-gray-500 tabular-nums text-right">{share.toFixed(0)}%</span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Por tenant o por usuario — el mismo espacio, no dos tablas
          apiladas: son la misma pregunta con distinto zoom. */}
      <div className="glass-elevated rounded-card p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold">
            {verUsuarios ? "Por usuario" : "Por tenant"}
          </h3>
          <div className="flex items-center gap-2">
            <span className="text-label text-gray-500">
              {(verUsuarios ? data.by_user : data.by_tenant || []).length} filas
            </span>
            <button
              onClick={() => setVerUsuarios((v) => !v)}
              className="text-caption px-3 py-1.5 rounded-lg glass hover:bg-white/[0.06] transition-colors"
            >
              {verUsuarios ? "Ver por tenant" : "Ver por usuario"}
            </button>
          </div>
        </div>
        <DataTable
          dense
          columns={verUsuarios ? userCols : tenantCols}
          rows={(verUsuarios ? data.by_user : data.by_tenant) || []}
          rowKey={(r) => (verUsuarios ? `${r.user_id}|${r.tenant_id}` : r.tenant_id)}
          empty={<EmptyState title="Sin gasto atribuido en el período" />}
        />
      </div>

      {/* Detalle por modelo. Es donde se ve si una tarifa envejeció: el
          `$/call` efectivo es el que se usa para todo lo de arriba. */}
      <details className="glass rounded-card p-5">
        <summary className="text-caption text-gray-400 cursor-pointer select-none">
          Tarifas efectivas por modelo ({(data.by_tool || []).length})
        </summary>
        <div className="mt-4 space-y-1.5">
          {(data.by_tool || []).map((t) => (
            <div key={`${t.tool_name}|${t.tool_provider}`}
                 className="flex items-center gap-3 text-label">
              <span className="flex-1 font-mono text-gray-300 truncate">{t.tool_name}</span>
              <span className="text-gray-500">{t.tool_provider}</span>
              <span className="w-16 text-right tabular-nums">{t.calls}×</span>
              <span className="w-16 text-right tabular-nums font-mono">
                ${Number(t.rate_per_call ?? 0).toFixed(3)}
              </span>
              <span className="w-20 text-right tabular-nums font-mono text-white">
                {dinero(t.cost)}
              </span>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
