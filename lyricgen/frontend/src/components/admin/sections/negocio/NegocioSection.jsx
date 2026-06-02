// Sección "Negocio" del Admin Panel v2 — sub-tabs "costos" y "invoices".
//
// LEGACY: PORT directo de las tabs "costs" e "invoices" del monolito
// AdminPanel.jsx. No se rediseñó nada (estructura JSX, labels y comportamiento
// idénticos al admin viejo). PR B lo rediseña — por ahora solo tiene que
// funcionar 100% dentro del shell nuevo.
//
// Cambios respecto del original, solo de plomería:
//   - API / authHeaders / fmtDate salen de adminApi
//   - flashError sale de useAdmin()
//   - los invoices usan StatusBadge (mapa INVOICE_STATUS) + EmptyState en vez
//     del badge/empty inline
//   - cada sub-tab carga su data al montar
import { useEffect, useState } from "react";

import { API, authHeaders, fmtDate, INVOICE_STATUS } from "../../adminApi";
import { useAdmin } from "../../AdminContext";
import SectionHeader from "../../layout/SectionHeader";
import StatusBadge from "../../primitives/StatusBadge";
import EmptyState from "../../primitives/EmptyState";

export default function NegocioSection({ subTab }) {
  const { flashError } = useAdmin();

  // --- Costos ---------------------------------------------------------------
  // Cost panel — populated by GET /admin/margin. Period selector lets the
  // operator switch between fresh (7d) and stable-average (90d) views;
  // revenue per video defaults to $8 (Universal contract: $2k / 250
  // videos) and is editable so we can model other deals.
  const [costSinceDays, setCostSinceDays] = useState(30);
  const [costRevenuePerVideo, setCostRevenuePerVideo] = useState(8);
  const [costDashboard, setCostDashboard] = useState(null);
  const [costLoading, setCostLoading] = useState(false);

  const loadCostDashboard = async () => {
    setCostLoading(true);
    try {
      const u = `${API}/admin/margin?since_days=${costSinceDays}` +
        `&revenue_per_video_usd=${costRevenuePerVideo}`;
      const res = await fetch(u, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCostDashboard(await res.json());
    } catch (err) {
      flashError(`No pude cargar el panel de costos: ${err.message || err}`);
    } finally {
      setCostLoading(false);
    }
  };

  useEffect(() => {
    if (subTab !== "costos") return;
    loadCostDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab, costSinceDays, costRevenuePerVideo]);

  // --- Invoices -------------------------------------------------------------
  const [invoices, setInvoices] = useState([]);

  const loadInvoices = async () => {
    try {
      const res = await fetch(`${API}/admin/invoices?limit=100`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setInvoices(data.invoices || []);
    } catch (err) {
      flashError(`No pude cargar las facturas: ${err.message || err}`);
    }
  };

  useEffect(() => {
    if (subTab !== "invoices") return;
    loadInvoices();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab]);

  // --- Render ---------------------------------------------------------------
  if (subTab === "costos") {
    return (
      <div>
        <SectionHeader
          title="Costos y márgenes"
          subtitle="Gasto IA, costo por deliverable y margen estimado contra el revenue por video."
        />
        <div className="space-y-6">
          {/* Period + revenue selectors */}
          <div className="glass rounded-card p-4 flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-gray-500 uppercase tracking-wide">Período</span>
              {[7, 30, 90].map((d) => (
                <button
                  key={d}
                  onClick={() => setCostSinceDays(d)}
                  className={`px-3 py-1 rounded-md text-xs ring-1 transition-colors ${
                    costSinceDays === d
                      ? "bg-brand/20 ring-brand/40 text-white"
                      : "ring-white/[0.06] text-gray-400 hover:text-white"
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-[11px] text-gray-500 uppercase tracking-wide">Revenue / video</span>
              <span className="text-xs text-gray-400">USD</span>
              <input
                type="number"
                step="0.5"
                min="0"
                value={costRevenuePerVideo}
                onChange={(e) => setCostRevenuePerVideo(Math.max(0, Number(e.target.value) || 0))}
                className="w-20 bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-xs text-white text-right"
              />
            </div>
          </div>

          {costLoading || !costDashboard ? (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
            </div>
          ) : (
            <>
              {/* Headline cards: spend, deliverable count, cost/video, margin */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Gasto IA total</p>
                  <p className="text-2xl font-bold tabular-nums">${costDashboard.total_cost.toFixed(2)}</p>
                  <p className="text-[11px] text-gray-500 mt-1">{costDashboard.total_calls} calls · últimos {costDashboard.since_days}d</p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Videos deliverable</p>
                  <p className="text-2xl font-bold tabular-nums">{costDashboard.video_counts.deliverable}</p>
                  <p className="text-[11px] text-gray-500 mt-1">{costDashboard.video_counts.done} done · {costDashboard.video_counts.pending_review} pending</p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Costo / deliverable</p>
                  <p className="text-2xl font-bold tabular-nums">
                    {costDashboard.cost_per_deliverable !== null
                      ? `$${costDashboard.cost_per_deliverable.toFixed(2)}`
                      : "—"}
                  </p>
                  <p className="text-[11px] text-gray-500 mt-1">
                    incluye rejects + retries
                  </p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Margen estimado</p>
                  <p className="text-2xl font-bold tabular-nums text-accent">
                    {costDashboard.margin_per_video !== null
                      ? `$${costDashboard.margin_per_video.toFixed(2)}`
                      : "—"}
                  </p>
                  <p className="text-[11px] text-gray-500 mt-1">
                    /video · total ${costDashboard.margin_total !== null
                      ? costDashboard.margin_total.toFixed(2)
                      : "—"}
                  </p>
                </div>
              </div>

              {/* Rejection rate + video counts breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Salud del pipeline</h3>
                  <span className="text-[11px] text-gray-500">% rejects + status counts</span>
                </div>
                <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Done</p>
                    <p className="text-base font-bold text-accent tabular-nums">{costDashboard.video_counts.done}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Pending</p>
                    <p className="text-base font-bold text-amber-400 tabular-nums">{costDashboard.video_counts.pending_review}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Rejected</p>
                    <p className="text-base font-bold text-red-400 tabular-nums">{costDashboard.video_counts.rejected}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Error</p>
                    <p className="text-base font-bold text-red-500 tabular-nums">{costDashboard.video_counts.error}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">% rejects</p>
                    <p className="text-base font-bold tabular-nums">
                      {costDashboard.rejection_rate !== null
                        ? `${(costDashboard.rejection_rate * 100).toFixed(1)}%`
                        : "—"}
                    </p>
                  </div>
                </div>
              </div>

              {/* Per-provider breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Desglose por proveedor</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_provider.length} buckets</span>
                </div>
                <div className="space-y-2">
                  {costDashboard.by_provider.map((p) => {
                    const pct = costDashboard.total_cost > 0
                      ? (p.cost / costDashboard.total_cost) * 100
                      : 0;
                    return (
                      <div key={p.provider} className="flex items-center gap-3">
                        <span className="w-20 text-xs font-medium capitalize">{p.provider}</span>
                        <div className="flex-1 h-2 rounded-full bg-surface-3/40 overflow-hidden">
                          <div
                            className="h-full bg-brand/60"
                            style={{ width: `${Math.min(100, pct)}%` }}
                          />
                        </div>
                        <span className="w-20 text-[11px] text-gray-400 tabular-nums text-right">
                          {p.calls} calls
                        </span>
                        <span className="w-20 text-xs font-mono tabular-nums text-right">
                          ${p.cost.toFixed(2)}
                        </span>
                        <span className="w-12 text-[11px] text-gray-500 tabular-nums text-right">
                          {pct.toFixed(0)}%
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Per-tenant breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Costo por tenant</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_tenant.length} tenants</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="text-gray-500 uppercase tracking-wide text-[10px]">
                        <th className="text-left font-medium pb-2 pr-3">Tenant</th>
                        <th className="text-right font-medium pb-2 px-3">Calls</th>
                        <th className="text-right font-medium pb-2 px-3">Gasto</th>
                        <th className="text-right font-medium pb-2 px-3">Done</th>
                        <th className="text-right font-medium pb-2 px-3">Pending</th>
                        <th className="text-right font-medium pb-2 px-3">Rejected</th>
                        <th className="text-right font-medium pb-2 px-3">Deliverable</th>
                        <th className="text-right font-medium pb-2 px-3">$/deliver</th>
                        <th className="text-right font-medium pb-2 pl-3">% rejects</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04]">
                      {costDashboard.by_tenant.map((t) => (
                        <tr key={t.tenant_id} className="hover:bg-white/[0.02]">
                          <td className="py-2 pr-3 font-mono text-white">{t.tenant_id || "—"}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{t.calls}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-white">${t.cost.toFixed(2)}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-accent">{t.done}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-amber-400">{t.pending_review}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-red-400">{t.rejected}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{t.deliverable}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-gray-300">
                            {t.cost_per_deliverable !== null ? `$${t.cost_per_deliverable.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 pl-3 text-right tabular-nums text-gray-400">
                            {t.rejection_rate !== null ? `${(t.rejection_rate * 100).toFixed(1)}%` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Per-user breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Costo por usuario</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_user.length} usuarios</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="text-gray-500 uppercase tracking-wide text-[10px]">
                        <th className="text-left font-medium pb-2 pr-3">Usuario</th>
                        <th className="text-left font-medium pb-2 px-3">Tenant</th>
                        <th className="text-right font-medium pb-2 px-3">Calls</th>
                        <th className="text-right font-medium pb-2 px-3">Gasto</th>
                        <th className="text-right font-medium pb-2 px-3">Done</th>
                        <th className="text-right font-medium pb-2 px-3">Pending</th>
                        <th className="text-right font-medium pb-2 px-3">Rejected</th>
                        <th className="text-right font-medium pb-2 px-3">$/deliver</th>
                        <th className="text-right font-medium pb-2 pl-3">% rejects</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04]">
                      {costDashboard.by_user.map((u) => (
                        <tr key={`${u.user_id}|${u.tenant_id}`} className="hover:bg-white/[0.02]">
                          <td className="py-2 pr-3 text-white">
                            {u.username || <span className="text-gray-500 italic">user #{u.user_id ?? "—"}</span>}
                          </td>
                          <td className="py-2 px-3 font-mono text-gray-400">{u.tenant_id || "—"}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{u.calls}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-white">${u.cost.toFixed(2)}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-accent">{u.done}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-amber-400">{u.pending_review}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-red-400">{u.rejected}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-gray-300">
                            {u.cost_per_deliverable !== null ? `$${u.cost_per_deliverable.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 pl-3 text-right tabular-nums text-gray-400">
                            {u.rejection_rate !== null ? `${(u.rejection_rate * 100).toFixed(1)}%` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Per-tool detail (collapsed by default mental load — just a small table) */}
              <details className="glass rounded-card p-5">
                <summary className="text-xs text-gray-400 cursor-pointer select-none">
                  Detalle por modelo ({costDashboard.by_tool.length} tools)
                </summary>
                <div className="mt-4 space-y-1.5">
                  {costDashboard.by_tool.map((t) => (
                    <div key={`${t.tool_name}|${t.tool_provider}`} className="flex items-center gap-3 text-[11px]">
                      <span className="flex-1 font-mono text-gray-300 truncate">{t.tool_name}</span>
                      <span className="text-gray-500">{t.tool_provider}</span>
                      <span className="w-16 text-right tabular-nums">{t.calls}×</span>
                      <span className="w-16 text-right tabular-nums font-mono">${t.rate_per_call.toFixed(3)}</span>
                      <span className="w-20 text-right tabular-nums font-mono text-white">${t.cost.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              </details>

              <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] p-4 space-y-2">
                <p className="text-[11px] text-gray-300 font-medium uppercase tracking-wide">
                  Cómo leer estos números
                </p>
                <ul className="text-[10px] text-gray-500 leading-relaxed list-disc pl-4 space-y-1">
                  <li>
                    <b>Veo Fast</b> a $0.80/call (palindrome loop 8s) · <b>Veo Standard</b> $3.20.
                  </li>
                  <li>
                    <b>Whisper</b> cobrado como API de OpenAI a ~$0.006/min de audio (estimado en $0.021/call · canción promedio ~3.5 min). Las canciones más largas pueden costar +50%.
                  </li>
                  <li>
                    <b>Margen</b> calculado contra revenue editable arriba (default $8/video = contrato Universal $2k / 250 videos). No incluye costos de infra (Railway + R2 ≈ $50/mes fijo) ni Stripe fees.
                  </li>
                  <li>
                    <b>Costo / deliverable</b> incluye rejects y retries — por eso es mayor que el marginal de un render limpio.
                  </li>
                  <li>
                    <b>Veo in-flight</b> (calls con duration NULL): se cuentan como costo aunque Google probablemente no las facture si el render no completó. Sobre-estimación máxima ~$1.60.
                  </li>
                  <li>
                    Provenance de Whisper antes del 2026-05-13 fue backfilled con rows sintéticas (un row por job que llegó a status done/pending/rejected/editing). Jobs nuevos quedan tracked automáticamente.
                  </li>
                </ul>
              </div>
            </>
          )}
        </div>
      </div>
    );
  }

  if (subTab === "invoices") {
    return (
      <div>
        <SectionHeader
          title="Facturación"
          subtitle="Facturas emitidas y su estado de cobro."
        />
        <div className="space-y-4">
          <div className="glass rounded-card overflow-hidden">
            {invoices.length === 0 ? (
              <EmptyState
                title="Sin facturas todavía"
                message="Cuando se emita la primera factura va a aparecer acá."
              />
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/[0.06]">
                    <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Date</th>
                    <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Description</th>
                    <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Amount</th>
                    <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Status</th>
                    <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Invoice</th>
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((inv) => (
                    <tr key={inv.id} className="border-b border-white/[0.03]">
                      <td className="px-4 py-3 text-xs text-gray-400">{fmtDate(inv.created_at)}</td>
                      <td className="px-4 py-3">{inv.description || "—"}</td>
                      <td className="px-4 py-3 font-medium">${inv.amount?.toFixed(2)}</td>
                      <td className="px-4 py-3">
                        <StatusBadge status={inv.status} map={INVOICE_STATUS} />
                      </td>
                      <td className="px-4 py-3">
                        {inv.invoice_url ? (
                          <a href={inv.invoice_url} target="_blank" rel="noopener noreferrer"
                            className="text-xs text-brand hover:text-brand-light">View</a>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    );
  }

  return null;
}
