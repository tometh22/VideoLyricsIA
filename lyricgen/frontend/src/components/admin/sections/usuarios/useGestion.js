// Hook de datos de la vista "Gestión de usuarios" (antes el tab `users` del
// monolito).
//
// Dueño de: la lista de usuarios (/admin/users), el estado de búsqueda y las
// mutaciones (crear, cambiar plan, toggle AI / overage / activo). Todas las
// mutaciones son pesimistas: ante fallo flashError + reload, para que un
// rechazo del backend NUNCA parezca exitoso (CONTRIBUTING.md §4).
import { useState, useEffect, useCallback } from "react";

import { API, authHeaders } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

export default function useGestion() {
  const { flashError } = useAdmin();

  const [users, setUsers] = useState([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [search, setSearch] = useState("");

  const loadUsers = useCallback(async () => {
    try {
      const q = search ? `&search=${encodeURIComponent(search)}` : "";
      const res = await fetch(`${API}/admin/users?limit=100${q}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setUsers(data.users || []);
      setUsersTotal(data.total || 0);
    } catch (err) {
      flashError(`No pude cargar los usuarios: ${err.message || err}`);
    }
  }, [search, flashError]);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  // Crea un usuario. Devuelve un string de error (para mostrar inline en el
  // modal) o null si salió bien. Recarga la lista en éxito.
  const createUser = useCallback(async (payload) => {
    try {
      const res = await fetch(`${API}/admin/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        return data.detail || `Error ${res.status}`;
      }
      loadUsers();
      return null;
    } catch (err) {
      return err.message || String(err);
    }
  }, [loadUsers]);

  // Helper para los 3 PATCH sobre /admin/users/{id} que comparten manejo de
  // error: chequear res.ok, extraer detail, flashError + reload (revierte
  // cualquier estado local desincronizado).
  const patchUser = useCallback(async (userId, body, errorLabel) => {
    try {
      const res = await fetch(`${API}/admin/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      loadUsers();
    } catch (err) {
      flashError(`${errorLabel}: ${err.message || err}`);
      loadUsers();
    }
  }, [loadUsers, flashError]);

  const changePlan = useCallback((userId, planId) =>
    patchUser(userId, { plan_id: planId }, "Cambiar plan falló"), [patchUser]);

  const toggleOverage = useCallback((userId, current) =>
    patchUser(userId, { allow_overage: !current }, "Toggle overage falló"), [patchUser]);

  const toggleActive = useCallback((userId, current) =>
    patchUser(userId, { is_active: !current }, "Toggle activo falló"), [patchUser]);

  // AI auth usa endpoints dedicados (POST), no un PATCH del recurso.
  const toggleAI = useCallback(async (userId, currentlyAuthorized) => {
    const endpoint = currentlyAuthorized ? "revoke-ai" : "authorize-ai";
    try {
      const res = await fetch(`${API}/admin/users/${userId}/${endpoint}`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      loadUsers();
    } catch (err) {
      flashError(`Toggle AI auth falló: ${err.message || err}`);
      loadUsers();
    }
  }, [loadUsers, flashError]);

  return {
    users,
    usersTotal,
    search,
    setSearch,
    loadUsers,
    createUser,
    changePlan,
    toggleAI,
    toggleOverage,
    toggleActive,
  };
}
