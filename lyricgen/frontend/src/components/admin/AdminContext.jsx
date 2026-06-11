// Contexto mínimo del Admin Panel v2.
//
// Solo lo genuinamente transversal:
//   - flashError / adminError: el banner de error que cualquier sección
//     dispara cuando el backend rechaza una mutación.
//   - stats / refreshStats: /admin/stats se fetchea UNA vez acá y lo leen
//     el sidebar (badges) y Operación (KPIs) — sin doble fetch.
//
// Todo lo demás (jobs, usuarios, actividad, fondos...) es estado local de
// cada sección vía su propio hook. No agrandar este contexto.
import { createContext, useContext, useState, useEffect, useCallback } from "react";

import { API, fetchJson } from "./adminApi";

const AdminContext = createContext(null);

export function AdminProvider({ children }) {
  // Banner de error de mutaciones. Auto-clear en 8 s (mismo contrato que el
  // admin viejo — ver CONTRIBUTING.md §4: una mutación rechazada NUNCA puede
  // parecer exitosa).
  const [adminError, setAdminError] = useState(null);
  const flashError = useCallback((msg) => {
    setAdminError(msg);
    setTimeout(() => setAdminError((cur) => (cur === msg ? null : cur)), 8000);
  }, []);

  // Período global de análisis (7/30/90 días) — consolidación 2026-06-11:
  // Rendimiento e Insights comparten UNA ventana en vez de selectores
  // locales desincronizados (uno de los "misma pregunta, números
  // distintos" del diagnóstico).
  const [periodDays, setPeriodDays] = useState(30);

  // Stats globales (/admin/stats) — para KPIs y badges.
  const [stats, setStats] = useState(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const refreshStats = useCallback(async () => {
    try {
      setStats(await fetchJson(`${API}/admin/stats`));
    } catch {
      // Best-effort: las secciones muestran "—" si stats es null.
    } finally {
      setStatsLoading(false);
    }
  }, []);
  useEffect(() => { refreshStats(); }, [refreshStats]);

  return (
    <AdminContext.Provider value={{ adminError, setAdminError, flashError, stats, statsLoading, refreshStats, periodDays, setPeriodDays }}>
      {children}
    </AdminContext.Provider>
  );
}

export function useAdmin() {
  const ctx = useContext(AdminContext);
  if (!ctx) throw new Error("useAdmin() must be used inside <AdminProvider>");
  return ctx;
}
