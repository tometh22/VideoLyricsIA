import { useEffect, useRef, useState } from "react";
import { API, fetchJson } from "../../adminApi";

const LIMIT = 30;
export default function CampaignAccessModal({ user, onClose }) {
  const dialog = useRef(null);
  const [search, setSearch] = useState("");
  const [grantedOnly, setGrantedOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const base = `${API}/admin/users/${user.id}/campaign-access`;

  useEffect(() => { dialog.current.showModal(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const query = new URLSearchParams({ search, granted_only: String(grantedOnly), offset: String(offset), limit: String(LIMIT) });
        const result = await fetchJson(`${base}?${query}`, { signal: controller.signal });
        if (!controller.signal.aborted) {
          if (offset > 0 && offset >= result.total) setOffset(Math.max(0, Math.floor((result.total - 1) / LIMIT) * LIMIT));
          else setData(result);
        }
      } catch (e) {
        if (!controller.signal.aborted) { setError(e.message); setData(null); }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 200);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [base, search, grantedOnly, offset, refresh]);

  async function changeAccess(campaign, enabled) {
    setBusy(true); setError(""); setNotice("");
    try {
      await fetchJson(`${base}/${encodeURIComponent(campaign.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, expected_granted: campaign.granted }),
      });
      setNotice(`${enabled ? "Acceso habilitado" : "Permiso adicional quitado"}: ${campaign.name}`);
    } catch (e) {
      setError(e.message);
    } finally {
      setConfirm(null); setBusy(false); setRefresh((r) => r + 1);
    }
  }
  const changeFilter = (fn) => { fn(); setOffset(0); setConfirm(null); setError(""); };
  return (
    <dialog ref={dialog} aria-labelledby="campaign-access-title"
      onCancel={(e) => { e.preventDefault(); if (!busy) onClose(); }}
      style={{ width: "min(48rem, calc(100% - 2rem))" }}
      className="m-auto max-h-[85vh] rounded-card bg-surface-2 p-0 text-white shadow-depth-lg backdrop:bg-black/70">
      <div className="p-5 space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div><h2 id="campaign-access-title" className="text-lg font-semibold">Acceso a campañas</h2>
            <p className="text-caption text-gray-300 break-all">{user.email || user.username}</p></div>
          <button type="button" disabled={busy} onClick={onClose} className="px-3 py-2 rounded-button bg-white/10 disabled:opacity-40">Cerrar</button>
        </div>
        <p className="text-caption text-gray-400">Da permiso para ver y revisar letras. La asignación de responsables se hace en Mi equipo. Los accesos por equipo o por rol administrador se conservan.</p>
        <label className="block text-caption">Buscar campaña
          <input type="search" value={search} disabled={busy} onChange={(e) => changeFilter(() => setSearch(e.target.value))}
            placeholder="Nombre de campaña o equipo…" className="mt-1 w-full rounded-button bg-surface-3 p-3 text-white" />
        </label>
        <label className="flex gap-2 text-caption"><input type="checkbox" checked={grantedOnly} disabled={busy}
          onChange={(e) => changeFilter(() => setGrantedOnly(e.target.checked))} />Solo permisos adicionales otorgados</label>
        {data && !data.user_active && <p className="text-amber-200">La cuenta está desactivada. Podés quitar permisos, pero no otorgar nuevos.</p>}
        {data && !data.campaigns_enabled && <p className="text-amber-200">Las campañas no están habilitadas para el equipo de este usuario. No se pueden otorgar nuevos accesos; los permisos existentes pueden revocarse.</p>}
        {error && <div role="alert" className="text-red-300">{error} <button type="button" disabled={busy || loading} onClick={() => { setError(""); setRefresh((r) => r + 1); }} className="underline">Actualizar lista</button></div>}
        {notice && <p role="status" className="text-green-300">{notice}</p>}
        {loading ? <p role="status">Cargando campañas…</p> : data && <>
          {!data.items.length && <p>No hay campañas que coincidan con estos filtros.</p>}
          <ul className="space-y-3">
            {data.items.map((campaign) => <li key={campaign.id} className="rounded-button border border-white/10 p-3 space-y-2">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1"><h3 className="font-medium break-words">{campaign.name}</h3>
                  <p className="text-caption text-gray-400 break-all">Equipo: {campaign.tenant_id}</p>
                  <p className="text-caption text-gray-300">{campaign.inherited === "admin" ? "Acceso por rol administrador" : campaign.inherited === "team" ? "Acceso por pertenecer al equipo" : campaign.granted ? "Permiso de revisión otorgado" : "Sin acceso"}</p>
                  {campaign.status !== "active" && <p className="text-caption text-amber-200">Campaña cerrada</p>}
                </div>
                {campaign.granted ? <button type="button" disabled={busy} onClick={() => { setConfirm(campaign.id); setNotice(""); }}
                  className="rounded-button px-3 py-2 bg-white/10 disabled:opacity-40">{campaign.inherited ? "Quitar permiso adicional" : "Quitar acceso"}</button>
                  : !campaign.inherited && <button type="button" disabled={busy || !data.user_active || !data.campaigns_enabled || campaign.status !== "active" || campaign.kind !== "lyric_video"}
                    onClick={() => changeAccess(campaign, true)} className="rounded-button px-3 py-2 bg-brand disabled:opacity-40">Dar acceso</button>}
              </div>
              {confirm === campaign.id && <div className="border-t border-white/10 pt-3 space-y-2">
                <p className="text-caption">{campaign.inherited ? "Se quitará el permiso adicional. Conserva el acceso por su rol o equipo." : "Al confirmar, este usuario dejará de tener acceso a esta campaña mediante este permiso."}</p>
                <div className="flex gap-3"><button type="button" disabled={busy} onClick={() => changeAccess(campaign, false)} className="px-3 py-2 rounded-button bg-red-900 disabled:opacity-40">Confirmar revocación</button>
                  <button type="button" disabled={busy} onClick={() => setConfirm(null)}>Cancelar</button></div>
              </div>}
            </li>)}
          </ul>
          <div className="flex items-center justify-between gap-3 text-caption">
            <button type="button" disabled={busy || offset === 0} onClick={() => { setOffset(Math.max(0, offset - LIMIT)); setConfirm(null); }} className="disabled:opacity-40">Anterior</button>
            <span>{data.total ? `${offset + 1}–${Math.min(offset + LIMIT, data.total)} de ${data.total}` : "0 campañas"}</span>
            <button type="button" disabled={busy || offset + LIMIT >= data.total} onClick={() => { setOffset(offset + LIMIT); setConfirm(null); }} className="disabled:opacity-40">Siguiente</button>
          </div>
        </>}
      </div>
    </dialog>
  );
}
