// Hook de datos de la sección "Negocio" del Admin Panel v2.
//
// Dueño de dos datasets independientes:
//   - Costos: el panel de márgenes (/admin/margin). El selector de período
//     deja al operador ver el promedio fresco (7d) o estable (90d); el revenue
//     por video default $8 (contrato Universal $2k / 250 videos) es editable
//     para modelar otros deals. Recarga al montar y cuando cambia período o
//     revenue.
//   - Invoices: la lista de facturas (/admin/invoices). Carga al montar.
//
// Errores → flashError (mismo patrón pesimista que useGestion).
import { useState, useEffect, useCallback } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

// `soloFacturas` apaga la carga del panel de márgenes.
//
// Los dos datasets se pedían SIEMPRE al montar, así que la tab de
// Facturación disparaba `/admin/margin` sin mostrarlo nunca.
//
// El motivo NO es performance: medido contra la base de producción, la
// agregación más pesada de `cost_dashboard_global` corre en ~2 ms sobre
// 1.924 filas de ai_provenance. El motivo es que `/admin/margin` devuelve
// `by_user` con los usernames de TODOS los tenants, y pedirlo desde una
// pantalla de facturas es superficie de datos regalada.
export default function useNegocio({ soloFacturas = false } = {}) {
  const { flashError } = useAdmin();

  // --- Costos ---------------------------------------------------------------
  const [costSinceDays, setCostSinceDays] = useState(30);
  const [costRevenuePerVideo, setCostRevenuePerVideo] = useState(8);
  const [costDashboard, setCostDashboard] = useState(null);
  const [costLoading, setCostLoading] = useState(false);

  const loadCostDashboard = useCallback(async () => {
    if (soloFacturas) return;
    setCostLoading(true);
    try {
      const url = `${API}/admin/margin?since_days=${costSinceDays}` +
        `&revenue_per_video_usd=${costRevenuePerVideo}`;
      setCostDashboard(await fetchJson(url));
    } catch (err) {
      flashError(`No pude cargar el panel de costos: ${err.message || err}`);
    } finally {
      setCostLoading(false);
    }
  }, [costSinceDays, costRevenuePerVideo, soloFacturas, flashError]);

  useEffect(() => { loadCostDashboard(); }, [loadCostDashboard]);

  // El "Margen por tenant" (economics, super-admin) vive en Insights; su
  // fetch está en useInsights. No se fusionó con la página de Costos a
  // propósito: usa otro denominador (`approved_videos`, por `approved_at`)
  // y otro precio (`PLANS[plan].price_per_video`, fijo) que el precio
  // editable de esa página. Ponerlos en la misma fila invitaría a restar
  // peras de manzanas.

  // --- Invoices -------------------------------------------------------------
  const [invoices, setInvoices] = useState([]);
  const [invoicesLoading, setInvoicesLoading] = useState(false);

  const loadInvoices = useCallback(async () => {
    setInvoicesLoading(true);
    try {
      const data = await fetchJson(`${API}/admin/invoices?limit=100`);
      setInvoices(data.invoices || []);
    } catch (err) {
      flashError(`No pude cargar las facturas: ${err.message || err}`);
    } finally {
      setInvoicesLoading(false);
    }
  }, [flashError]);

  useEffect(() => { loadInvoices(); }, [loadInvoices]);

  return {
    // costos
    costSinceDays,
    setCostSinceDays,
    costRevenuePerVideo,
    setCostRevenuePerVideo,
    costDashboard,
    costLoading,
    loadCostDashboard,
    // invoices
    invoices,
    invoicesLoading,
    loadInvoices,
  };
}
