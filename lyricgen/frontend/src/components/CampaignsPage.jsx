import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
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

function storedCampaignContext(id) {
  try {
    const value = JSON.parse(sessionStorage.getItem(`campaign-review-context:${id}`) || "null");
    return value && typeof value === "object" ? value : {};
  } catch { return {}; }
}

function reviewStateLabel(row) {
  if (row.state === "approved" || row.state === "exported") return "Aprobada";
  if (row.reviewer_lock_active) {
    return row.reviewer_is_current_user ? "En revisión por vos" : `En revisión por ${row.reviewer_name || "otra persona"}`;
  }
  if (row.resume_available) return "En revisión por vos";
  return "Sin revisar";
}

function reviewActionLabel(row) {
  if (row.state === "approved" || row.state === "exported") return "Ver canción";
  if (row.reviewer_lock_active && !row.reviewer_is_current_user) return null;
  return row.resume_available || row.reviewer_is_current_user ? "Continuar" : "Revisar";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const savedContext = storedCampaignContext(id);
  const initialScope = ["pending", "approved", "all"].includes(searchParams.get("tab"))
    ? searchParams.get("tab") : (["pending", "approved", "all"].includes(savedContext.tab) ? savedContext.tab : "pending");
  const initialOrder = searchParams.get("order") === "learning" ? "learning" : (savedContext.order === "learning" ? "learning" : "effort");
  const [campaign, setCampaign] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState(() => searchParams.get("phase") || savedContext.phase || "");
  const [page, setPage] = useState(() => Number(searchParams.get("page") || savedContext.page || 1) || 1);
  const [pages, setPages] = useState(1);
  const [pair, setPair] = useState(null);
  const [error, setError] = useState("");
  const [presetText, setPresetText] = useState("");
  const [queueStage, setQueueStage] = useState(() => searchParams.get("stage") || savedContext.stage || "lyrics");
  const [queueOrder, setQueueOrder] = useState(initialOrder);
  const [queueVersion, setQueueVersion] = useState(() => searchParams.get("version") || savedContext.version || "");
  const [queueState, setQueueState] = useState(() => searchParams.get("state") || savedContext.state || "");
  const [queueScope, setQueueScope] = useState(initialScope);
  const [queueBackground, setQueueBackground] = useState("");
  const [queueArtist, setQueueArtist] = useState(() => searchParams.get("artist") || savedContext.artist || "");
  const [queueSearch, setQueueSearch] = useState(() => searchParams.get("q") || savedContext.q || "");
  const [queueMine, setQueueMine] = useState(() => searchParams.get("mine") === "1" || savedContext.mine === true);
  const [queueAudit, setQueueAudit] = useState(false);
  const [reviewQueue, setReviewQueue] = useState(null);
  const [highlightedJobId, setHighlightedJobId] = useState(() => searchParams.get("focus") || savedContext.focus || null);
  const restoredScrollRef = useRef(null);

  const saveContext = useCallback((overrides = {}) => {
    const values = {
      tab: queueScope, q: queueSearch, mine: queueMine, order: queueOrder,
      page, ...overrides,
    };
    const next = new URLSearchParams(searchParams);
    ["tab", "q", "mine", "order", "page", "stage", "version", "state", "artist", "phase", "focus", "scroll"].forEach((key) => next.delete(key));
    const path = contextPath(id, values);
    const query = path.split("?")[1];
    if (query) new URLSearchParams(query).forEach((value, key) => next.set(key, value));
    setSearchParams(next, { replace: true });
    try { sessionStorage.setItem(`campaign-review-context:${id}`, JSON.stringify(values)); } catch { /* best effort */ }
  }, [id, page, queueMine, queueOrder, queueScope, queueSearch, searchParams, setSearchParams]);

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
  }, [id, navigate, page, queueMine, queueOrder, queueScope, queueSearch]);

  const load = useCallback(async () => {
    try {
      const queueQuery = `stage=${queueStage}&order=${queueOrder}&scope=${queueScope}&limit=100${queueVersion ? `&version=${queueVersion}` : ""}${queueState ? `&state=${encodeURIComponent(queueState)}` : ""}${queueStage === "final" && queueBackground ? `&background_mode=${encodeURIComponent(queueBackground)}` : ""}${queueArtist ? `&artist=${encodeURIComponent(queueArtist)}` : ""}${queueSearch ? `&search=${encodeURIComponent(queueSearch)}` : ""}${queueMine ? "&reviewed_by=me" : ""}${queueAudit ? "&audit_preapproved=true" : ""}`;
      const [head, rows, firstReview] = await Promise.all([
        api(`/batch/campaigns/${id}`),
        api(`/batch/campaigns/${id}/items?page=${page}&limit=50${phase ? `&phase=${phase}` : ""}`),
        api(`/batch/campaigns/${id}/review-queue?${queueQuery}`),
      ]);
      const remainingReviews = await Promise.all(
        Array.from({ length: Math.max(0, Number(firstReview.pages || 1) - 1) }, (_, index) => (
          api(`/batch/campaigns/${id}/review-queue?${queueQuery}&page=${index + 2}`)
        )),
      );
      const review = {
        ...firstReview,
        items: [firstReview.items || [], ...remainingReviews.map((pageData) => pageData.items || [])].flat(),
      };
      setCampaign(head); setItems(rows.items || []); setPages(rows.pages || 1);
      setReviewQueue(review);
      setPresetText((current) => current || JSON.stringify(head.default_render_params || {}, null, 2));
      setError("");
    } catch (e) { setError(e.message); }
  }, [id, page, phase, queueStage, queueOrder, queueScope, queueVersion, queueState, queueBackground, queueArtist, queueSearch, queueMine, queueAudit]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void load();
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
    const timer = window.setInterval(load, 10_000);
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

  const labels = useMemo(() => Object.fromEntries(PHASES), []);
  if (!campaign) return <div className="p-8 text-sm text-ink-secondary">{error || "Cargando campaña…"}</div>;
  const selectedRow = (reviewQueue?.items || []).find((row) => row.job_id === highlightedJobId);
  const approvedCount = reviewQueue?.campaign_totals?.approved ?? 0;
  const songCount = reviewQueue?.campaign_totals?.songs ?? campaign.registered_count ?? 0;
  const clearFilters = () => {
    setQueueSearch(""); setQueueArtist(""); setQueueVersion(""); setQueueState(""); setQueueMine(false);
    setHighlightedJobId(null); setSearchParams(new URLSearchParams(), { replace: true });
    try { sessionStorage.setItem(`campaign-review-context:${id}`, JSON.stringify({ tab: queueScope, order: queueOrder, page })); } catch { /* best effort */ }
  };
  const queueRows = reviewQueue?.items || [];
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <button onClick={() => navigate("/campaigns")} className="text-sm text-ink-secondary hover:text-white">← Campañas</button>
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0"><h1 className="truncate text-3xl font-bold text-white">{campaign.name}</h1><p className="mt-2 text-sm text-ink-secondary">{approvedCount} de {songCount} letras aprobadas · {campaign.status === "active" ? "campaña activa" : campaign.status}</p></div>
        <button onClick={() => takeNext(queueStage)} disabled={queueScope !== "pending" || !reviewQueue?.counters?.ready} className="rounded-xl bg-brand px-5 py-3 text-sm font-semibold text-white disabled:opacity-40">Revisar siguiente canción</button>
      </div>
      {selectedRow?.resume_available && <div className="flex items-center justify-between gap-3 rounded-xl bg-brand/10 p-4 text-sm text-brand-light ring-1 ring-brand/25"><span>Tenés una revisión guardada: <strong>{selectedRow.title}</strong>.</span><button onClick={() => openReview(selectedRow)} className="rounded-lg bg-brand px-3 py-2 text-xs font-semibold text-white">Continuar {selectedRow.title}</button></div>}
      {highlightedJobId && !selectedRow && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25"><span>La canción recién abierta ya no coincide con este filtro. Puede haber cambiado de estado o estar aprobada en otra pestaña.</span><button onClick={() => { setQueueScope("approved"); saveContext({ tab: "approved", focus: highlightedJobId }); }} className="rounded-lg bg-amber-500/20 px-3 py-2 text-xs font-semibold">Buscar en Aprobadas</button></div>}
      {error && <div className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/25">{error}</div>}
      <nav aria-label="Canciones de la campaña" className="flex flex-wrap gap-2 border-b border-white/[0.08]">
        {[['pending', 'Por revisar'], ['approved', 'Aprobadas'], ['all', 'Todas']].map(([key, label]) => <button key={key} onClick={() => { setQueueScope(key); saveContext({ tab: key, focus: null, scroll: null }); }} className={`border-b-2 px-4 py-3 text-sm font-semibold ${queueScope === key ? "border-brand text-white" : "border-transparent text-ink-tertiary hover:text-white"}`}>{label} <span className="ml-1 text-xs text-ink-tertiary">{key === "pending" ? (reviewQueue?.scope?.total || 0) : key === "approved" ? approvedCount : songCount}</span></button>)}
      </nav>
      <section className="space-y-4 rounded-2xl bg-surface-2/40 p-5 ring-1 ring-white/[0.06]">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between"><div><h2 className="font-semibold text-white">Encontrá una canción y empezá a trabajar</h2><p className="mt-1 text-xs text-ink-tertiary">El orden predeterminado prioriza el menor esfuerzo estimado según la evidencia disponible.</p></div><div className="flex flex-wrap gap-2"><input value={queueSearch} onChange={(e) => { setQueueSearch(e.target.value); saveContext({ q: e.target.value, focus: null }); }} placeholder="Buscar canción o artista" aria-label="Buscar canción o artista" className="w-56 rounded-lg bg-black/25 px-3 py-2 text-sm text-white ring-1 ring-white/10" /><select value={queueOrder} onChange={(e) => { setQueueOrder(e.target.value); saveContext({ order: e.target.value }); }} aria-label="Orden" className="rounded-lg bg-black/25 px-3 py-2 text-sm text-white ring-1 ring-white/10"><option value="effort">Menor esfuerzo estimado primero</option><option value="learning">Aprendizaje (20%)</option></select><button onClick={() => { const next = !queueMine; setQueueMine(next); saveContext({ mine: next }); }} className={`rounded-lg px-3 py-2 text-sm ring-1 ${queueMine ? "bg-brand/15 text-brand-light ring-brand/30" : "bg-black/25 text-ink-secondary ring-white/10"}`}>Revisadas por mí</button></div></div>
        {(queueSearch || queueMine || queueVersion || queueArtist || queueState) && <div className="flex flex-wrap items-center gap-2 text-xs text-ink-secondary"><span>Filtros activos:</span>{queueSearch && <span className="rounded-full bg-white/[0.07] px-2 py-1">“{queueSearch}”</span>}{queueMine && <span className="rounded-full bg-white/[0.07] px-2 py-1">Revisadas por mí</span>}<button onClick={clearFilters} className="underline hover:text-white">Limpiar</button></div>}
        <div className="flex flex-wrap items-center gap-2 text-xs"><span className="rounded-full bg-white/[0.06] px-2.5 py-1 text-ink-secondary">Revisión breve sugerida: {reviewQueue?.classification_counts?.standard || 0}</span><span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-amber-200">Revisar algunos fragmentos: {reviewQueue?.classification_counts?.timing_targeted || 0}</span><span className="rounded-full bg-red-500/10 px-2.5 py-1 text-red-200">Revisar completa: {reviewQueue?.classification_counts?.manual_full || 0}</span><span className="text-ink-tertiary">{reviewQueue?.scope?.total || 0} canciones en este alcance</span></div>
        <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-sm"><thead className="border-b border-white/[0.06] text-xs text-ink-tertiary"><tr><th className="p-3">Canción</th><th className="p-3">Duración</th><th className="p-3">Estado</th><th className="p-3">Trabajo sugerido</th><th className="p-3" /></tr></thead><tbody>{queueRows.map((row) => { const action = reviewActionLabel(row); const reasons = [...new Map((row.review_reasons || []).map((reason) => [reason.code, reason])).values()]; return <tr key={row.item_id} data-review-job={row.job_id || undefined} className={`border-b border-white/[0.045] ${row.job_id === highlightedJobId ? "bg-brand/10 ring-1 ring-inset ring-brand/30" : ""}`}><td className="p-3"><div className="font-medium text-white">{row.title}</div><div className="text-xs text-ink-tertiary">{row.artist || "Artista no informado"}</div>{row.approval?.user_id && <div className="mt-1 text-xs text-emerald-200">Aprobó {row.approval.name || "otro operador"}{row.approval.at ? ` · ${new Date(row.approval.at).toLocaleString()}` : ""}</div>}</td><td className="p-3 text-ink-secondary">{row.duration_seconds ? timestamp(row.duration_seconds) : "—"}</td><td className="p-3 text-ink-secondary">{reviewStateLabel(row)}</td><td className="p-3"><div className="text-xs font-medium text-white">{row.review_priority_label || "Revisión breve sugerida"}</div>{reasons.length > 0 && <div className="mt-1 text-xs text-ink-secondary">{reasons.map((reason) => reason.label).join(" · ")}</div>}{row.timing_evidence?.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{row.timing_evidence.slice(0, 4).map((window) => <span key={window.id} className="rounded bg-cyan-400/10 px-1.5 py-0.5 text-[11px] text-cyan-200">{timestamp(window.start)}–{timestamp(window.end)}{window.reasons?.length ? ` · ${window.reasons.join(", ")}` : ""}</span>)}</div>}{row.timing_localization === "general" && <div className="mt-1 text-xs text-amber-200">Revisar timing; la duda no está localizada en un intervalo.</div>}</td><td className="p-3 text-right">{action ? <button onClick={() => openReview(row)} disabled={row.reviewer_lock_active && !row.reviewer_is_current_user} className="rounded-lg bg-brand/15 px-3 py-2 text-xs font-semibold text-brand-light disabled:cursor-not-allowed disabled:bg-white/[0.05] disabled:text-ink-tertiary">{action}</button> : <span className="text-xs text-ink-tertiary">Otra persona está revisando</span>}</td></tr>; })}</tbody></table>{!queueRows.length && !error && <div className="p-10 text-center text-sm text-ink-tertiary">{queueSearch || queueMine ? "No hay coincidencias con los filtros actuales." : queueScope === "approved" ? "Todavía no hay canciones aprobadas en este alcance." : "No hay canciones pendientes para revisar."}</div>}</div>
      </section>
      <details className="rounded-2xl bg-surface-2/30 p-4 ring-1 ring-white/[0.06]"><summary className="cursor-pointer text-sm font-semibold text-white">Opciones avanzadas y trazabilidad</summary><div className="mt-4 space-y-4"><div className="flex flex-wrap gap-2"><select value={queueStage} onChange={(e) => { setQueueStage(e.target.value); saveContext({ stage: e.target.value }); }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="lyrics">Letra y timing</option><option value="final">QC final</option></select><select value={queueVersion} onChange={(e) => { setQueueVersion(e.target.value); saveContext({ version: e.target.value }); }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="">Studio + live</option><option value="studio">Studio</option><option value="live">Live</option></select><select value={queueState} onChange={(e) => { setQueueState(e.target.value); saveContext({ state: e.target.value }); }} className="rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10"><option value="">Todos los estados</option><option value="pending">Pendiente</option><option value="processing">Procesando</option><option value="ready">Sin revisar</option><option value="reviewing">En revisión</option><option value="approved">Aprobada</option><option value="exported">Exportada</option></select><input value={queueArtist} onChange={(e) => { setQueueArtist(e.target.value); saveContext({ artist: e.target.value }); }} placeholder="Filtrar artista" className="w-36 rounded-lg bg-black/25 px-3 py-2 text-xs text-white ring-1 ring-white/10" /></div><CampaignReviewerSummary status={campaign.reviewer_campaign_status} /><p className="text-xs text-ink-tertiary">La recomendación describe alcance de trabajo; no es confianza calibrada. La ausencia de referencia externa no determina por sí sola que la letra sea incorrecta. Los detalles de evidencia quedan disponibles aquí sin interrumpir la cola.</p>{reviewQueue?.review_minutes_today && <p className="text-xs text-ink-tertiary">Actividad secundaria: {reviewQueue.review_minutes_today.average ?? "—"} min promedio hoy en {reviewQueue.review_minutes_today.songs || 0} canciones con telemetría; pausas mayores a 25 s quedan fuera y un único latido suma 15 s. Sin telemetría no entra al promedio.</p>}</div></details>
      <details className="rounded-2xl bg-surface-2/30 p-4 ring-1 ring-white/[0.06]">
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
                <div className="flex gap-2">{item.metadata_error && <button onClick={() => editMetadata(item)} className="rounded-lg bg-amber-500/10 px-3 py-1.5 text-xs text-amber-100">Completar metadata</button>}{item.job_id && item.phase === "lyrics_ready" && <button onClick={() => navigate(`/review/${item.job_id}`)} className="rounded-lg bg-brand/15 px-3 py-1.5 text-xs text-brand-light">Corregir</button>}{item.phase === "failed" && <button onClick={() => retry(item)} className="rounded-lg bg-red-500/10 px-3 py-1.5 text-xs text-red-200">Reintentar</button>}</div>
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
  return campaignId ? <CampaignDetail id={campaignId} /> : <CampaignList />;
}
