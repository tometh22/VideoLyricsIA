// Datos de la sección Insights (panel CEO, solo super-admin).
//
// Navegación jerárquica de 3 niveles — app → tenant → usuario — con la
// misma gramática de datos en cada nivel: overview (uso/calidad/costo),
// adoption (features elegidas) y wizard (funnel de ui_events). El nivel
// usuario suma el detalle de /admin/activity/{id} (timeline + sesiones +
// logins + fondos).
//
// Cache por clave nivel:tenant:usuario:días — volver hacia arriba en el
// breadcrumb es instantáneo; cambiar el período refetchea.
import { useCallback, useEffect, useRef, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

function keyOf(nav, days) {
  return `${nav.level}:${nav.tenantId || ""}:${nav.userId || ""}:${days}`;
}

function scopeParams(nav, days) {
  const p = new URLSearchParams({ days: String(days) });
  if (nav.tenantId) p.set("tenant_id", nav.tenantId);
  if (nav.userId) p.set("user_id", String(nav.userId));
  return p.toString();
}

export default function useInsights() {
  const { flashError, periodDays, setPeriodDays } = useAdmin();
  const [nav, setNav] = useState({ level: "app", tenantId: null, userId: null });
  // Período global del panel (AdminContext) — compartido con Rendimiento.
  const days = periodDays;
  const setDays = setPeriodDays;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const cacheRef = useRef(new Map());

  const load = useCallback(async (targetNav, targetDays, { force = false } = {}) => {
    const key = keyOf(targetNav, targetDays);
    if (!force && cacheRef.current.has(key)) {
      setData(cacheRef.current.get(key));
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const qs = scopeParams(targetNav, targetDays);
      // overview no acepta user_id — el nivel usuario lo cubre activity/{id}.
      const overviewQs = scopeParams({ ...targetNav, userId: null }, targetDays);
      const [overview, adoption, wizard, detail, economics, userEvents] = await Promise.all([
        targetNav.level === "user"
          ? Promise.resolve(null)
          : fetchJson(`${API}/admin/insights/overview?${overviewQs}`),
        fetchJson(`${API}/admin/insights/adoption?${qs}`),
        fetchJson(`${API}/admin/insights/wizard?${qs}`),
        targetNav.level === "user"
          ? fetchJson(`${API}/admin/activity/${targetNav.userId}?since_days=${targetDays}`)
          : Promise.resolve(null),
        // Margen por tenant (consolidación 2026-06-11: vivía escondido en
        // Negocio→Costos; acá está con el resto de la vista por-tenant).
        // economics acepta days 7..90 — clamp inferior.
        targetNav.level === "app"
          ? fetchJson(`${API}/admin/metrics/economics?days=${Math.max(targetDays, 7)}`).catch(() => null)
          : Promise.resolve(null),
        // Sesiones de wizard del usuario (profundidad 2026-06-11).
        targetNav.level === "user"
          ? fetchJson(`${API}/admin/insights/user/${targetNav.userId}/events?days=${targetDays}`).catch(() => null)
          : Promise.resolve(null),
      ]);
      const bundle = { overview, adoption, wizard, detail, economics, userEvents };
      cacheRef.current.set(key, bundle);
      setData(bundle);
    } catch (e) {
      flashError(`No se pudo cargar Insights: ${e.message}`);
      setData(null);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load(nav, days);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav, days]);

  const drillTenant = (tenantId) => setNav({ level: "tenant", tenantId, userId: null });
  const drillUser = (userId, tenantId = nav.tenantId) =>
    setNav({ level: "user", tenantId, userId });
  const upToApp = () => setNav({ level: "app", tenantId: null, userId: null });
  const upToTenant = () =>
    nav.tenantId
      ? setNav({ level: "tenant", tenantId: nav.tenantId, userId: null })
      : upToApp();
  const refresh = () => load(nav, days, { force: true });

  return {
    nav,
    days,
    setDays,
    loading,
    overview: data?.overview ?? null,
    adoption: data?.adoption ?? null,
    wizard: data?.wizard ?? null,
    detail: data?.detail ?? null,
    economics: data?.economics ?? null,
    userEvents: data?.userEvents ?? null,
    drillTenant,
    drillUser,
    upToApp,
    upToTenant,
    refresh,
  };
}
