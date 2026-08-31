import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { editorSessionHeaders } from "../lib/editorSession";

const API = import.meta.env.VITE_API_URL || "";
const PHASES = [
  ["", "Todos"], ["waiting_upload", "Esperando carga"],
  ["uploading", "Subiendo"], ["waiting_processing", "En espera"],
  ["transcribing", "Transcribiendo"], ["lyrics_ready", "Listo para corregir"],
  ["rendering", "Renderizando"], ["final_review", "Revisión final"],
  ["done", "Terminado"], ["failed", "Fallido"],
];

function authHeaders(headers = {}) {
  const token = localStorage.getItem("genly_token");
  return token ? { ...headers, Authorization: `Bearer ${token}` } : headers;
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(
    typeof body.detail === "string" ? body.detail : body.detail?.code || `Error ${response.status}`,
  );
  return body;
}

function Counter({ label, value, active, onClick }) {
  return (
    <button onClick={onClick} className={`rounded-xl p-4 text-left ring-1 transition ${active ? "bg-brand/15 ring-brand/40" : "bg-surface-2/50 ring-white/[0.06] hover:ring-white/15"}`}>
      <div className="text-2xl font-bold text-white">{value || 0}</div>
      <div className="mt-1 text-xs text-ink-tertiary">{label}</div>
    </button>
  );
}

function CampaignList() {
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [name, setName] = useState("");
  const [expected, setExpected] = useState(600);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => api("/batch/campaigns").then((d) => setItems(d.items || [])).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const create = async (event) => {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true); setError("");
    try {
      const campaign = await api("/batch/campaigns", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(), expected_count: Number(expected) || 0,
          default_render_params: { background_mode: "ai", delivery_profile: "youtube" },
        }),
      });
      navigate(`/campaigns/${campaign.id}`);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[.18em] text-brand">Producción masiva</p>
        <h1 className="mt-2 text-3xl font-bold text-white">Campañas</h1>
        <p className="mt-2 text-sm text-ink-secondary">Subí todos los audios, corregí letras y generá sin bloquear los videos normales.</p>
      </div>
      <form onSubmit={create} className="grid gap-3 rounded-2xl bg-surface-2/50 p-5 ring-1 ring-white/[0.06] md:grid-cols-[1fr_150px_auto]">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nombre de la campaña" maxLength={160} className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none focus:ring-brand/50" />
        <input value={expected} onChange={(e) => setExpected(e.target.value)} type="number" min="1" max="1000" aria-label="Cantidad esperada" className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none focus:ring-brand/50" />
        <button disabled={busy || !name.trim()} className="rounded-xl bg-brand px-5 py-3 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Creando…" : "Nueva campaña"}</button>
      </form>
      {error && <div className="rounded-xl bg-red-500/10 p-4 text-sm text-red-200 ring-1 ring-red-500/25">{error}</div>}
      <div className="grid gap-3">
        {items.map((campaign) => (
          <button key={campaign.id} onClick={() => navigate(`/campaigns/${campaign.id}`)} className="flex items-center gap-4 rounded-2xl bg-surface-2/40 p-5 text-left ring-1 ring-white/[0.06] hover:ring-brand/30">
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold text-white">{campaign.name}</div>
              <div className="mt-1 text-xs text-ink-tertiary">{campaign.registered_count}/{campaign.expected_count || "—"} registradas · {campaign.counters?.done || 0} terminadas</div>
            </div>
            <span className="rounded-full bg-white/[0.06] px-3 py-1 text-xs text-ink-secondary">{campaign.status}</span>
          </button>
        ))}
        {!items.length && !error && <div className="rounded-2xl border border-dashed border-white/10 p-10 text-center text-sm text-ink-tertiary">Todavía no hay campañas.</div>}
      </div>
    </div>
  );
}

function CampaignDetail({ id }) {
  const navigate = useNavigate();
  const [campaign, setCampaign] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState("");
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [pair, setPair] = useState(null);
  const [error, setError] = useState("");
  const [presetText, setPresetText] = useState("");

  const load = useCallback(async () => {
    try {
      const [head, rows] = await Promise.all([
        api(`/batch/campaigns/${id}`),
        api(`/batch/campaigns/${id}/items?page=${page}&limit=50${phase ? `&phase=${phase}` : ""}`),
      ]);
      setCampaign(head); setItems(rows.items || []); setPages(rows.pages || 1);
      setPresetText((current) => current || JSON.stringify(head.default_render_params || {}, null, 2));
      setError("");
    } catch (e) { setError(e.message); }
  }, [id, page, phase]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!campaign || ["completed", "cancelled"].includes(campaign.status)) return undefined;
    const timer = window.setInterval(load, 10_000);
    return () => window.clearInterval(timer);
  }, [campaign?.status, load]);

  const patch = async (value) => {
    try {
      await api(`/batch/campaigns/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
      await load();
    } catch (e) { setError(e.message); }
  };
  const takeNext = async () => {
    try {
      const data = await api(`/batch/campaigns/${id}/next`, { method: "POST", headers: editorSessionHeaders() });
      if (data.job_id) navigate(`/review/${data.job_id}`);
      else setError("Todavía no hay letras listas para corregir.");
    } catch (e) { setError(e.message); }
  };
  const editMetadata = async (item) => {
    const title = window.prompt("Título", item.title || ""); if (title == null) return;
    const artist = window.prompt("Artista", item.artist || ""); if (artist == null) return;
    const technicalCode = window.prompt("ARF / ARUM", item.technical_code || ""); if (technicalCode == null) return;
    try {
      await api(`/batch/campaigns/${id}/items/${item.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, artist, technical_code: technicalCode }) });
      await load();
    } catch (e) { setError(e.message); }
  };
  const retry = async (item) => {
    try {
      await api(`/batch/campaigns/${id}/items/${item.id}/retry`, { method: "POST" });
      await load();
    } catch (e) { setError(e.message); }
  };

  const labels = useMemo(() => Object.fromEntries(PHASES), []);
  if (!campaign) return <div className="p-8 text-sm text-ink-secondary">{error || "Cargando campaña…"}</div>;
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <button onClick={() => navigate("/campaigns")} className="text-sm text-ink-secondary hover:text-white">← Campañas</button>
      <div className="flex flex-col gap-4 md:flex-row md:items-start">
        <div className="min-w-0 flex-1"><h1 className="truncate text-3xl font-bold text-white">{campaign.name}</h1><p className="mt-2 text-sm text-ink-secondary">{campaign.registered_count}/{campaign.expected_count || "—"} audios registrados · estado {campaign.status}</p></div>
        <div className="flex flex-wrap gap-2">
          <button onClick={takeNext} disabled={!campaign.counters?.lyrics_ready} className="rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white disabled:opacity-40">Tomar siguiente</button>
          {campaign.status === "active" ? <button onClick={() => patch({ status: "paused" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Pausar</button> : campaign.status === "paused" ? <button onClick={() => patch({ status: "active" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Reanudar</button> : null}
          {!['completed', 'cancelled'].includes(campaign.status) && <button onClick={() => window.confirm("¿Cancelar esta campaña?") && patch({ status: "cancelled" })} className="rounded-xl bg-red-500/10 px-4 py-2.5 text-sm text-red-200">Cancelar</button>}
        </div>
      </div>
      {error && <div className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25">{error}</div>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {PHASES.slice(1).map(([key, label]) => <Counter key={key} label={label} value={campaign.counters?.[key]} active={phase === key} onClick={() => { setPhase(phase === key ? "" : key); setPage(1); }} />)}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]">
          <h2 className="font-semibold text-white">Cargador local</h2>
          <p className="mt-2 text-sm text-ink-secondary">Generá un código temporal. El cargador nunca recibe el token principal de tu cuenta.</p>
          <button onClick={async () => { try { setPair(await api(`/batch/campaigns/${id}/upload-session`, { method: "POST" })); } catch (e) { setError(e.message); } }} className="mt-4 rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Generar código</button>
          {pair && <div className="mt-4 rounded-xl bg-black/25 p-4"><div className="font-mono text-2xl font-bold tracking-[.2em] text-brand-light">{pair.pairing_code}</div><code className="mt-3 block whitespace-pre-wrap break-all text-xs text-ink-secondary">python3 scripts/campaign_uploader.py --api "{API || "https://TU-API"}" --campaign {id} --code {pair.pairing_code} --folder "/ruta/a/audios"</code></div>}
        </section>
        <section className="rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]">
          <h2 className="font-semibold text-white">Preset compartido</h2>
          <p className="mt-2 text-sm text-ink-secondary">JSON con fondo, tipografía, movimiento, formato y entrega. Cada canción puede tener overrides.</p>
          <textarea value={presetText} onChange={(e) => setPresetText(e.target.value)} rows={6} className="mt-3 w-full rounded-xl bg-black/25 p-3 font-mono text-xs text-white ring-1 ring-white/10 outline-none focus:ring-brand/40" />
          <button onClick={() => { try { patch({ default_render_params: JSON.parse(presetText) }); } catch { setError("El preset no es JSON válido."); } }} className="mt-3 rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Guardar preset</button>
        </section>
      </div>
      <section className="overflow-hidden rounded-2xl bg-surface-2/40 ring-1 ring-white/[0.06]">
        <div className="flex items-center justify-between border-b border-white/[0.06] p-4"><h2 className="font-semibold text-white">Canciones {phase ? `· ${labels[phase]}` : ""}</h2><span className="text-xs text-ink-tertiary">Página {page}/{pages}</span></div>
        <div className="divide-y divide-white/[0.05]">
          {items.map((item) => <div key={item.id} className="grid gap-3 p-4 md:grid-cols-[55px_1fr_180px_auto] md:items-center"><span className="text-xs text-ink-tertiary">#{item.ordinal}</span><div className="min-w-0"><div className="truncate text-sm font-medium text-white">{item.title || item.filename}</div><div className="truncate text-xs text-ink-tertiary">{item.artist || "Falta artista"} · {item.technical_code || "Falta código"}</div></div><span className="text-xs text-ink-secondary">{labels[item.phase] || item.phase}</span><div className="flex gap-2">{item.metadata_error && <button onClick={() => editMetadata(item)} className="rounded-lg bg-amber-500/10 px-3 py-1.5 text-xs text-amber-100">Completar metadata</button>}{item.job_id && item.phase === "lyrics_ready" && <button onClick={() => navigate(`/review/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Corregir</button>}{item.phase === "failed" && <button onClick={() => retry(item)} className="rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-200">Reintentar</button>}</div></div>)}
          {!items.length && <div className="p-10 text-center text-sm text-ink-tertiary">No hay items en este filtro.</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-white/[0.06] p-4"><button disabled={page <= 1} onClick={() => setPage((p) => p - 1)} className="rounded-lg bg-white/[0.06] px-3 py-2 text-xs text-white disabled:opacity-30">Anterior</button><button disabled={page >= pages} onClick={() => setPage((p) => p + 1)} className="rounded-lg bg-white/[0.06] px-3 py-2 text-xs text-white disabled:opacity-30">Siguiente</button></div>
      </section>
    </div>
  );
}

export default function CampaignsPage() {
  const { campaignId } = useParams();
  return campaignId ? <CampaignDetail id={campaignId} /> : <CampaignList />;
}
