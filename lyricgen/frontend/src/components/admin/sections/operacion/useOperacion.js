// Hook dueño de TODA la data de la sección Operación.
//
// Acá viven los tres flujos de triaje del operador:
//   - health + stuck-jobs (refresh 15 s, pausado si la pestaña está oculta)
//   - jobs en vivo (refresh 5 s cuando auto-refresh está on)
//   - change requests de UMG (sin auto-refresh, recarga manual / al cambiar filtro)
//
// Todo lo de red pasa por fetchJson (un solo lugar con auth + res.ok). Las
// mutaciones reportan error vía useAdmin().flashError, igual que el admin viejo.
import { useCallback, useEffect, useRef, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

const STUCK_THRESHOLD_MIN = 100;

export default function useOperacion() {
  const { flashError } = useAdmin();

  // --- Health + stuck jobs (15 s) ------------------------------------------
  const [health, setHealth] = useState(null);
  const [stuckJobs, setStuckJobs] = useState({ count: 0, jobs: [] });
  const [reaperRunning, setReaperRunning] = useState(false);
  const [reaperResult, setReaperResult] = useState(null);

  const loadHealth = useCallback(async () => {
    try {
      // /health es público — NO mandar auth headers.
      setHealth(await fetchJson(`${API}/health`));
    } catch {
      setHealth({ status: "error", _fetch_failed: true });
    }
  }, []);

  const loadStuckJobs = useCallback(async () => {
    try {
      setStuckJobs(await fetchJson(`${API}/admin/stuck-jobs?threshold_min=${STUCK_THRESHOLD_MIN}`));
    } catch {
      // Best-effort: si falla dejamos el último conteo conocido.
    }
  }, []);

  const runReaperNow = useCallback(async () => {
    if (reaperRunning) return;
    setReaperRunning(true);
    setReaperResult(null);
    try {
      const data = await fetchJson(
        `${API}/admin/runbook/reaper-now?threshold_min=${STUCK_THRESHOLD_MIN}`,
        { method: "POST" },
      );
      setReaperResult(data);
      // Refrescamos el conteo enseguida para que el banner refleje la realidad.
      loadStuckJobs();
    } catch (err) {
      setReaperResult({ error: String(err.message || err) });
    } finally {
      setReaperRunning(false);
    }
  }, [reaperRunning, loadStuckJobs]);

  useEffect(() => {
    loadHealth();
    loadStuckJobs();
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadHealth();
      loadStuckJobs();
    }, 15000);
    return () => clearInterval(iv);
  }, [loadHealth, loadStuckJobs]);

  // --- Jobs en vivo (5 s) ---------------------------------------------------
  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsTenantFilter, setJobsTenantFilter] = useState("");
  const [jobsAutoRefresh, setJobsAutoRefresh] = useState(true);
  const [jobsLoading, setJobsLoading] = useState(true);

  // Refs para que el intervalo lea los filtros vigentes sin re-crearse.
  const jobsStatusRef = useRef(jobsStatusFilter);
  const jobsTenantRef = useRef(jobsTenantFilter);
  jobsStatusRef.current = jobsStatusFilter;
  jobsTenantRef.current = jobsTenantFilter;

  const loadJobs = useCallback(async () => {
    try {
      const tenant = jobsTenantRef.current
        ? `&tenant_id=${encodeURIComponent(jobsTenantRef.current)}` : "";
      const status = jobsStatusRef.current
        ? `&status=${encodeURIComponent(jobsStatusRef.current)}` : "";
      const data = await fetchJson(`${API}/admin/jobs?limit=200${tenant}${status}`);
      setJobs(data.jobs || []);
      setJobsTotal(data.total || 0);
    } catch {
      // Best-effort: el auto-refresh reintentará en 5 s.
    } finally {
      setJobsLoading(false);
    }
  }, []);

  // Carga inmediata al montar y cada vez que cambian los filtros.
  useEffect(() => {
    setJobsLoading(true);
    loadJobs();
  }, [jobsStatusFilter, jobsTenantFilter, loadJobs]);

  // Auto-refresh cada 5 s, pausado si la pestaña está oculta.
  useEffect(() => {
    if (!jobsAutoRefresh) return;
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadJobs();
    }, 5000);
    return () => clearInterval(iv);
  }, [jobsAutoRefresh, loadJobs]);

  // --- Change requests de UMG ----------------------------------------------
  const [changeRequests, setChangeRequests] = useState([]);
  const [crStatusFilter, setCrStatusFilter] = useState("pending");
  const [crPendingCount, setCrPendingCount] = useState(0);
  const [crResolvedCount, setCrResolvedCount] = useState(0);
  const [crLoading, setCrLoading] = useState(true);
  const [crResolvingId, setCrResolvingId] = useState(null);

  const crStatusRef = useRef(crStatusFilter);
  crStatusRef.current = crStatusFilter;

  const loadChangeRequests = useCallback(async () => {
    setCrLoading(true);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests?status=${crStatusRef.current}&limit=200`,
      );
      setChangeRequests(data.items || []);
      setCrPendingCount(data.pending_count || 0);
      setCrResolvedCount(data.resolved_count || 0);
    } catch (err) {
      flashError(`No pude cargar los cambios: ${err.message || err}`);
    } finally {
      setCrLoading(false);
    }
  }, [flashError]);

  useEffect(() => {
    loadChangeRequests();
  }, [crStatusFilter, loadChangeRequests]);

  const resolveChangeRequest = useCallback(async (id, note) => {
    setCrResolvingId(id);
    try {
      await fetchJson(`${API}/admin/change-requests/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_note: (note || "").trim() }),
      });
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude marcar como resuelto: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  }, [flashError, loadChangeRequests]);

  const reopenChangeRequest = useCallback(async (id) => {
    setCrResolvingId(id);
    try {
      await fetchJson(`${API}/admin/change-requests/${id}/reopen`, { method: "POST" });
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude reabrir: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  }, [flashError, loadChangeRequests]);

  return {
    // health + reaper
    health,
    stuckJobs,
    reaperRunning,
    reaperResult,
    setReaperResult,
    runReaperNow,
    // jobs
    jobs,
    jobsTotal,
    jobsLoading,
    jobsStatusFilter,
    setJobsStatusFilter,
    jobsTenantFilter,
    setJobsTenantFilter,
    jobsAutoRefresh,
    setJobsAutoRefresh,
    // change requests
    changeRequests,
    crStatusFilter,
    setCrStatusFilter,
    crPendingCount,
    crResolvedCount,
    crLoading,
    crResolvingId,
    resolveChangeRequest,
    reopenChangeRequest,
  };
}
