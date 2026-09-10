import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { editorSessionHeaders } from "../lib/editorSession";
import { CampaignReviewerRow, CampaignReviewerSummary } from "./CampaignReviewerStatus";
import ReviewScopes from "./ReviewScopes";
import CampaignReviewWork from "./CampaignReviewWork";
import useLatestReviewRequest from "../hooks/useLatestReviewRequest";
import { reviewCounts, reviewStateLabel, reviewActionLabel, validReviewScope, reviewStateFilter } from "../lib/reviewerNavigation";

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

function timestamp(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

function contextPath(id, values = {}) {
  const params = new URLSearchParams();
  const entries = {
    tab: values.tab || "pending",
    q: values.q || "",
    mine: values.mine ? "1" : "",
    order: values.order || "effort",
    page: values.page > 1 ? String(values.page) : "",
    stage: values.stage && values.stage !== "lyrics" ? values.stage : "",
    version: values.version || "",
    state: values.state || "",
    artist: values.artist || "",
    phase: values.phase || "",
    focus: values.focus || "",
    scroll: values.scroll ? String(Math.round(values.scroll)) : "",
  };
  Object.entries(entries).forEach(([key, value]) => { if (value) params.set(key, value); });
  const query = params.toString();
  return `/campaigns/${encodeURIComponent(id)}${query ? `?${query}` : ""}`;
}

function returnReviewPath(id, jobId, values = {}) {
  const returnTo = contextPath(id, values);
  return `/review/${encodeURIComponent(jobId)}?return_to=${encodeURIComponent(returnTo)}`;
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    // Campaign state changes while the operator is in the editor. A cached
    // GET can therefore show the song removed from the active filter while
    // leaving the campaign counters at their previous value.
    cache: options.cache || "no-store",
    headers: authHeaders(options.headers || {}),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(
    typeof body.detail === "string" ? body.detail : body.detail?.code || `Error ${response.status}`,
  );
  return body;
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
  const [loading, setLoading] = useState(true);
  const { start, cancel } = useLatestReviewRequest();
  const load = useCallback(async () => {
    const request = start();
    setLoading(true);
    try {
      const data = await api("/batch/campaigns", { signal: request.signal });
      if (request.current()) { setItems(data.items || []); setError(""); }
    } catch (e) { if (request.current()) setError(e.message); }
    finally { if (request.current()) setLoading(false); request.finish(); }
  }, [start]);
  useEffect(() => { load(); return cancel; }, [load, cancel]);

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
      {loading && <p role="status" className="text-sm text-ink-secondary">Cargando campañas…</p>}
      <div className="grid gap-3">
        {items.map((campaign) => (
          <button key={campaign.id} onClick={() => navigate(`/campaigns/${campaign.id}`)} className="flex items-center gap-4 rounded-2xl bg-surface-2/40 p-5 text-left ring-1 ring-white/[0.06] hover:ring-brand/30">
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold text-white">{campaign.name}</div>
              <div className="mt-1 text-xs text-ink-tertiary">{campaign.registered_count}/{campaign.expected_count || "—"} registradas · {campaign.counters?.done || 0} terminadas</div>
            </div>
            <span className="rounded-full bg-white/[0.06] px-3 py-1 text-xs text-ink-secondary">{{ active: "Activa", paused: "Pausada", completed: "Completada", cancelled: "Cancelada" }[campaign.status] || "Estado no disponible"}</span>
          </button>
        ))}
        {!items.length && !error && !loading && <div className="rounded-2xl border border-dashed border-white/10 p-10 text-center text-sm text-ink-tertiary">Todavía no hay campañas.</div>}
      </div>
    </div>
  );
}

function CampaignDetail({ id }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialScope = validReviewScope(searchParams.get("tab"));
  const initialOrder = searchParams.get("order") === "learning" ? "learning" : "effort";
  const [campaign, setCampaign] = useState(null);
  const [items, setItems] = useState([]);
  const [adminOpen, setAdminOpen] = useState(false);
  const [phase, setPhase] = useState(() => searchParams.get("phase") || "");
  const [page, setPage] = useState(() => Number(searchParams.get("page") || 1) || 1);
  const [pages, setPages] = useState(1);
  const [pair, setPair] = useState(null);
  const [error, setError] = useState("");
  const [presetText, setPresetText] = useState("");
  const [assetBusy, setAssetBusy] = useState(false);
  const [deliveryMessage, setDeliveryMessage] = useState("");
  const fileInput = useRef(null);
  const [queueStage, setQueueStage] = useState(() => searchParams.get("stage") || "lyrics");
  const [queueOrder, setQueueOrder] = useState(initialOrder);
  const [queueVersion, setQueueVersion] = useState(() => searchParams.get("version") || "");
  const [queueState, setQueueState] = useState(() => reviewStateFilter(searchParams.get("state"), initialScope));
  const [queueScope, setQueueScope] = useState(initialScope);
  const [queueBackground, setQueueBackground] = useState("");
  const [queueArtist, setQueueArtist] = useState(() => searchParams.get("artist") || "");
  const [queueSearch, setQueueSearch] = useState(() => searchParams.get("q") || "");
  const [queueMine, setQueueMine] = useState(() => searchParams.get("mine") === "1");
  const [discardTarget, setDiscardTarget] = useState(null);
  const [discardReason, setDiscardReason] = useState("Instrumental · solicitud del cliente");
  const [discardBusy, setDiscardBusy] = useState(false);
  const [queueAudit, setQueueAudit] = useState(false);
  const [reviewQueue, setReviewQueue] = useState(null);
  const [highlightedJobId, setHighlightedJobId] = useState(() => searchParams.get("focus") || null);
  const restoredScrollRef = useRef(null);
  const [queueLoading, setQueueLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [totals, setTotals] = useState(null);
  const { start, cancel, active } = useLatestReviewRequest();

  useEffect(() => {
    const discardId = searchParams.get("discard");
    if (!discardId || queueLoading || !reviewQueue) return;
    const row = reviewQueue.items?.find(item => item.item_id === discardId);
    if (row?.can_discard) setDiscardTarget(row);
    else setError("La canción cambió de estado y no se puede descartar desde esta revisión.");
    const next = new URLSearchParams(searchParams);
    next.delete("discard");
    setSearchParams(next, { replace: true });
  }, [queueLoading, reviewQueue, searchParams, setSearchParams]);

  // URL is authoritative for browser Back/Forward and copied links. Saved
  // context must not resurrect a stale status filter after changing tabs.
  useEffect(() => {
    setQueueScope(validReviewScope(searchParams.get("tab")));
    setQueueOrder(searchParams.get("order") === "learning" ? "learning" : "effort");
    setQueueSearch(searchParams.get("q") || "");
    setQueueMine(searchParams.get("mine") === "1");
    setQueueStage(searchParams.get("stage") === "final" ? "final" : "lyrics");
    setQueueVersion(searchParams.get("version") || "");
    setQueueState(reviewStateFilter(searchParams.get("state"), validReviewScope(searchParams.get("tab"))));
    setQueueArtist(searchParams.get("artist") || "");
    setPhase(searchParams.get("phase") || "");
    setPage(Math.max(1, Number(searchParams.get("page")) || 1));
    setHighlightedJobId(searchParams.get("focus") || null);
  }, [searchParams]);

  const saveContext = useCallback((overrides = {}, push = false) => {
    const values = {
      tab: queueScope, q: queueSearch, mine: queueMine, order: queueOrder,
      page, stage: queueStage, version: queueVersion, state: queueState,
      artist: queueArtist, phase, ...overrides,
    };
    const next = new URLSearchParams(searchParams);
    ["tab", "q", "mine", "order", "page", "stage", "version", "state", "artist", "phase", "focus", "scroll"].forEach((key) => next.delete(key));
    const path = contextPath(id, values);
    const query = path.split("?")[1];
    if (query) new URLSearchParams(query).forEach((value, key) => next.set(key, value));
    setSearchParams(next, { replace: !push });
    try { sessionStorage.setItem(`campaign-review-context:${id}`, JSON.stringify(values)); } catch { /* best effort */ }
  }, [id, page, phase, queueStage, queueVersion, queueState, queueArtist, queueMine, queueOrder, queueScope, queueSearch, searchParams, setSearchParams]);

  const selectScope = (scope) => {
    if (scope === queueScope && !queueState) return;
    cancel(); setReviewQueue(null); setQueueLoading(true); setError("");
    setQueueScope(scope); setQueueState(""); setHighlightedJobId(null);
    saveContext({ tab: scope, state: "", focus: null, scroll: null, page: 1 }, true);
  };

  const openReview = useCallback((row) => {
    if (!row?.job_id) return;
    const context = {
      tab: queueScope, q: queueSearch, mine: queueMine, order: queueOrder,
      page, stage: queueStage, version: queueVersion, state: queueState,
      artist: queueArtist, phase, focus: row.job_id, scroll: window.scrollY,
    };
    try { sessionStorage.setItem(`campaign-review-context:${id}`, JSON.stringify(context)); } catch { /* best effort */ }
    if (row.state === "approved" || row.state === "exported") {
      navigate(`/videos/${encodeURIComponent(row.job_id)}?return_to=${encodeURIComponent(contextPath(id, context))}`);
    } else {
      navigate(returnReviewPath(id, row.job_id, context));
    }
  }, [id, navigate, page, phase, queueStage, queueVersion, queueState, queueArtist, queueMine, queueOrder, queueScope, queueSearch]);

  const load = useCallback(async () => {
    const request = start();
    const options = { signal: request.signal };
    try {
      const queueQuery = `stage=${queueStage}&order=${queueOrder}&scope=${queueScope}&limit=1000${queueVersion ? `&version=${queueVersion}` : ""}${queueState ? `&state=${encodeURIComponent(queueState)}` : ""}${queueStage === "final" && queueBackground ? `&background_mode=${encodeURIComponent(queueBackground)}` : ""}${queueArtist ? `&artist=${encodeURIComponent(queueArtist)}` : ""}${queueSearch ? `&search=${encodeURIComponent(queueSearch)}` : ""}${queueMine ? "&reviewed_by=me" : ""}${queueAudit ? "&audit_preapproved=true" : ""}`;
      const firstReview = await api(`/batch/campaigns/${id}/review-queue?${queueQuery}`, options);
      const head = firstReview.campaign || await api(`/batch/campaigns/${id}`, options);
      if (!request.current()) return;
      setCampaign(head);
      setReviewQueue(firstReview); setTotals(firstReview.campaign_totals);
      setQueueLoading(false); setLoadingMore(Number(firstReview.pages || 1) > 1); setError("");
      const remainingReviews = await Promise.all(
        Array.from({ length: Math.max(0, Number(firstReview.pages || 1) - 1) }, (_, index) => (
          api(`/batch/campaigns/${id}/review-queue?${queueQuery}&page=${index + 2}`, options)
        )),
      );
      const review = {
        ...firstReview,
        items: [firstReview.items || [], ...remainingReviews.map((pageData) => pageData.items || [])].flat(),
      };
      if (!request.current()) return;
      setReviewQueue(review);
      setPresetText((current) => current || JSON.stringify(head.default_render_params || {}, null, 2));
      setError("");
    } catch (e) { if (request.current()) setError(e.message); }
    finally {
      if (request.current()) { setQueueLoading(false); setLoadingMore(false); }
      request.finish();
    }
  }, [id, page, phase, queueStage, queueOrder, queueScope, queueVersion, queueState, queueBackground, queueArtist, queueSearch, queueMine, queueAudit, start]);
  useEffect(() => {
    setQueueLoading(true);
    const timer = window.setTimeout(load, queueSearch ? 300 : 0);
    return () => { window.clearTimeout(timer); cancel(); };
  }, [load, cancel, queueSearch]);
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible" && !active.current) void load();
    };
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("pageshow", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [load]);
  useEffect(() => {
    if (!campaign || ["completed", "cancelled"].includes(campaign.status)) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !active.current) void load();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [campaign?.status, load]);

  useEffect(() => {
    if (!reviewQueue || restoredScrollRef.current === highlightedJobId) return;
    restoredScrollRef.current = highlightedJobId;
    let saved = Number(searchParams.get("scroll") || 0);
    if (!saved) {
      try { saved = Number(JSON.parse(sessionStorage.getItem(`campaign-review-context:${id}`) || "{}").scroll || 0); } catch { saved = 0; }
    }
    requestAnimationFrame(() => {
      if (saved > 0) window.scrollTo(0, saved);
      if (highlightedJobId) document.querySelector(`[data-review-job="${highlightedJobId}"]`)?.scrollIntoView({ block: "center" });
    });
    const timer = window.setTimeout(() => {
      if (searchParams.get("focus")) {
        const next = new URLSearchParams(searchParams);
        next.delete("focus"); next.delete("scroll");
        setSearchParams(next, { replace: true });
      }
    }, 5000);
    return () => window.clearTimeout(timer);
  }, [id, highlightedJobId, reviewQueue, searchParams, setSearchParams]);

  useEffect(() => {
    if (!adminOpen) return;
    const controller = new AbortController();
    api(`/batch/campaigns/${id}/items?page=${page}&limit=50${phase ? `&phase=${phase}` : ""}`, { signal: controller.signal })
      .then(rows => { if (!controller.signal.aborted) { setItems(rows.items || []); setPages(rows.pages || 1); } })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [adminOpen, id, page, phase]);

  const patch = async (value) => {
    try {
      await api(`/batch/campaigns/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
      await load();
    } catch (e) { setError(e.message); }
  };
  const takeNext = async (stage = queueStage) => {
    try {
      const params = new URLSearchParams({ stage });
      if (queueSearch) params.set("search", queueSearch);
      if (queueVersion) params.set("version", queueVersion);
      if (queueArtist) params.set("artist", queueArtist);
      if (queueMine) params.set("reviewed_by", "me");
      const data = await api(`/batch/campaigns/${id}/review-queue/next?${params.toString()}`, { method: "POST", headers: editorSessionHeaders() });
      if (data.job_id) openReview({ job_id: data.job_id });
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
  const selectedRow = (reviewQueue?.items || []).find((row) => row.job_id === highlightedJobId);
  const counts = reviewCounts({ campaign_totals: totals }, campaign);
  const approvedCount = counts.approved ?? "—";
  const songCount = counts.all ?? "—";
  const clearFilters = () => {
    setQueueSearch(""); setQueueArtist(""); setQueueVersion(""); setQueueState(""); setQueueMine(false);
    setHighlightedJobId(null); saveContext({ q: "", mine: false, version: "", artist: "", state: "", focus: null, scroll: null });
    try { sessionStorage.setItem(`campaign-review-context:${id}`, JSON.stringify({ tab: queueScope, order: queueOrder, page })); } catch { /* best effort */ }
  };
  const queueRows = reviewQueue?.items || [];
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <button onClick={() => navigate("/campaigns")} className="text-sm text-ink-secondary hover:text-white">← Campañas</button>
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0"><h1 className="truncate text-3xl font-bold text-white">{campaign.name}</h1><p className="mt-2 text-sm text-ink-secondary">{campaign.kind === "art_track" ? `${campaign.registered_count}/${campaign.expected_count || "—"} art tracks registrados` : `${approvedCount} de ${songCount} letras aprobadas`} · {campaign.status === "active" ? "campaña activa" : campaign.status}</p></div>
        {campaign.kind !== "art_track" && <button onClick={() => takeNext(queueStage)} disabled={queueLoading || !!error || queueScope !== "pending" || !reviewQueue?.counters?.ready} className="rounded-xl bg-brand px-5 py-3 text-sm font-semibold text-white disabled:opacity-40">Revisar siguiente canción</button>}
      </div>
      {campaign.kind === "art_track" && <section className="rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold text-white">Art tracks · audio + cover</h2><p className="mt-1 text-sm text-ink-secondary">Seleccioná una carpeta con hasta 500 audios y portadas. Los conflictos quedan bloqueados para corrección manual.</p></div><input ref={fileInput} type="file" multiple webkitdirectory="" directory="" accept=".wav,.mp3,.jpg,.jpeg,.png" onChange={importArtFiles} className="max-w-[260px] text-xs text-ink-secondary" /></div><div className="mt-4 flex flex-wrap gap-2"><button disabled={assetBusy} onClick={confirmAndRender} className="rounded-xl bg-brand px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{assetBusy ? "Subiendo…" : "Confirmar asociados y generar"}</button><button onClick={previewDeliveries} className="rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Previsualizar envíos</button><button onClick={createDeliveries} className="rounded-xl bg-white/[0.07] px-4 py-2 text-sm text-white">Enviar aprobados</button></div></section>}
      {selectedRow?.resume_available && <div className="flex items-center justify-between gap-3 rounded-xl bg-brand/10 p-4 text-sm text-brand-light ring-1 ring-brand/25"><span>Tenés una revisión guardada: <strong>{selectedRow.title}</strong>.</span><button onClick={() => openReview(selectedRow)} className="rounded-lg bg-brand px-3 py-2 text-xs font-semibold text-white">Continuar {selectedRow.title}</button></div>}
      {!queueLoading && !loadingMore && highlightedJobId && !selectedRow && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25"><span>La canción recién abierta ya no coincide con este filtro. Puede haber cambiado de estado o estar aprobada en otra pestaña.</span><button onClick={() => selectScope("approved")} className="rounded-lg bg-amber-500/20 px-3 py-2 text-xs font-semibold">Buscar en Aprobadas</button></div>}
      {error && <div role="alert" className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25">{error} <button onClick={load} className="underline">Reintentar</button></div>}
      <ReviewScopes value={queueScope} counts={counts} onChange={selectScope} />
      <p className="text-xs text-ink-secondary">Las alertas orientan la revisión; no son un porcentaje de acierto. La confianza todavía no está calibrada.</p>
      {queueLoading && <p role="status" className="text-sm text-ink-secondary">Cargando canciones…</p>}
      {loadingMore && <p role="status" className="text-sm text-ink-secondary">Mostrando {reviewQueue?.items?.length || 0} de {reviewQueue?.scope?.total || 0} canciones. Cargando el resto…</p>}
      {discardTarget && <section role="dialog" aria-modal="true" aria-label={discardTarget.state === "discarded" ? "Recuperar canción" : "Descartar canción"} className="rounded-2xl bg-surface-2 p-5 ring-1 ring-amber-400/30">
        <h2 className="font-semibold text-white">{discardTarget.state === "discarded" ? "Recuperar" : "Descartar"} · {discardTarget.title}</h2>
        <p className="mt-2 text-sm text-ink-secondary">{discardTarget.artist} · El audio y el borrador se conservan. El cambio queda registrado.</p>
        {discardTarget.state !== "discarded" && <label className="mt-3 block text-sm text-ink-secondary">Motivo<input autoFocus value={discardReason} onChange={e => setDiscardReason(e.target.value)} maxLength={500} className="mt-1 block w-full rounded-lg bg-black/25 p-3 text-white ring-1 ring-white/10" /></label>}
        <div className="mt-4 flex gap-3"><button disabled={discardBusy || (discardTarget.state !== "discarded" && discardReason.trim().length < 3)} onClick={async () => {
          setDiscardBusy(true);
          try {
            const restoring = discardTarget.state === "discarded";
            await api(`/batch/campaigns/${id}/items/${discardTarget.item_id}/${restoring ? "restore" : "discard"}`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              ...(restoring ? {} : { body: JSON.stringify({ reason: discardReason.trim() }) }),
            });
            setDiscardTarget(null); await load();
          } catch (e) { setError(e.message); }
          finally { setDiscardBusy(false); }
        }} className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">{discardBusy ? "Guardando…" : discardTarget.state === "discarded" ? "Confirmar recuperación" : "Confirmar descarte"}</button><button disabled={discardBusy} onClick={() => setDiscardTarget(null)} className="text-sm text-ink-secondary">Cancelar</button></div>
      </section>}
      <section className="space-y-4 rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between"><div><h2 className="font-semibold text-white">Encontrá una canción y empezá a trabajar</h2><p className="mt-1 text-xs text-ink-tertiary">Primero aparecen las canciones con menos alertas. Todas requieren escuchar y confirmar letra y tiempos.</p></div><div className="flex flex-wrap gap-2"><input value={queueSearch} onChange={(e) => { setQueueSearch(e.target.value); saveContext({ q: e.target.value, focus: null }); }} placeholder="Buscar canción o artista" aria-label="Buscar canción o artista" className="w-56 rounded-lg bg-black/25 px-3 py-2 text-sm text-white ring-1 ring-white/10" /><select value={queueOrder} onChange={(e) => { setQueueOrder(e.target.value); saveContext({ order: e.target.value }); }} aria-label="Orden" className="rounded-lg bg-black/25 px-3 py-2 text-sm text-white ring-1 ring-white/10"><option value="effort">Menos alertas primero</option><option value="learning">Aprendizaje (20%)</option></select><button onClick={() => { const next = !queueMine; setQueueMine(next); saveContext({ mine: next }); }} className={`rounded-lg px-3 py-2 text-sm ring-1 ${queueMine ? "bg-brand/15 text-brand-light ring-brand/30" : "bg-black/25 text-ink-secondary ring-white/10"}`}>Revisadas por mí</button></div></div>
        {(queueSearch || queueMine || queueVersion || queueArtist || queueState) && <div className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary"><span>Filtros activos:</span>{queueSearch && <span className="rounded-full bg-white/[0.07] px-2 py-1">“{queueSearch}”</span>}{queueMine && <span className="rounded-full bg-white/[0.07] px-2 py-1">Revisadas por mí</span>}<button onClick={clearFilters} className="underline hover:text-white">Limpiar</button></div>}
        <div className="flex flex-wrap items-center gap-2 text-xs"><span className="rounded-full bg-white/[0.06] px-2.5 py-1 text-ink-secondary">Sin alertas concretas: {reviewQueue?.classification_counts?.standard || 0}</span><span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-amber-200">Con alertas de tiempos: {reviewQueue?.classification_counts?.timing_targeted || 0}</span><span className="rounded-full bg-red-500/10 px-2.5 py-1 text-red-200">Requieren comprobación completa: {reviewQueue?.classification_counts?.manual_full || 0}</span><span className="text-ink-tertiary">{reviewQueue?.scope?.total || 0} canciones en este alcance</span></div>
        <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-sm"><thead className="border-b border-white/[0.06] text-xs text-ink-tertiary"><tr><th className="p-3">Canción</th><th className="p-3">Duración</th><th className="p-3">Estado</th><th className="p-3">Qué revisar y por qué</th><th className="p-3" /></tr></thead><tbody>{queueRows.map((row) => { const action = reviewActionLabel(row); return <tr key={row.item_id} data-review-job={row.job_id || undefined} className={`border-b border-white/[0.045] ${row.job_id === highlightedJobId ? "bg-brand/10 ring-1 ring-inset ring-brand/30" : ""}`}><td className="p-3"><div className="font-medium text-white">{row.title}</div><div className="text-xs text-ink-tertiary">{row.artist || "Artista no informado"}</div>{row.approval?.user_id && <div className="mt-1 text-xs text-emerald-200">Aprobó {row.approval.name || "otro operador"}{row.approval.at ? ` · ${new Date(row.approval.at).toLocaleString()}` : ""}</div>}</td><td className="p-3 text-ink-secondary">{row.duration_seconds ? timestamp(row.duration_seconds) : "—"}</td><td className="p-3 text-ink-secondary">{reviewStateLabel(row)}</td><td className="p-3"><CampaignReviewWork row={row} timestamp={timestamp} /></td><td className="p-3 text-right">{action ? <button onClick={() => openReview(row)} disabled={queueLoading || (!["approved", "exported"].includes(row.state) && row.reviewer_lock_active && !row.reviewer_is_current_user)} className="rounded-lg bg-brand/15 px-3 py-2 text-xs font-semibold text-brand-light disabled:cursor-not-allowed disabled:bg-white/[0.05] disabled:text-ink-tertiary">{action}</button> : <span className="text-xs text-ink-tertiary">{row.reviewer_lock_active ? "Otra persona está revisando" : "No disponible para revisión"}</span>}{(row.can_discard || row.state === "discarded") && <button disabled={queueLoading || (row.reviewer_lock_active && !row.reviewer_is_current_user)} onClick={() => setDiscardTarget(row)} className="mt-2 block w-full rounded-lg px-3 py-2 text-xs text-ink-secondary hover:bg-white/10 disabled:opacity-40">{row.state === "discarded" ? "Recuperar" : "Descartar"}</button>}</td></tr>; })}</tbody></table>{!queueRows.length && !error && !queueLoading && <div className="p-10 text-center text-sm text-ink-tertiary">{queueSearch || queueMine ? "No hay coincidencias con los filtros actuales." : queueScope === "approved" ? "Todavía no hay canciones aprobadas en este alcance." : "No hay canciones pendientes para revisar."}</div>}</div>
      </section>
      <details className="rounded-2xl bg-surface-2/30 p-4 ring-1 ring-white/[0.06]"><summary className="cursor-pointer text-sm font-semibold text-white">Opciones avanzadas y trazabilidad</summary><div className="mt-4 space-y-4"><div className="flex flex-wrap gap-2"><select value={queueStage} onChange={(e) => { setQueueStage(e.target.value); saveContext({ stage: e.target.value }); }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="lyrics">Letra y timing</option><option value="final">QC final</option></select><select value={queueVersion} onChange={(e) => { setQueueVersion(e.target.value); saveContext({ version: e.target.value }); }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="">Studio + live</option><option value="studio">Studio</option><option value="live">Live</option></select><select value={queueState} aria-label="Estado de revisión" onChange={(e) => {
      const state = e.target.value;
      const scope = ["approved", "exported"].includes(state) ? "approved" : state ? "pending" : queueScope;
      setQueueState(state); setQueueScope(scope);
      saveContext({ state, tab: scope, page: 1 }, true);
    }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="">Todos los estados</option><option value="pending">Pendiente</option><option value="processing">Procesando</option><option value="ready">Sin revisar</option><option value="reviewing">En revisión</option><option value="approved">Aprobada</option><option value="exported">Exportada</option></select><input value={queueArtist} onChange={(e) => { setQueueArtist(e.target.value); saveContext({ artist: e.target.value }); }} placeholder="Filtrar artista" className="w-36 rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10" /></div><CampaignReviewerSummary status={campaign.reviewer_campaign_status} /><p className="text-xs text-ink-tertiary">La recomendación describe alcance de trabajo; no es confianza calibrada. La ausencia de referencia externa no determina por sí sola que la letra sea incorrecta. Los detalles de evidencia quedan disponibles aquí sin interrumpir la cola.</p>{reviewQueue?.review_minutes_today && <p className="text-xs text-ink-tertiary">Actividad secundaria: {reviewQueue.review_minutes_today.average ?? "—"} min promedio hoy en {reviewQueue.review_minutes_today.songs || 0} canciones con telemetría; pausas mayores a 25 s quedan fuera y un único latido suma 15 s. Sin telemetría no entra al promedio.</p>}</div></details>
      <details onToggle={e => setAdminOpen(e.currentTarget.open)} className="rounded-2xl bg-surface-2/30 p-4 ring-1 ring-white/[0.06]">
        <summary className="cursor-pointer text-sm font-semibold text-white">Administración de campaña (fuera de la revisión)</summary>
        <div className="mt-4 space-y-4">
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
                <div className="flex gap-2">{item.metadata_error && <button onClick={() => editMetadata(item)} className="rounded-lg bg-amber-500/10 px-3 py-1.5 text-xs text-amber-100">Completar metadata</button>}{item.job_id && campaign.kind === "art_track" && ["final_review", "done"].includes(item.phase) && <button onClick={() => navigate(`/videos/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Revisar</button>}{item.job_id && campaign.kind !== "art_track" && item.phase === "lyrics_ready" && <button onClick={() => navigate(`/review/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Corregir</button>}{item.phase === "failed" && <button onClick={() => retry(item)} className="rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-200">Reintentar</button>}</div>
              </div>)}
              {!items.length && <div className="p-10 text-center text-sm text-ink-tertiary">No hay items en este filtro.</div>}
            </div>
            <div className="flex justify-end gap-2 border-t border-white/[0.06] p-4"><button disabled={page <= 1} onClick={() => setPage((p) => p - 1)} className="rounded-lg bg-white/[0.06] px-3 py-2 text-xs text-white disabled:opacity-30">Anterior</button><button disabled={page >= pages} onClick={() => setPage((p) => p + 1)} className="rounded-lg bg-white/[0.06] px-3 py-2 text-xs text-white disabled:opacity-30">Siguiente</button></div>
          </section>
        </div>
      </details>
    </div>
  );
}

export default function CampaignsPage() {
  const { campaignId } = useParams();
  return campaignId ? <CampaignDetail key={campaignId} id={campaignId} /> : <CampaignList />;
}
