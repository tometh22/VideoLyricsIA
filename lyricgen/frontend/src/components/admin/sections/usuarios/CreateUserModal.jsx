// Fast account setup with searchable workspace and campaign access pickers.
import { useEffect, useMemo, useState } from "react";

import { API, authHeaders } from "../../adminApi";

const PLANS = ["free", "100", "250", "500", "1000", "unlimited"];
const EMPTY = {
  username: "", password: "", email: "", tenant_id: "", plan_id: "100",
  role: "user", allow_overage: false, ai_authorized: false, billing_group: "",
  campaign_ids: [],
};
const inputClass = "w-full bg-surface-3/40 ring-1 ring-white/[0.08] focus:ring-brand/50 focus:outline-none rounded-button px-3 py-2.5 text-ui text-white placeholder:text-gray-500";
const labelClass = "mb-1.5 block text-label font-medium text-gray-300";

export default function CreateUserModal({ onCreate, onClose }) {
  const [form, setForm] = useState(EMPTY);
  const [options, setOptions] = useState({ workspaces: [], campaigns: [] });
  const [optionsError, setOptionsError] = useState("");
  const [workspaceQuery, setWorkspaceQuery] = useState("");
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [campaignQuery, setCampaignQuery] = useState("");
  const [createError, setCreateError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  useEffect(() => {
    let active = true;
    fetch(`${API}/admin/user-assignment-options`, { headers: authHeaders() })
      .then(async (res) => {
        if (!res.ok) throw new Error(`No pude cargar las opciones (${res.status})`);
        return res.json();
      })
      .then((data) => { if (active) setOptions(data); })
      .catch((err) => { if (active) setOptionsError(err.message || "No pude cargar workspaces y campañas"); });
    return () => { active = false; };
  }, []);

  const visibleWorkspaces = useMemo(() => {
    const q = workspaceQuery.trim().toLocaleLowerCase();
    return options.workspaces.filter((w) => !q || w.id.toLocaleLowerCase().includes(q));
  }, [options.workspaces, workspaceQuery]);
  const availableCampaigns = useMemo(() => {
    const q = campaignQuery.trim().toLocaleLowerCase();
    return options.campaigns.filter((c) => c.tenant_id !== form.tenant_id
      && (!q || `${c.name} ${c.tenant_id}`.toLocaleLowerCase().includes(q)));
  }, [options.campaigns, campaignQuery, form.tenant_id]);
  const selectedCampaigns = options.campaigns.filter((c) => form.campaign_ids.includes(c.id));

  const toggleCampaign = (id) => set({
    campaign_ids: form.campaign_ids.includes(id)
      ? form.campaign_ids.filter((value) => value !== id)
      : [...form.campaign_ids, id],
  });

  const handleSubmit = async (e) => {
    e.preventDefault();
    setCreateError("");
    setSubmitting(true);
    const err = await onCreate(form);
    setSubmitting(false);
    if (err) { setCreateError(err); return; }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-3 sm:p-6">
      <div className="glass-elevated max-h-[94vh] w-full max-w-3xl overflow-y-auto rounded-card p-5 sm:p-7">
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <p className="text-label font-semibold uppercase tracking-[0.16em] text-brand-light">Acceso del equipo</p>
            <h3 className="mt-1 text-section font-semibold text-white">Crear usuario</h3>
            <p className="mt-1 text-caption text-gray-400">Cuenta, workspace y campañas en un solo paso.</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-full px-3 py-1 text-gray-400 hover:bg-white/[0.06] hover:text-white">✕</button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5">
          <section className="rounded-card border border-white/[0.07] bg-black/10 p-4 sm:p-5">
            <div className="mb-4 flex items-center gap-2"><span className="grid h-6 w-6 place-items-center rounded-full bg-brand/20 text-label font-semibold text-brand-light">1</span><h4 className="text-ui font-semibold text-white">Datos de la cuenta</h4></div>
            <div className="grid gap-3 sm:grid-cols-2">
              <label><span className={labelClass}>Usuario</span><input autoFocus type="text" placeholder="Ej. danniivanna04" value={form.username} onChange={(e) => set({ username: e.target.value })} className={inputClass} required /></label>
              <label><span className={labelClass}>Email</span><input type="email" placeholder="nombre@empresa.com" value={form.email} onChange={(e) => set({ email: e.target.value })} className={inputClass} /></label>
              <label><span className={labelClass}>Contraseña inicial</span><input type="password" placeholder="Contraseña segura" value={form.password} onChange={(e) => set({ password: e.target.value })} className={inputClass} required /></label>
              <div className="grid grid-cols-2 gap-3">
                <label><span className={labelClass}>Plan</span><select value={form.plan_id} onChange={(e) => set({ plan_id: e.target.value })} className={inputClass}>{PLANS.map((p) => <option key={p} value={p}>Plan {p}</option>)}</select></label>
                <label><span className={labelClass}>Rol</span><select value={form.role} onChange={(e) => set({ role: e.target.value })} className={inputClass}><option value="user">Usuario</option><option value="admin">Admin</option></select></label>
              </div>
            </div>
          </section>

          <section className="rounded-card border border-white/[0.07] bg-black/10 p-4 sm:p-5">
            <div className="mb-3 flex items-center gap-2"><span className="grid h-6 w-6 place-items-center rounded-full bg-brand/20 text-label font-semibold text-brand-light">2</span><h4 className="text-ui font-semibold text-white">Workspace principal</h4></div>
            <p className="mb-3 text-caption text-gray-400">El workspace comparte el historial y los videos del equipo.</p>
            <div className="relative">
              <input
                type="text" value={workspaceQuery || form.tenant_id}
                onChange={(e) => { setWorkspaceQuery(e.target.value); set({ tenant_id: e.target.value }); setWorkspaceOpen(true); }}
                onFocus={() => setWorkspaceOpen(true)}
                onBlur={() => setTimeout(() => setWorkspaceOpen(false), 140)}
                placeholder="Buscar o escribir un workspace…" className={inputClass} role="combobox" aria-expanded={workspaceOpen} aria-autocomplete="list"
              />
              {workspaceOpen && <div className="absolute z-20 mt-1 max-h-52 w-full overflow-y-auto rounded-button border border-white/10 bg-[#171821] p-1 shadow-xl">
                {visibleWorkspaces.map((w) => <button key={w.id} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => { set({ tenant_id: w.id }); setWorkspaceQuery(w.id); setWorkspaceOpen(false); }} className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-ui hover:bg-white/[0.07] ${form.tenant_id === w.id ? "text-white" : "text-gray-300"}`}><span className="truncate">{w.id}</span><span className="ml-3 shrink-0 text-label text-gray-500">{w.users} usuarios · {w.campaigns} campañas</span></button>)}
                {workspaceQuery.trim() && !options.workspaces.some((w) => w.id.toLowerCase() === workspaceQuery.trim().toLowerCase()) && <div className="px-3 py-2 text-caption text-gray-400">Se creará el workspace <strong className="text-white">{workspaceQuery.trim()}</strong></div>}
                {!visibleWorkspaces.length && !workspaceQuery.trim() && <p className="px-3 py-2 text-caption text-gray-500">No hay workspaces existentes.</p>}
              </div>}
            </div>
            {!form.tenant_id && <p className="mt-2 text-caption text-amber-200/80">Sin elegir uno, el usuario tendrá un workspace propio.</p>}
          </section>

          <section className="rounded-card border border-white/[0.07] bg-black/10 p-4 sm:p-5">
            <div className="mb-2 flex items-center gap-2"><span className="grid h-6 w-6 place-items-center rounded-full bg-brand/20 text-label font-semibold text-brand-light">3</span><h4 className="text-ui font-semibold text-white">Campañas asociadas <span className="font-normal text-gray-500">· opcional</span></h4></div>
            <p className="mb-3 text-caption text-gray-400">Elegí campañas de otros workspaces para dar acceso de revisión de letras. Las del workspace principal ya quedan visibles.</p>
            {selectedCampaigns.length > 0 && <div className="mb-3 flex flex-wrap gap-2">{selectedCampaigns.map((c) => <button type="button" key={c.id} onClick={() => toggleCampaign(c.id)} className="inline-flex max-w-full items-center gap-2 rounded-full bg-brand/15 px-3 py-1.5 text-label text-brand-light hover:bg-brand/25"><span className="max-w-[240px] truncate">{c.name}</span><span aria-hidden="true">×</span></button>)}</div>}
            <input type="search" value={campaignQuery} onChange={(e) => setCampaignQuery(e.target.value)} placeholder="Buscar campaña o workspace…" className={`${inputClass} mb-2`} />
            <div className="max-h-48 space-y-1 overflow-y-auto pr-1">
              {availableCampaigns.map((c) => <label key={c.id} className="flex cursor-pointer items-center gap-3 rounded-button px-3 py-2.5 hover:bg-white/[0.05]">
                <input type="checkbox" checked={form.campaign_ids.includes(c.id)} onChange={() => toggleCampaign(c.id)} className="h-4 w-4 shrink-0 accent-brand" />
                <span className="min-w-0 flex-1"><span className="block truncate text-ui text-gray-200">{c.name}</span><span className="block truncate text-label text-gray-500">{c.tenant_id} · {c.expected_count || 0} canciones</span></span>
                <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gray-500">Letras</span>
              </label>)}
              {optionsError && <p className="px-3 py-2 text-caption text-red-300">{optionsError}</p>}
              {!optionsError && !availableCampaigns.length && <p className="px-3 py-3 text-caption text-gray-500">{form.tenant_id ? "No hay otras campañas activas que coincidan." : "Elegí un workspace para ver campañas disponibles."}</p>}
            </div>
          </section>

          <details className="group rounded-card border border-white/[0.07] px-4 py-3">
            <summary className="cursor-pointer list-none text-ui font-medium text-gray-300"><span className="mr-2 text-brand-light">＋</span>Opciones adicionales</summary>
            <div className="mt-4 space-y-4">
              <label><span className={labelClass}>Grupo de facturación</span><input type="text" placeholder="Opcional" value={form.billing_group} onChange={(e) => set({ billing_group: e.target.value })} className={inputClass} /><span className="mt-1 block text-label text-gray-500">Compartido entre workspaces que facturan juntos.</span></label>
              <label className="flex items-start gap-2 text-ui text-gray-300"><input type="checkbox" checked={form.ai_authorized} onChange={(e) => set({ ai_authorized: e.target.checked })} className="mt-0.5 accent-brand" /><span>Autorizar uso de IA<span className="mt-0.5 block text-label text-gray-500">Permite generar variaciones y fondos con IA.</span></span></label>
              <label className="flex items-start gap-2 text-ui text-gray-300"><input type="checkbox" checked={form.allow_overage} onChange={(e) => set({ allow_overage: e.target.checked })} className="mt-0.5 accent-amber-500" /><span>Permitir overage<span className="mt-0.5 block text-label text-gray-500">Puede pasar el límite mensual; los videos extra se facturan al cierre.</span></span></label>
            </div>
          </details>

          {createError && <p role="alert" className="rounded-button bg-red-500/10 px-3 py-2 text-ui text-red-300">{createError}</p>}
          <div className="sticky bottom-0 flex flex-col-reverse gap-2 border-t border-white/[0.07] bg-[#111218]/95 pt-4 sm:flex-row sm:justify-end">
            <button type="button" onClick={onClose} className="rounded-button px-4 py-2.5 text-ui text-gray-300 hover:bg-white/[0.05]">Cancelar</button>
            <button type="submit" disabled={submitting || !form.username || !form.password} className="rounded-button bg-brand px-5 py-2.5 text-ui font-medium text-white hover:bg-brand-dark disabled:opacity-50">{submitting ? "Creando…" : "Crear usuario"}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
