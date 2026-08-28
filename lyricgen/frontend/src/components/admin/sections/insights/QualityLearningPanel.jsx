import { useCallback, useEffect, useMemo, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import KpiCard from "../../primitives/KpiCard";
import StatusBadge from "../../primitives/StatusBadge";

const STATUS = {
  emerging: { label: "Emergente", classes: "bg-gray-500/10 text-gray-300 ring-gray-500/20" },
  stale: { label: "Sin soporte actual", classes: "bg-gray-500/10 text-gray-400 ring-gray-500/20" },
  qualified: { label: "Asociado", classes: "bg-amber-500/10 text-amber-300 ring-amber-500/20" },
  correlated: { label: "Correlacionado", classes: "bg-amber-500/10 text-amber-300 ring-amber-500/20" },
  confirmed: { label: "Causa confirmada", classes: "bg-accent/10 text-accent ring-accent/20" },
  draft: { label: "Borrador", classes: "bg-gray-500/10 text-gray-300 ring-gray-500/20" },
  validating: { label: "Validando", classes: "bg-brand/10 text-brand-light ring-brand/20" },
  ready: { label: "Validado", classes: "bg-accent/10 text-accent ring-accent/20" },
  approved: { label: "Listo para implementar", classes: "bg-accent/10 text-accent ring-accent/20" },
  failed: { label: "No pasó", classes: "bg-red-500/10 text-red-300 ring-red-500/20" },
  blocked: { label: "Bloqueado", classes: "bg-amber-500/10 text-amber-300 ring-amber-500/20" },
  rejected: { label: "Rechazado", classes: "bg-red-500/10 text-red-300 ring-red-500/20" },
  superseded: { label: "Superado", classes: "bg-gray-500/10 text-gray-400 ring-gray-500/20" },
};

function actionKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `quality-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function MetricBreakdown({ title, rows = {} }) {
  const entries = Object.entries(rows);
  if (!entries.length) return null;
  return (
    <div className="rounded-card bg-white/[0.025] ring-1 ring-white/[0.06] p-3">
      <h4 className="text-caption font-medium text-gray-300 mb-2">{title}</h4>
      <div className="space-y-1.5">
        {entries.map(([name, value]) => (
          <div key={name} className="grid grid-cols-[1fr_auto_auto] gap-3 text-label text-gray-400">
            <span className="truncate" title={name}>{name}</span>
            <span>{value.observations} canciones</span>
            <span>p50/p90 {value.operator_minutes_p50 ?? "—"}/{value.operator_minutes_p90 ?? "—"}m</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function QualityLearningPanel() {
  const [summary, setSummary] = useState(null);
  const [patterns, setPatterns] = useState([]);
  const [proposals, setProposals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [working, setWorking] = useState("");
  const [patternStatus, setPatternStatus] = useState("");
  const [proposalStatus, setProposalStatus] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [summaryPayload, patternPayload, proposalPayload] = await Promise.all([
        fetchJson(`${API}/admin/quality-learning/summary?days=90`),
        fetchJson(`${API}/admin/quality-learning/patterns?limit=100`),
        fetchJson(`${API}/admin/quality-learning/proposals?limit=100`),
      ]);
      setSummary(summaryPayload);
      setPatterns(patternPayload.patterns || []);
      setProposals(proposalPayload.proposals || []);
    } catch (err) {
      setError(err.message || "No se pudo cargar aprendizaje");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const mutate = useCallback(async (proposal, action) => {
    const reason = window.prompt(
      action === "validate"
        ? "Motivo y objetivo de esta validación"
        : action === "approve"
          ? "Motivo para aprobar como listo para implementación"
          : "Motivo del rechazo",
    );
    if (!reason?.trim()) return;
    setWorking(`${proposal.id}:${action}`);
    setError("");
    try {
      await fetchJson(`${API}/admin/quality-learning/proposals/${proposal.id}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason: reason.trim(),
          expected_version: proposal.version,
          idempotency_key: actionKey(),
        }),
      });
      await refresh();
    } catch (err) {
      setError(err.message || "La acción fue rechazada");
    } finally {
      setWorking("");
    }
  }, [refresh]);

  const patternColumns = useMemo(() => [
    { key: "category", header: "Problema" },
    { key: "context_key", header: "Factor asociado", render: (row) => <code className="text-label text-gray-300">{row.context_key}</code> },
    { key: "support_jobs", header: "Canciones", align: "right" },
    { key: "support_tenants", header: "Tenants", align: "right" },
    { key: "support_artists", header: "Artistas", align: "right" },
    { key: "relative_risk", header: "Riesgo", align: "right", render: (row) => `${Number(row.relative_risk || 0).toFixed(2)}×` },
    { key: "impact_seconds", header: "Impacto", align: "right", render: (row) => `${Math.round(row.impact_seconds || 0)} s` },
    { key: "status", header: "Estado", render: (row) => <StatusBadge status={row.status} map={STATUS} /> },
  ], []);

  const proposalColumns = useMemo(() => [
    { key: "title", header: "Propuesta", render: (row) => (
      <div className="max-w-md"><span className="text-white font-medium">{row.title}</span><span className="block text-label text-gray-500 mt-0.5">{row.hypothesis}</span></div>
    ) },
    { key: "candidate_config", header: "Una variable", render: (row) => <code className="text-label text-gray-300">{JSON.stringify(row.candidate_config)}</code> },
    { key: "validation", header: "Ablation / costo", render: (row) => {
      const validation = row.validation_summary || {};
      const metrics = validation.metrics || {};
      if (!Object.keys(metrics).length) {
        const seconds = row.expected_impact?.observed_impact_seconds;
        return <span className="text-label text-gray-500">{seconds == null ? "Pendiente" : `Hasta ${Math.round(seconds)} s observados`}</span>;
      }
      return (
        <span className="text-label text-gray-300">
          {`${Math.round(Number(metrics.target_relative_reduction || 0) * 100)}% error · WER ${Number(metrics.wer_delta_percentage_points || 0).toFixed(1)} pp · costo CI $${Number(metrics.cost_delta_ci_high_usd || 0).toFixed(3)}`}
        </span>
      );
    } },
    { key: "status", header: "Estado", render: (row) => <StatusBadge status={row.status} map={STATUS} /> },
    { key: "actions", header: "Gobierno", align: "right", render: (row) => (
      <div className="flex justify-end gap-2" onClick={(event) => event.stopPropagation()}>
        {["draft", "failed", "blocked"].includes(row.status) && (
          <button type="button" disabled={Boolean(working)} onClick={() => mutate(row, "validate")} className="px-2.5 py-1 rounded-md text-label bg-brand/15 text-brand-light disabled:opacity-40">Validar</button>
        )}
        {row.status === "ready" && (
          <button type="button" disabled={Boolean(working)} onClick={() => mutate(row, "approve")} className="px-2.5 py-1 rounded-md text-label bg-accent/15 text-accent disabled:opacity-40">Aprobar</button>
        )}
        {!['approved', 'rejected', 'superseded'].includes(row.status) && (
          <button type="button" disabled={Boolean(working)} onClick={() => mutate(row, "reject")} className="px-2.5 py-1 rounded-md text-label bg-red-500/10 text-red-300 disabled:opacity-40">Rechazar</button>
        )}
      </div>
    ) },
  ], [mutate, working]);

  const observations = summary?.observations || {};
  const readiness = summary?.model_readiness || {};
  const operatorSuggestions = summary?.operator_suggestions || {};
  const suggestionTypes = operatorSuggestions.by_type || {};
  const suggestionTotals = Object.values(suggestionTypes).reduce(
    (total, row) => ({
      shown: total.shown + Number(row?.shown || 0),
      accepted: total.accepted + Number(row?.accepted || 0),
      decided: total.decided + Number(row?.decided || 0),
    }),
    { shown: 0, accepted: 0, decided: 0 },
  );
  const filteredPatterns = patternStatus
    ? patterns.filter((row) => row.status === patternStatus) : patterns;
  const filteredProposals = proposalStatus
    ? proposals.filter((row) => row.status === proposalStatus) : proposals;
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-white">Aprendizaje por correcciones</h2>
          <p className="text-caption text-gray-500 mt-1">Asociaciones desidentificadas. Ninguna acción modifica el pipeline automáticamente.</p>
        </div>
        <button type="button" onClick={refresh} className="px-3 py-1.5 rounded-md text-caption ring-1 ring-white/[0.08] text-gray-300">Refrescar</button>
      </div>

      {error && <div role="alert" className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-4 py-3 text-caption text-red-200">{error}</div>}

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <KpiCard loading={loading} label="Observaciones" value={observations.total || 0} />
        <KpiCard loading={loading} label="Confiables" value={observations.tiers?.trusted || 0} tone="accent" />
        <KpiCard loading={loading} label="Corrección p50" value={observations.operator_minutes?.p50 == null ? "—" : `${observations.operator_minutes.p50} min`} tone="brand" />
        <KpiCard loading={loading} label="Corrección p90" value={observations.operator_minutes?.p90 == null ? "—" : `${observations.operator_minutes.p90} min`} tone="brand" />
        <KpiCard loading={loading} label="Modelo" value={readiness.eligible ? "Shadow listo" : `${readiness.trusted_observations || 0}/500`} hint="Nunca escribe letras" tone={readiness.eligible ? "accent" : "warn"} />
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <MetricBreakdown title="Trabajo por release" rows={observations.by_release} />
        <MetricBreakdown title="Trabajo por ruta" rows={observations.by_route} />
      </div>

      <section className="glass rounded-card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
          <div>
            <h3 className="text-ui font-semibold text-white">Sugerencias de un clic</h3>
            <p className="text-label text-gray-500 mt-0.5">Aceptación humana y ahorro operativo; nunca autocorrige.</p>
          </div>
          <div className="flex gap-2 text-label">
            <span className="rounded-md bg-white/[0.04] px-2 py-1 text-gray-300">{operatorSuggestions.songs || 0} canciones</span>
            <span className="rounded-md bg-white/[0.04] px-2 py-1 text-gray-300">{suggestionTotals.shown} mostradas</span>
            <span className="rounded-md bg-accent/10 px-2 py-1 text-accent">
              {suggestionTotals.decided ? `${Math.round(100 * suggestionTotals.accepted / suggestionTotals.decided)}% aceptadas` : "Sin decisiones aún"}
            </span>
          </div>
        </div>
        <div className="grid gap-2 md:grid-cols-3">
          {["timing", "text", "vocalization"].map((kind) => {
            const row = suggestionTypes[kind] || {};
            const label = kind === "timing" ? "Timing" : kind === "text" ? "Texto" : "Vocalizaciones";
            return (
              <div key={kind} className="rounded-card bg-white/[0.025] ring-1 ring-white/[0.06] p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-caption font-medium text-gray-200">{label}</span>
                  <StatusBadge status={row.sanity_gate_met ? "ready" : "validating"} map={STATUS} />
                </div>
                <p className="mt-2 text-label text-gray-400">
                  {row.accepted || 0} aceptadas · {row.rejected || 0} rechazadas · {row.manual || 0} manuales · {row.shown || 0} mostradas
                </p>
                <p className="mt-1 text-label text-gray-500">
                  Aceptación {row.acceptance_rate == null ? "—" : `${Math.round(row.acceptance_rate * 100)}%`} · gate 70%
                </p>
              </div>
            );
          })}
        </div>
        <p className="mt-3 text-label text-gray-500">
          Finales graves resueltos: {operatorSuggestions.severe_timing_resolved || 0}
          {" "}({operatorSuggestions.severe_timing_accepted || 0} por clic · {operatorSuggestions.severe_timing_manual || 0} a mano).
        </p>
      </section>

      <section className="glass rounded-card p-4">
        <div className="flex items-center justify-between gap-3 mb-3">
          <h3 className="text-ui font-semibold text-white">Patrones recurrentes</h3>
          <label className="text-label text-gray-400">
            Estado{" "}
            <select aria-label="Filtrar patrones por estado" value={patternStatus} onChange={(event) => setPatternStatus(event.target.value)} className="bg-gray-950 ring-1 ring-white/10 rounded px-2 py-1">
              <option value="">Todos</option>
              <option value="emerging">Emergente</option>
              <option value="correlated">Correlacionado</option>
              <option value="confirmed">Confirmado</option>
              <option value="stale">Sin soporte</option>
            </select>
          </label>
        </div>
        {Object.keys(observations.by_category || {}).length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3">
            {Object.entries(observations.by_category).slice(0, 8).map(([category, value]) => (
              <span key={category} className="px-2 py-1 rounded-md bg-white/[0.04] text-label text-gray-400">
                {category}: {value.observations} · p50/p90 {value.operator_minutes_p50 ?? "—"}/{value.operator_minutes_p90 ?? "—"}m
              </span>
            ))}
          </div>
        )}
        <DataTable columns={patternColumns} rows={filteredPatterns} rowKey={(row) => row.id} loading={loading} dense empty={<EmptyState title="Todavía no hay patrones k-anónimos" />} />
      </section>

      <section className="glass rounded-card p-4">
        <div className="flex items-center justify-between gap-3 mb-3">
          <h3 className="text-ui font-semibold text-white">Fixes propuestos</h3>
          <label className="text-label text-gray-400">
            Estado{" "}
            <select aria-label="Filtrar propuestas por estado" value={proposalStatus} onChange={(event) => setProposalStatus(event.target.value)} className="bg-gray-950 ring-1 ring-white/10 rounded px-2 py-1">
              <option value="">Todos</option>
              <option value="draft">Borrador</option>
              <option value="validating">Validando</option>
              <option value="ready">Validado</option>
              <option value="approved">Listo para implementar</option>
              <option value="failed">No pasó</option>
              <option value="blocked">Bloqueado</option>
              <option value="rejected">Rechazado</option>
            </select>
          </label>
        </div>
        <DataTable columns={proposalColumns} rows={filteredProposals} rowKey={(row) => row.id} loading={loading} dense empty={<EmptyState title="Todavía no hay propuestas calificadas" />} />
      </section>
    </div>
  );
}
