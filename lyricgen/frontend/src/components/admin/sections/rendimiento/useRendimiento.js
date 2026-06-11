// Datos de la sección Rendimiento ("¿mejor o peor que antes?") — la capa
// analítica que vivía mezclada dentro de Operación (consolidación
// 2026-06-11): salud por cuenta, funnel operativo y KPIs con tendencia
// WoW derivados de /admin/metrics/timeseries.
//
// Nivel admin (no super-admin): es la vista de decisión compartida con
// el equipo; el comportamiento por usuario vive en Insights.
import { useEffect, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

// El funnel del backend acepta days 1..28 — clampear el período global.
function funnelDays(periodDays) {
  return Math.min(periodDays, 28);
}

// Suma una métrica de la serie {day: {tenant: {created, approved, ...}}}
// dentro de una ventana [from, to) expresada en días-atrás.
function sumWindow(series, key, fromDaysAgo, toDaysAgo) {
  const now = new Date();
  let total = 0;
  for (const [day, tenants] of Object.entries(series || {})) {
    const ageDays = (now - new Date(`${day}T00:00:00Z`)) / 86_400_000;
    if (ageDays >= toDaysAgo && ageDays < fromDaysAgo) {
      for (const m of Object.values(tenants)) total += m[key] || 0;
    }
  }
  return total;
}

export default function useRendimiento() {
  const { periodDays } = useAdmin();
  const [tenantHealth, setTenantHealth] = useState(null);
  const [funnel, setFunnel] = useState(null);
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      // Best-effort por bloque: un fetch caído no voltea la sección.
      try {
        const h = await fetchJson(`${API}/admin/metrics/health`);
        if (alive) setTenantHealth(h);
      } catch { /* sin cards */ }
      try {
        const f = await fetchJson(`${API}/admin/metrics/funnel?days=${funnelDays(periodDays)}`);
        if (alive) setFunnel(f);
      } catch { /* sin funnel */ }
      try {
        const t = await fetchJson(`${API}/admin/metrics/timeseries?days=28`);
        if (alive) setSeries(t?.series || null);
      } catch { /* sin KPIs WoW */ }
      if (alive) setLoading(false);
    })();
    return () => { alive = false; };
  }, [periodDays]);

  // KPIs 7d vs 7d previos (las dos ventanas para las que timeseries trae
  // margen — independientes del período global, siempre "última semana").
  const kpis = series
    ? {
        created: sumWindow(series, "created", 7, 0),
        createdPrev: sumWindow(series, "created", 14, 7),
        approved: sumWindow(series, "approved", 7, 0),
        approvedPrev: sumWindow(series, "approved", 14, 7),
        edits: sumWindow(series, "edit_requests", 7, 0),
        editsPrev: sumWindow(series, "edit_requests", 14, 7),
        aiCost: sumWindow(series, "ai_cost_usd", 7, 0),
        aiCostPrev: sumWindow(series, "ai_cost_usd", 14, 7),
      }
    : null;

  return { tenantHealth, funnel, kpis, loading };
}
