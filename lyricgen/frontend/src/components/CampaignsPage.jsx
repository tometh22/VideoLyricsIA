import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { editorSessionHeaders } from "../lib/editorSession";
import { CampaignReviewerRow, CampaignReviewerSummary } from "./CampaignReviewerStatus";

const API = import.meta.env.VITE_API_URL || "";
const PHASES = [
  ["", "Todos"], ["waiting_upload", "Esperando carga"],
  ["uploading", "Subiendo"], ["waiting_processing", "En espera"],
  ["transcribing", "Transcribiendo"], ["lyrics_ready", "Listo para corregir"],
  ["lyrics_approved", "Letra y timing aprobados"],
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
  const [queueStage, setQueueStage] = useState("lyrics");
  const [queueOrder, setQueueOrder] = useState("delivery");
  const [queueVersion, setQueueVersion] = useState("");
  const [queueState, setQueueState] = useState("");
  const [queueBackground, setQueueBackground] = useState("");
  const [queueArtist, setQueueArtist] = useState("");
  const [queueAudit, setQueueAudit] = useState(false);
  const [reviewQueue, setReviewQueue] = useState(null);

  const load = useCallback(async () => {
    try {
      const [head, rows, review] = await Promise.all([
        api(`/batch/campaigns/${id}`),
        api(`/batch/campaigns/${id}/items?page=${page}&limit=50${phase ? `&phase=${phase}` : ""}`),
        api(`/batch/campaigns/${id}/review-queue?stage=${queueStage}&order=${queueOrder}${queueVersion ? `&version=${queueVersion}` : ""}${queueState ? `&state=${encodeURIComponent(queueState)}` : ""}${queueStage === "final" && queueBackground ? `&background_mode=${encodeURIComponent(queueBackground)}` : ""}${queueArtist ? `&artist=${encodeURIComponent(queueArtist)}` : ""}${queueAudit ? "&audit_preapproved=true" : ""}`),
      ]);
      setCampaign(head); setItems(rows.items || []); setPages(rows.pages || 1);
      setReviewQueue(review);
      setPresetText((current) => current || JSON.stringify(head.default_render_params || {}, null, 2));
      setError("");
    } catch (e) { setError(e.message); }
  }, [id, page, phase, queueStage, queueOrder, queueVersion, queueState, queueBackground, queueArtist, queueAudit]);
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
  const takeNext = async (stage = queueStage) => {
    try {
      const data = await api(`/batch/campaigns/${id}/review-queue/next?stage=${stage}`, { method: "POST", headers: editorSessionHeaders() });
      if (data.job_id) navigate(data.open_path || (stage === "lyrics" ? `/review/${data.job_id}` : `/videos/${data.job_id}`));
      else setError(stage === "lyrics" ? "Todavía no hay letras listas para corregir." : "Todavía no hay renders listos para QC final.");
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
          <button onClick={() => takeNext(queueStage)} disabled={queueStage === "lyrics" ? !reviewQueue?.counters?.ready : !reviewQueue?.counters?.ready} className="rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white disabled:opacity-40">Tomar siguiente · {queueStage === "lyrics" ? "letra" : "QC final"}</button>
          {campaign.status === "active" ? <button onClick={() => patch({ status: "paused" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Pausar</button> : campaign.status === "paused" ? <button onClick={() => patch({ status: "active" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Reanudar</button> : null}
          {!['completed', 'cancelled'].includes(campaign.status) && <button onClick={() => window.confirm("¿Cancelar esta campaña?") && patch({ status: "cancelled" })} className="rounded-xl bg-red-500/10 px-4 py-2.5 text-sm text-red-200">Cancelar</button>}
        </div>
      </div>
      {error && <div className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25">{error}</div>}
      <CampaignReviewerSummary status={campaign.reviewer_campaign_status} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {PHASES.slice(1).map(([key, label]) => <Counter key={key} label={label} value={campaign.counters?.[key]} active={phase === key} onClick={() => { setPhase(phase === key ? "" : key); setPage(1); }} />)}
      </div>
      <section className="space-y-4 rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="font-semibold text-white">Cola de revisión en dos etapas</h2>
            <p className="mt-1 text-xs text-ink-tertiary">El fondo y el render se habilitan sólo después de aprobar letra y timing contra el audio completo.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select value={queueStage} onChange={(e) => setQueueStage(e.target.value)} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10">
              <option value="lyrics">1 · Letra y timing</option><option value="final">2 · QC final</option>
            </select>
            <select value={queueOrder} onChange={(e) => setQueueOrder(e.target.value)} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10">
              <option value="delivery">Orden entrega</option><option value="learning">Aprendizaje (20%)</option>
            </select>
            <select value={queueVersion} onChange={(e) => setQueueVersion(e.target.value)} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10">
              <option value="">Studio + live</option><option value="studio">Studio</option><option value="live">Live</option>
            </select>
            <select value={queueState} onChange={(e) => setQueueState(e.target.value)} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10">
              <option value="">Todos los estados</option><option value="pending">Pendiente</option><option value="processing">Procesando</option><option value="ready">Lista</option><option value="reviewing">En revisión</option><option value="approved">Aprobada</option><option value="exported">Exportada</option>
            </select>
            {queueStage === "final" && <input value={queueBackground} onChange={(e) => setQueueBackground(e.target.value)} placeholder="Modo de fondo" className="w-32 rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10" />}
            <input value={queueArtist} onChange={(e) => setQueueArtist(e.target.value)} placeholder="Artista" className="w-32 rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10" />
            {reviewQueue?.confidence?.preapproved_audit_available && <button onClick={() => setQueueAudit((value) => !value)} className={`rounded-lg px-3 py-2 text-xs ring-1 ${queueAudit ? "bg-emerald-500/15 text-emerald-200 ring-emerald-500/30" : "bg-black/25 text-white ring-white/10"}`}>Auditar verdes preaprobados</button>}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-7">
          {["pending", "processing", "ready", "reviewing", "approved", "approved_today", "exported"].map((key) => <div key={key} className="rounded-xl bg-black/20 p-3"><div className="text-lg font-bold text-white">{reviewQueue?.counters?.[key] || 0}</div><div className="text-[11px] uppercase tracking-wide text-ink-tertiary">{key}</div></div>)}
        </div>
        <div className="flex flex-wrap gap-4 text-xs text-ink-secondary">
          <span>Promedio hoy: {reviewQueue?.review_minutes_today?.average ?? "—"} min</span>
          {queueStage === "final" && <span>Fondos fijos: {reviewQueue?.background_split?.fixed || 0}</span>}
          {queueStage === "final" && <span>Fondos generados: {reviewQueue?.background_split?.generated || 0}</span>}
          <span>{reviewQueue?.confidence?.colors_visible ? "Semáforo visible" : `Semáforo oculto hasta ${reviewQueue?.confidence?.calibration_target || 50} revisiones`}</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[850px] text-left text-xs">
            <thead className="text-ink-tertiary"><tr><th className="p-2">Prioridad</th><th className="p-2">Artista / canción</th><th className="p-2">Versión</th>{queueStage === "final" && <th className="p-2">Fondo</th>}<th className="p-2">Duración</th><th className="p-2">Estado</th><th className="p-2">Referencia</th><th className="p-2"></th></tr></thead>
            <tbody>{(reviewQueue?.items || []).map((row) => <tr key={row.item_id} className="border-t border-white/[0.05] text-ink-secondary">
              <td className="p-2">{row.priority}</td>
              <td className="p-2"><div className="font-medium text-white">{row.title}</div><div>{row.artist}</div>
                {queueStage === "lyrics" && campaign.reviewer_campaign_status?.enabled === true && <CampaignReviewerRow status={row.reviewer_campaign_status} jobId={row.job_id} onOpen={navigate} />}
              </td>
              <td className="p-2">{row.version}</td>{queueStage === "final" && <td className="p-2">{row.background_mode}</td>}
              <td className="p-2">{row.duration_seconds ? `${Math.round(row.duration_seconds)}s` : "—"}</td>
              <td className="p-2">{row.state}{row.reviewer_name ? ` · ${row.reviewer_name}` : ""}</td>
              <td className="p-2">{row.reference?.provider || "pendiente"} · {row.reference?.status || "sin asociar"}</td>
              <td className="p-2">{row.open_path && <button onClick={() => navigate(row.open_path)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-brand-light">Abrir</button>}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </section>
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
          {items.map((item) => <div key={item.id} className="grid gap-3 p-4 md:grid-cols-[55px_1fr_180px_auto] md:items-center">
            <span className="text-xs text-ink-tertiary">#{item.ordinal}</span>
            <div className="min-w-0"><div className="truncate text-sm font-medium text-white">{item.title || item.filename}</div>
              <div className="truncate text-xs text-ink-tertiary">{item.artist || "Falta artista"} · {item.technical_code || "Falta código"}</div>
              {campaign.reviewer_campaign_status?.enabled === true && <CampaignReviewerRow status={item.reviewer_campaign_status} jobId={item.job_id} onOpen={navigate} />}
            </div>
            <span className="text-xs text-ink-secondary">{labels[item.phase] || item.phase}</span>
            <div className="flex gap-2">{item.metadata_error && <button onClick={() => editMetadata(item)} className="rounded-lg bg-amber-500/10 px-3 py-1.5 text-xs text-amber-100">Completar metadata</button>}{item.job_id && item.phase === "lyrics_ready" && <button onClick={() => navigate(`/review/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Corregir</button>}{item.phase === "failed" && <button onClick={() => retry(item)} className="rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-200">Reintentar</button>}</div>
          </div>)}
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
