import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

const API = import.meta.env.VITE_API_URL || "";

const STATE_LABELS = {
  pending: "Pendiente",
  processing: "Procesando",
  ready: "Sin revisar",
  reviewing: "En revisión",
  approved: "Aprobada",
  failed: "Fallida",
};

async function api(path, options = {}) {
  const token = localStorage.getItem("genly_token");
  const response = await fetch(`${API}${path}`, {
    ...options,
    // Approval mutates the queue while this page is often restored from the
    // browser back-forward cache. Never reuse a stale campaign snapshot.
    cache: options.cache || "no-store",
    headers: {
      ...(options.headers || {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(
    typeof body.detail === "string" ? body.detail : body.detail?.code || `Error ${response.status}`,
  );
  return body;
}

function duration(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Math.round(Number(seconds)));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function timestamp(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

function exportMinutes(rows) {
  const cells = [["orden", "artista", "titulo", "estado", "minutos_activos"]];
  rows.forEach((row) => cells.push([
    row.priority, row.artist, row.title, row.state, Number(row.active_minutes || 0).toFixed(2),
  ]));
  const csv = cells.map((line) => line.map((value) => (
    `"${String(value ?? "").replaceAll('"', '""')}"`
  )).join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `minutos-revision-${new Date().toISOString().slice(0, 10)}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function ReviewQueuePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [campaign, setCampaign] = useState(null);
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [scope, setScope] = useState(() => (
    ["pending", "approved", "all"].includes(searchParams.get("scope"))
      ? searchParams.get("scope") : "pending"
  ));

  const load = useCallback(async () => {
    try {
      const campaigns = await api("/batch/campaigns");
      const current = (campaigns.items || []).find((row) => row.status === "active")
        || (campaigns.items || [])[0];
      if (!current) throw new Error("No hay una campaña activa.");
      const firstPage = await api(
        `/batch/campaigns/${current.id}/review-queue?stage=lyrics&order=effort&scope=${scope}&limit=100`,
      );
      const remainingPages = await Promise.all(
        Array.from({ length: Math.max(0, Number(firstPage.pages || 1) - 1) }, (_, index) => (
          api(`/batch/campaigns/${current.id}/review-queue?stage=lyrics&order=effort&scope=${scope}&limit=100&page=${index + 2}`)
        )),
      );
      const data = {
        ...firstPage,
        items: [
          ...(firstPage.items || []),
          ...remainingPages.flatMap((page) => page.items || []),
        ],
      };
      setCampaign(current);
      setQueue(data);
      setError("");
    } catch (nextError) {
      setError(nextError.message || "No pudimos cargar la cola.");
    } finally {
      setLoading(false);
    }
  }, [scope]);

  useEffect(() => { void load(); }, [load]);
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
    const timer = window.setInterval(load, 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const rows = queue?.items || [];
  const highlightedJobId = useMemo(() => {
    const approved = searchParams.get("approved");
    const approvedIndex = rows.findIndex((row) => row.job_id === approved);
    const tail = approvedIndex >= 0 ? rows.slice(approvedIndex + 1) : rows;
    return (tail.find((row) => row.state === "ready")
      || rows.find((row) => row.state === "ready"))?.job_id || null;
  }, [rows, searchParams]);
  const pending = queue?.scope?.total || 0;
  const open = (row) => row.job_id && navigate(`/review/${row.job_id}`);
  const next = rows.find((row) => row.job_id === highlightedJobId);

  if (loading) return <div className="mx-auto max-w-6xl p-8 text-sm text-ink-secondary">Cargando cola…</div>;
  return (
    <div className="mx-auto max-w-7xl space-y-5" data-testid="review-queue-page">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[.18em] text-brand">Letra y timing · etapa 1</p>
          <h1 className="mt-2 text-3xl font-bold text-white">Cola de revisión</h1>
          <p className="mt-2 text-sm text-ink-secondary">{campaign?.name || "Campaña actual"} · guardado automático activo</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={() => next && open(next)} disabled={!next || scope !== "pending"} className="rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white disabled:opacity-40">Siguiente</button>
          <select value={scope} onChange={(event) => { setScope(event.target.value); setLoading(true); }} aria-label="Alcance de la cola" className="rounded-xl bg-white/[0.07] px-3 py-2.5 text-sm text-white">
            <option value="pending">Pendientes</option>
            <option value="approved">Aprobadas</option>
            <option value="all">Toda la campaña</option>
          </select>
          <button onClick={() => exportMinutes(rows)} className="rounded-xl bg-white/[0.07] px-4 py-2.5 text-sm text-white">Exportar minutos</button>
        </div>
      </div>

      {error && <div role="alert" className="rounded-xl bg-red-500/10 p-4 text-sm text-red-200 ring-1 ring-red-500/30">{error} <button onClick={load} className="ml-2 underline">Reintentar</button></div>}
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-2xl bg-surface-2/50 p-4 ring-1 ring-white/[0.06]"><div className="text-2xl font-bold text-white">{pending}</div><div className="text-xs text-ink-tertiary">Pendientes</div></div>
        <div className="rounded-2xl bg-surface-2/50 p-4 ring-1 ring-white/[0.06]"><div className="text-2xl font-bold text-white">{queue?.campaign_totals?.approved || 0}</div><div className="text-xs text-ink-tertiary">Aprobadas en campaña</div></div>
        <div className="rounded-2xl bg-surface-2/50 p-4 ring-1 ring-white/[0.06]"><div className="text-2xl font-bold text-white">{queue?.review_minutes_today?.average ?? "—"}</div><div className="text-xs text-ink-tertiary">Minutos promedio hoy</div></div>
      </div>
      <p className="text-xs text-ink-secondary">Alcance: <strong>{queue?.scope?.label || "Pendientes"}</strong> · {pending} canciones. Aprobadas hoy: {queue?.campaign_totals?.approved_today || 0}. Las categorías y “Siguiente” usan este mismo conjunto; las aprobadas siguen accesibles en su filtro.</p>

      <div className="mb-2 flex flex-wrap gap-2 text-xs">
        <span className="rounded-full bg-red-500/10 px-2.5 py-1 text-red-200">Extensa: {queue?.classification_counts?.manual_full || 0}</span>
        <span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-amber-200">Focalizada: {queue?.classification_counts?.timing_targeted || 0}</span>
        <span className="rounded-full bg-white/[0.06] px-2.5 py-1 text-ink-secondary">Breve sugerida: {queue?.classification_counts?.standard || 0}</span>
      </div>
      <div className="overflow-x-auto rounded-2xl bg-surface-2/40 ring-1 ring-white/[0.06]">
        <table className="w-full min-w-[850px] text-left text-sm">
          <thead className="border-b border-white/[0.06] text-xs text-ink-tertiary"><tr><th className="p-4">Prioridad operativa</th><th className="p-4">Artista</th><th className="p-4">Título</th><th className="p-4">Estudio / vivo</th><th className="p-4">Duración</th><th className="p-4">Estado</th><th className="p-4">Tiempo acumulado</th><th className="p-4" /></tr></thead>
          <tbody>{rows.map((row) => {
            const highlighted = row.job_id === highlightedJobId;
            return <tr key={row.item_id} className={`border-b border-white/[0.045] ${highlighted ? "bg-brand/10 ring-1 ring-inset ring-brand/30" : ""}`}>
              <td className="p-4"><div className="font-semibold text-white">{row.priority} · {row.review_priority_label || "Revisión breve sugerida"}</div><div className="mt-1 text-[11px] text-ink-tertiary">Texto: {row.review_domains?.text?.status === "manual_full" ? "manual" : row.review_domains?.text?.status || "—"} · Timing: {row.review_domains?.timing?.status === "targeted" ? "dirigido" : row.review_domains?.timing?.status || "—"}</div>{!!row.review_reasons?.length && <div className="mt-1 text-[11px] text-amber-200">{[...new Map(row.review_reasons.map((reason) => [reason.code, reason])).values()].map((reason) => reason.label).join(" · ")}</div>}{!!row.timing_evidence?.length && <div className="mt-1 flex flex-wrap gap-1 text-[11px] text-cyan-200">{row.timing_evidence.slice(0, 4).map((window) => <span key={window.id} className="rounded bg-cyan-400/10 px-1.5 py-0.5">{timestamp(window.start)}–{timestamp(window.end)}{window.reasons?.length ? ` · ${window.reasons.join(", ")}` : ""}</span>)}</div>}</td><td className="p-4 text-ink-secondary">{row.artist || "—"}</td><td className="p-4 font-medium text-white">{row.title}</td><td className="p-4 text-ink-secondary">{row.version === "live" ? "Vivo" : "Estudio"}</td><td className="p-4 text-ink-secondary">{duration(row.duration_seconds)}</td><td className="p-4 text-ink-secondary">{STATE_LABELS[row.state] || row.state}{row.reviewer_name ? ` por ${row.reviewer_name}` : ""}</td><td className="p-4 tabular-nums text-ink-secondary">{row.active_minutes_observed ? `${Number(row.active_minutes || 0).toFixed(2)} min` : "sin telemetría"}</td><td className="p-4"><button onClick={() => open(row)} disabled={!row.job_id || !["ready", "reviewing", "approved", "exported"].includes(row.state)} className="min-w-28 rounded-xl bg-brand px-5 py-3 text-sm font-semibold text-white disabled:bg-white/[0.06] disabled:text-ink-tertiary">{scope === "approved" ? "Abrir" : "Revisar"}</button></td>
            </tr>;
          })}</tbody>
        </table>
        {!rows.length && !error && <div className="p-10 text-center text-sm text-ink-tertiary">No hay canciones en la campaña actual.</div>}
      </div>
      <p className="text-xs text-ink-tertiary">La prioridad operativa usa reglas explícitas de alcance de trabajo; no es confianza calibrada ni automatiza aprobaciones. “Sin telemetría” queda fuera del cálculo de minutos. Esta pantalla no genera fondos ni renders.</p>
    </div>
  );
}
