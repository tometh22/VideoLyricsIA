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

export default function useNegocio() {
  const { flashError } = useAdmin();

  // --- Costos ---------------------------------------------------------------
  const [costSinceDays, setCostSinceDays] = useState(30);
  const [costRevenuePerVideo, setCostRevenuePerVideo] = useState(8);
  const [costDashboard, setCostDashboard] = useState(null);
  const [costLoading, setCostLoading] = useState(false);

  const loadCostDashboard = useCallback(async () => {
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
  }, [costSinceDays, costRevenuePerVideo, flashError]);

  useEffect(() => { loadCostDashboard(); }, [loadCostDashboard]);

  // --- Margen por tenant (Fase 1, gate super-admin) -------------------------
  // 403 = el usuario no está en SUPER_ADMIN_USERS → ocultar el bloque en
  // silencio (sin flashError: no es un error, es un permiso).
  const [economics, setEconomics] = useState(null);
  const [economicsForbidden, setEconomicsForbidden] = useState(false);
  const [economicsLoading, setEconomicsLoading] = useState(false);
  useEffect(() => {
    (async () => {
      setEconomicsLoading(true);
      try {
        setEconomics(await fetchJson(`${API}/admin/metrics/economics?days=28`));
      } catch (err) {
        if (String(err?.message || "").toLowerCase().includes("super admin")) {
          setEconomicsForbidden(true);
        }
      } finally {
        setEconomicsLoading(false);
      }
    })();
  }, []);

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
    economics,
    economicsForbidden,
    economicsLoading,
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
