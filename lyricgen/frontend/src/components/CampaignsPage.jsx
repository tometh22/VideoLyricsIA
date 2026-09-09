import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  const [expected, setExpected] = useState(500);
  const [kind, setKind] = useState("lyric_video");
  const [destination, setDestination] = useState("argentina");
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
          kind, destination_portal: kind === "art_track" ? destination : null,
          default_render_params: kind === "art_track"
            ? { delivery_profile: "both", art_track: true }
            : { background_mode: "ai", delivery_profile: "youtube" },
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
      <form onSubmit={create} className="grid gap-3 rounded-2xl bg-surface-2/50 p-5 ring-1 ring-white/[0.06] md:grid-cols-[1fr_150px_170px_150px_auto]">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nombre de la campaña" maxLength={160} className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none focus:ring-brand/50" />
        <input value={expected} onChange={(e) => setExpected(e.target.value)} type="number" min="1" max={kind === "art_track" ? 500 : 1000} aria-label="Cantidad esperada" className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none focus:ring-brand/50" />
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Tipo de campaña" className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none"><option value="lyric_video">Lyric videos</option><option value="art_track">Art tracks</option></select>
        <select value={destination} onChange={(e) => setDestination(e.target.value)} disabled={kind !== "art_track"} aria-label="Portal de destino" className="rounded-xl bg-black/20 px-4 py-3 text-sm text-white ring-1 ring-white/10 outline-none"><option value="argentina">UMG Argentina</option><option value="chile">UMG Chile</option></select>
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
  const [assetBusy, setAssetBusy] = useState(false);
  const [deliveryMessage, setDeliveryMessage] = useState("");
  const fileInput = useRef(null);

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

  const digest = async (file) => {
    const bytes = await file.arrayBuffer();
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
  };
  const uploadAsset = async (asset) => {
    const ticket = await api(`/batch/art-track-assets/${asset.id}/ticket`, { method: "POST" });
    if (ticket.complete) return;
    const file = asset.file;
    if (!ticket.use_multipart) {
      const response = await fetch(ticket.upload_url, { method: "PUT", headers: { "Content-Type": ticket.content_type }, body: file });
      if (!response.ok) throw new Error(`No se pudo subir ${file.name}`);
      await api(`/batch/art-track-assets/${asset.id}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ parts: [] }) });
      return;
    }
    const parts = [];
    const uploaded = new Map((ticket.uploaded_parts || []).map((part) => [
      Number(part.part_number || part.PartNumber),
      String(part.etag || part.ETag || "").replaceAll('"', ""),
    ]));
    for (const part of ticket.parts) {
      if (uploaded.has(part.part_number) && uploaded.get(part.part_number)) {
        parts.push({ part_number: part.part_number, etag: uploaded.get(part.part_number) });
        continue;
      }
      const start = (part.part_number - 1) * ticket.part_size;
      const response = await fetch(part.url, { method: "PUT", body: file.slice(start, Math.min(start + ticket.part_size, file.size)) });
      if (!response.ok) throw new Error(`No se pudo subir la parte ${part.part_number} de ${file.name}`);
      parts.push({ part_number: part.part_number, etag: (response.headers.get("ETag") || "").replaceAll('"', "") });
    }
    await api(`/batch/art-track-assets/${asset.id}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ parts }) });
  };
  const importArtFiles = async (event) => {
    const files = [...(event.target.files || [])];
    if (!files.length || campaign.kind !== "art_track") return;
    setAssetBusy(true); setError("");
    try {
      const audios = [], covers = [];
      for (const file of files) {
        const relative = file.webkitRelativePath || file.name;
        const sha = await digest(file);
        if (/\.(wav|mp3)$/i.test(file.name)) {
          const bits = file.name.replace(/\.[^.]+$/, "").split(" - ");
          audios.push({ client_id: sha, filename: file.name, relative_path: relative, artist: bits.length > 1 ? bits[0] : "", title: bits.length > 1 ? bits.slice(1).join(" - ") : bits[0], size_bytes: file.size, sha256: sha });
        } else if (/\.(jpg|jpeg|png)$/i.test(file.name)) {
          covers.push({ filename: file.name, relative_path: relative, size_bytes: file.size, sha256: sha, mime_type: file.type || undefined });
        }
      }
      const manifest = await api(`/batch/art-track-campaigns/${id}/manifest`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ audios, covers }) });
      const assets = await api(`/batch/art-track-campaigns/${id}/assets`);
      const local = new Map(files.map((file) => [file.webkitRelativePath || file.name, file]));
      for (const asset of assets.items || []) {
        const file = local.get(asset.relative_path) || local.get(asset.filename);
        if (file && asset.upload_state !== "uploaded") await uploadAsset({ ...asset, file });
      }
      setDeliveryMessage(`${manifest.registered_count} audios registrados; ${manifest.matched_count} covers asociados. Confirmá las asociaciones antes de generar.`);
      await load();
    } catch (e) { setError(e.message); }
    finally { setAssetBusy(false); if (fileInput.current) fileInput.current.value = ""; }
  };
  const confirmAndRender = async () => {
    try {
      await api(`/batch/art-track-campaigns/${id}/associations/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm_all_matched: true }) });
      const result = await api(`/batch/art-track-campaigns/${id}/start-rendering`, { method: "POST" });
      setDeliveryMessage(`Generación iniciada: ${result.created_count} art tracks; ${result.blocked_item_ids?.length || 0} pendientes de asociación.`); await load();
    } catch (e) { setError(e.message); }
  };
  const previewDeliveries = async () => {
    try { const result = await api(`/batch/art-track-campaigns/${id}/delivery-preview`); setDeliveryMessage(`${result.eligible_count} art tracks aprobados para ${result.hostname}.`); }
    catch (e) { setError(e.message); }
  };
  const createDeliveries = async () => {
    try { const result = await api(`/batch/art-track-campaigns/${id}/deliveries`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: `ui-${id}-${Date.now()}` }) }); setDeliveryMessage(`Envío durable creado: ${result.total_count} canciones a ${result.hostname}.`); }
    catch (e) { setError(e.message); }
  };

  const labels = useMemo(() => Object.fromEntries(PHASES), []);
  if (!campaign) return <div className="p-8 text-sm text-ink-secondary">{error || "Cargando campaña…"}</div>;
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <button onClick={() => navigate("/campaigns")} className="text-sm text-ink-secondary hover:text-white">← Campañas</button>
      <div className="flex flex-col gap-4 md:flex-row md:items-start">
      <div className="min-w-0 flex-1"><h1 className="truncate text-3xl font-bold text-white">{campaign.name}</h1><p className="mt-2 text-sm text-ink-secondary">{campaign.registered_count}/{campaign.expected_count || "—"} {campaign.kind === "art_track" ? "art tracks" : "audios"} registrados · estado {campaign.status}</p></div>
        <div className="flex flex-wrap gap-2">
          {campaign.kind !== "art_track" && <button onClick={takeNext} disabled={!campaign.counters?.lyrics_ready} className="rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white disabled:opacity-40">Tomar siguiente</button>}
          {campaign.status === "active" ? <button onClick={() => patch({ status: "paused" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Pausar</button> : campaign.status === "paused" ? <button onClick={() => patch({ status: "active" })} className="rounded-xl bg-white/[0.06] px-4 py-2.5 text-sm text-white">Reanudar</button> : null}
          {!['completed', 'cancelled'].includes(campaign.status) && <button onClick={() => window.confirm("¿Cancelar esta campaña?") && patch({ status: "cancelled" })} className="rounded-xl bg-red-500/10 px-4 py-2.5 text-sm text-red-200">Cancelar</button>}
        </div>
      </div>
      {error && <div className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25">{error}</div>}
      {deliveryMessage && <div className="rounded-xl bg-brand/10 p-4 text-sm text-brand-light ring-1 ring-brand/25">{deliveryMessage}</div>}
      {campaign.kind === "art_track" && <section className="rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-white">Art tracks · audio + cover</h2><p className="mt-1 text-sm text-ink-secondary">Seleccioná hasta 500 audios y portadas. Se asocian por ruta, código, nombre o cover de carpeta; los ambiguos quedan bloqueados.</p></div><input ref={fileInput} type="file" multiple webkitdirectory="" directory="" accept=".wav,.mp3,.jpg,.jpeg,.png" onChange={importArtFiles} className="max-w-[260px] text-xs text-ink-secondary" /></div><div className="mt-4 flex flex-wrap gap-2"><button disabled={assetBusy} onClick={confirmAndRender} className="rounded-xl bg-brand px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{assetBusy ? "Subiendo…" : "Confirmar asociados y generar"}</button><button onClick={previewDeliveries} className="rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Previsualizar envíos</button><button onClick={createDeliveries} className="rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Enviar aprobados</button></div></section>}
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
          {items.map((item) => <div key={item.id} className="grid gap-3 p-4 md:grid-cols-[55px_1fr_180px_auto] md:items-center"><span className="text-xs text-ink-tertiary">#{item.ordinal}</span><div className="min-w-0"><div className="truncate text-sm font-medium text-white">{item.title || item.filename}</div><div className="truncate text-xs text-ink-tertiary">{item.artist || "Falta artista"} · {item.technical_code || "Falta código"}</div></div><span className="text-xs text-ink-secondary">{labels[item.phase] || item.phase}</span><div className="flex gap-2">{item.metadata_error && <button onClick={() => editMetadata(item)} className="rounded-lg bg-amber-500/10 px-3 py-1.5 text-xs text-amber-100">Completar metadata</button>}{item.job_id && campaign.kind === "art_track" && ["final_review", "done"].includes(item.phase) && <button onClick={() => navigate(`/videos/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Revisar</button>}{item.job_id && campaign.kind !== "art_track" && item.phase === "lyrics_ready" && <button onClick={() => navigate(`/review/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Corregir</button>}{item.phase === "failed" && <button onClick={() => retry(item)} className="rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-200">Reintentar</button>}</div></div>)}
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
