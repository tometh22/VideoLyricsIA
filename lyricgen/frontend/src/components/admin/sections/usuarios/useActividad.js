// Hook de datos de la vista "Actividad" (antes el tab `activity` del monolito).
//
// Dueño de: la respuesta de /admin/activity (una fila por usuario), el cache
// de detalle por usuario (drill-down on-demand), los filtros de la tabla
// (período / tenant / segmento) y las listas derivadas (usuarios visibles,
// errores recientes agregados).
import { useState, useEffect, useCallback, useMemo } from "react";

import { API, authHeaders, hasActivity, reworkTotal } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

// Segmentos de la tabla: vistas de un click en vez de toggles + dropdowns.
//   active  → usuarios con actividad real (default; esconde cuentas de test)
//   errors  → solo los que tuvieron errores (¿a quién le falló algo?)
//   online  → conectados ahora (solo con telemetría)
//   all     → todos, incluso sin actividad
export const SEGMENTS = [
  { id: "active", label: "Con actividad" },
  { id: "errors", label: "Con errores" },
  { id: "online", label: "En línea" },
  { id: "all", label: "Todos" },
];

function matchesSegment(u, segment) {
  if (segment === "all") return true;
  if (segment === "errors") return (u.errors?.count || 0) > 0;
  if (segment === "online") return Boolean(u.sessions?.online);
  return hasActivity(u); // "active"
}

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

  // Filtros: segmento (vistas de un click) + tenant.
  const [segment, setSegment] = useState("active");
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

  // Usuarios que pasan el filtro de tenant (base para segmentos y errores).
  const tenantUsers = useMemo(
    () => (activity?.users || []).filter(
      (u) => !activityTenantFilter || u.tenant_id === activityTenantFilter,
    ),
    [activity, activityTenantFilter],
  );

  // Conteo por segmento — para mostrar "(N)" en cada chip.
  const segmentCounts = useMemo(() => {
    const counts = {};
    for (const s of SEGMENTS) {
      counts[s.id] = tenantUsers.filter((u) => matchesSegment(u, s.id)).length;
    }
    return counts;
  }, [tenantUsers]);

  // Lista visible = tenant + segmento. Las KPI cards y la tabla usan ESTA
  // misma lista para que los números sean consistentes con lo que se ve.
  const visibleUsers = useMemo(
    () => tenantUsers.filter((u) => matchesSegment(u, segment)),
    [tenantUsers, segment],
  );

  // Errores recientes agregados de TODOS los usuarios del tenant (no del
  // segmento — el panel "¿qué falló?" tiene que mostrar todo lo que falló
  // aunque estés mirando otro segmento). Ordenados del más nuevo al más viejo.
  const recentErrors = useMemo(() => {
    const all = [];
    for (const u of tenantUsers) {
      for (const e of (u.errors?.recent || [])) {
        all.push({ ...e, username: u.username, user_id: u.user_id, tenant_id: u.tenant_id });
      }
    }
    all.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    return all;
  }, [tenantUsers]);

  // Total de retrabajos del tenant (para el resumen del panel de problemas).
  const totalRework = useMemo(
    () => tenantUsers.reduce((s, u) => s + reworkTotal(u.rework), 0),
    [tenantUsers],
  );

  return {
    activity,
    activityLoading,
    activitySinceDays,
    setActivitySinceDays,
    activityExpanded,
    activityDetail,
    toggleRow,
    segment,
    setSegment,
    segmentCounts,
    activityTenantFilter,
    setActivityTenantFilter,
    visibleUsers,
    recentErrors,
    totalRework,
    loadActivity,
  };
}
