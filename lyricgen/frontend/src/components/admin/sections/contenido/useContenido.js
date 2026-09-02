// Hook dueño de TODA la data de la sección Contenido.
//
// Dos slices independientes, una por sub-vista del sidebar:
//   - Fondos: biblioteca de backgrounds (IA + subidos), globales o exclusivos
//     de un tenant; upload multipart, borrado y filtro server-side.
//   - Compliance: estado de los checks de cumplimiento UMG (read-only).
//
// El montaje ES el trigger: cada vista monta sólo cuando su sub-tab está
// activo (ContenidoSection es un switch), así que cargamos al montar y —en
// Fondos— cada vez que cambia el filtro de la biblioteca (es server-side).
//
// Los GET/DELETE pasan por fetchJson (un solo lugar con auth + res.ok). El
// upload es la excepción: usa `fetch` crudo con FormData y conserva su propio
// `if (!res.ok)` (lo exige el gate check:fetch). Errores → flashError.
import { useCallback, useEffect, useState } from "react";

import { API, fetchJson, authHeaders } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

export default function useContenido() {
  const { flashError } = useAdmin();

  // --- Fondos ---------------------------------------------------------------
  const [backgrounds, setBackgrounds] = useState([]);
  const [bgUploading, setBgUploading] = useState(false);
  const [bgName, setBgName] = useState("");
  const [bgTags, setBgTags] = useState("");
  // "" = global / visible para todos. Cualquier otro valor es un tenant_id al
  // que el asset queda atado. La exclusividad de UMG cuelga de acá.
  const [bgOwnerTenant, setBgOwnerTenant] = useState("");
  // Tenants con al menos un usuario; viene de /admin/background-tenants para
  // no hardcodear el nombre de UMG.
  const [bgTenants, setBgTenants] = useState([]);
  // Filtro de la biblioteca: "" = todos, "__global__" = sin dueño, cualquier
  // otro = match exacto de tenant. Server-side via el mismo endpoint.
  const [bgListFilter, setBgListFilter] = useState("");

  const loadBackgrounds = useCallback(async () => {
    try {
      const q = bgListFilter ? `?owner_tenant_id=${encodeURIComponent(bgListFilter)}` : "";
      setBackgrounds(await fetchJson(`${API}/admin/backgrounds${q}`));
    } catch (err) {
      flashError(`No pude cargar los fondos: ${err.message || err}`);
    }
    try {
      const data = await fetchJson(`${API}/admin/background-tenants`);
      setBgTenants(Array.isArray(data?.tenants) ? data.tenants : []);
    } catch (err) {
      flashError(`No pude cargar los tenants de fondos: ${err.message || err}`);
    }
  }, [bgListFilter, flashError]);

  const handleUploadBg = useCallback(async (file) => {
    if (!file || !bgName.trim()) return;
    setBgUploading(true);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("name", bgName.trim());
    formData.append("tags", bgTags.trim());
    if (bgOwnerTenant) formData.append("owner_tenant_id", bgOwnerTenant);
    try {
      // Upload multipart: fetch crudo (fetchJson no maneja FormData ni el
      // Content-Type que el browser setea solo con el boundary).
      const res = await fetch(`${API}/admin/backgrounds`, { method: "POST", headers: authHeaders(), body: formData });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      setBgName("");
      setBgTags("");
      // Reseteamos también el selector de tenant para que el próximo asset no
      // herede silenciosamente la asignación anterior (ej: un upload Global
      // terminando en la biblioteca de UMG porque el dropdown quedó pegado).
      setBgOwnerTenant("");
      loadBackgrounds();
    } catch (err) {
      flashError(`Subida de background falló: ${err.message || err}`);
    }
    setBgUploading(false);
  }, [bgName, bgTags, bgOwnerTenant, loadBackgrounds, flashError]);

  const handleDeleteBg = useCallback(async (id) => {
    if (!window.confirm("¿Borrar este fondo?")) return;
    try {
      await fetchJson(`${API}/admin/backgrounds/${id}`, { method: "DELETE" });
      loadBackgrounds();
    } catch (err) {
      flashError(`Borrar background falló: ${err.message || err}`);
    }
  }, [loadBackgrounds, flashError]);

  // Recarga al montar y cada vez que cambia el filtro de la biblioteca.
  useEffect(() => { loadBackgrounds(); }, [loadBackgrounds]);

  // --- Compliance -----------------------------------------------------------
  const [compliance, setCompliance] = useState(null);

  const loadCompliance = useCallback(async () => {
    try {
      setCompliance(await fetchJson(`${API}/compliance/status`));
    } catch (err) {
      flashError(`No pude cargar el estado de compliance: ${err.message || err}`);
    }
  }, [flashError]);

  useEffect(() => { loadCompliance(); }, [loadCompliance]);

  return {
    // fondos
    backgrounds,
    bgName,
    setBgName,
    bgTags,
    setBgTags,
    bgOwnerTenant,
    setBgOwnerTenant,
    bgTenants,
    bgListFilter,
    setBgListFilter,
    bgUploading,
    handleUploadBg,
    handleDeleteBg,
    // compliance
    compliance,
  };
}
