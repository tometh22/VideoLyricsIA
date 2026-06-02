// Hook de datos de la vista "Actividad" (antes el tab `activity` del monolito).
//
// Dueño de: la respuesta de /admin/activity (una fila por usuario), el cache
// de detalle por usuario (drill-down on-demand), los filtros de la tabla
// (período / tenant / ocultar inactivos) y la lista visible derivada.
import { useState, useEffect, useCallback } from "react";

import { API, authHeaders, hasActivity } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

export default function useActividad() {
  const { flashError } = useAdmin();

  // Ventana de tiempo (días). 30 por default — recarga al cambiar.
  const [activitySinceDays, setActivitySinceDays] = useState(30);
  const [activity, setActivity] = useState(null);
  const [activityLoading, setActivityLoading] = useState(false);

  // user_id expandido (null = ninguno) + cache de detalle por user_id para no
  // re-pedir al colapsar/expandir.
  const [activityExpanded, setActivityExpanded] = useState(null);
  const [activityDetail, setActivityDetail] = useState({});

  // Filtros de la tabla. Ocultar inactivos default ON (las cuentas de test
  // ahogan la tabla); filtro por tenant para mirar un solo workspace (ej. UMG).
  const [activityHideInactive, setActivityHideInactive] = useState(true);
  const [activityTenantFilter, setActivityTenantFilter] = useState("");

  const loadActivity = useCallback(async () => {
    setActivityLoading(true);
    // Al recargar (cambio de período) el detalle cacheado queda stale respecto
    // de la nueva ventana → lo descartamos.
    setActivityDetail({});
    setActivityExpanded(null);
    try {
      // fetch crudo (no fetchJson) porque el 403 NO es un error de red: es un
      // admin sin permiso de super admin (SUPER_ADMIN_USERS) → mostramos el
      // panel de acceso restringido en vez de tirar el banner de error.
      const res = await fetch(`${API}/admin/activity?since_days=${activitySinceDays}`, { headers: authHeaders() });
      if (res.status === 403) {
        setActivity({ restricted: true, users: [], since_days: activitySinceDays });
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setActivity(await res.json());
    } catch (err) {
      flashError(`No pude cargar la actividad: ${err.message || err}`);
    } finally {
      setActivityLoading(false);
    }
  }, [activitySinceDays, flashError]);

  // Recarga al montar y cada vez que cambia el período.
  useEffect(() => { loadActivity(); }, [loadActivity]);

  const loadActivityDetail = useCallback(async (uid) => {
    try {
      const res = await fetch(`${API}/admin/activity/${uid}?since_days=${activitySinceDays}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setActivityDetail((d) => ({ ...d, [uid]: data }));
    } catch (err) {
      flashError(`No pude cargar el detalle del usuario: ${err.message || err}`);
    }
  }, [activitySinceDays, flashError]);

  const toggleRow = useCallback((uid) => {
    setActivityExpanded((cur) => {
      if (cur === uid) return null;
      return uid;
    });
    // Pedimos el detalle solo si no está cacheado. (Se evalúa con el cache
    // actual; expandir una fila ya vista no re-pide.)
    setActivityDetail((d) => {
      if (!d[uid] && activityExpanded !== uid) loadActivityDetail(uid);
      return d;
    });
  }, [activityExpanded, loadActivityDetail]);

  // Lista visible según filtros. Las KPI cards y la tabla usan ESTA misma
  // lista para que los números sean consistentes con lo que se ve.
  const visibleUsers = (activity?.users || []).filter((u) => {
    if (activityTenantFilter && u.tenant_id !== activityTenantFilter) return false;
    if (activityHideInactive && !hasActivity(u)) return false;
    return true;
  });
  const hiddenCount = (activity?.users?.length || 0) - visibleUsers.length;

  return {
    activity,
    activityLoading,
    activitySinceDays,
    setActivitySinceDays,
    activityExpanded,
    activityDetail,
    toggleRow,
    activityHideInactive,
    setActivityHideInactive,
    activityTenantFilter,
    setActivityTenantFilter,
    visibleUsers,
    hiddenCount,
    loadActivity,
  };
}
