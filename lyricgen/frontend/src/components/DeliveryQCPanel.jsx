import { useMemo, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";
function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const TONES = {
  PASS: "text-emerald-300 bg-emerald-500/10 ring-emerald-500/25",
  REVIEW: "text-amber-200 bg-amber-500/10 ring-amber-500/25",
  BLOCK: "text-red-300 bg-red-500/10 ring-red-500/25",
  STALE: "text-ink-secondary bg-white/[0.04] ring-white/10",
};

export default function DeliveryQCPanel({ job, onJobUpdate, onSeek, onOpenEditor }) {
  const report = job?.delivery_qc;
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const safeActions = useMemo(
    () => (report?.repairs?.actions || []).filter((row) => row.status === "APPLIED"),
    [report],
  );
  if (!report) return null;

  const state = report.status === "STALE" ? "STALE" : (report.decision || "PASS");
  const updateDecision = async (issue, decision) => {
    setBusy(issue.issue_id);
    setError("");
    try {
      const response = await fetch(
        `${API}/jobs/${job.job_id}/delivery-qc/issues/${issue.issue_id}/decision`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ decision, reason: "reviewer_qc_panel" }),
        },
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail?.message || data.detail || "No se pudo guardar la decisión");
      onJobUpdate?.({ ...job, delivery_qc: data.delivery_qc });
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy("");
    }
  };

  const applySafeActions = async (domain) => {
    const actions = safeActions.filter((row) => domain === "metadata" ? row.domain === "metadata" : ["text", "timing"].includes(row.domain));
    if (!actions.length) return;
    setBusy(`apply-${domain}`);
    setError("");
    try {
      const payload = {
        edit_type: domain === "metadata" ? "metadata" : "lyrics",
        base_revision: job.segments_revision || 0,
        delivery_qc_action_ids: actions.map((row) => row.action_id),
      };
      if (domain === "metadata") {
        for (const action of actions) {
          const path = action.patch?.path || "";
          if (path.endsWith("rendered_title")) payload.song_title = action.patch.after;
          if (path.endsWith("rendered_artist")) payload.artist = action.patch.after;
        }
      } else {
        payload.segments = report.repairs.candidate_segments;
      }
      const response = await fetch(`${API}/edit/${job.job_id}`, {
        method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail?.message || data.detail || "No se pudo aplicar la sugerencia");
      onJobUpdate?.({ ...job, status: "editing", delivery_qc: { ...report, status: "STALE", stale_reason: "edit_render_pending" } });
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy("");
    }
  };

  return (
    <section data-testid="delivery-qc-panel" className="rounded-card p-5 mb-6 bg-surface/80 ring-1 ring-white/10">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h3 className="text-sm font-semibold">Preflight de entrega</h3>
          <p className="text-xs text-ink-secondary mt-1">Control final tipo sello sobre el video renderizado.</p>
        </div>
        <span className={`px-2.5 py-1 rounded-full text-[11px] font-semibold ring-1 ${TONES[state] || TONES.STALE}`}>
          {state === "PASS" ? "Sin hallazgos" : state === "REVIEW" ? "Revisar" : state === "BLOCK" ? "Bloqueado" : "Desactualizado"}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 mb-4 text-center">
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.summary?.fail_count || 0}</div><div className="text-[10px] text-ink-secondary">críticos</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.summary?.warn_count || 0}</div><div className="text-[10px] text-ink-secondary">avisos</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.summary?.open_count || 0}</div><div className="text-[10px] text-ink-secondary">abiertos</div></div>
      </div>

      {report.status === "STALE" && <p className="text-xs text-amber-200 mb-3">Se está generando o falta analizar el render más reciente.</p>}
      <div className="space-y-2">
        {(report.issues || []).map((issue) => (
          <div key={issue.issue_id} className="rounded-xl bg-white/[0.03] ring-1 ring-white/[0.06] p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={issue.severity === "FAIL" ? "text-red-300 text-[10px] font-bold" : "text-amber-200 text-[10px] font-bold"}>{issue.severity}</span>
                  <p className="text-xs font-medium">{issue.summary}</p>
                </div>
                {issue.description && <p className="text-[11px] text-ink-secondary mt-1">{issue.description}</p>}
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {(issue.seconds || []).slice(0, 8).map((seconds, index) => (
                    <button key={`${seconds}-${index}`} onClick={() => onSeek?.(Number(seconds))} className="text-[10px] px-2 py-1 rounded-lg bg-brand/10 text-brand-light hover:bg-brand/20">
                      {issue.timecodes?.[index] || `${Number(seconds).toFixed(2)}s`}
                    </button>
                  ))}
                </div>
              </div>
              {issue.status === "OPEN" ? (
                <button disabled={busy === issue.issue_id} onClick={() => updateDecision(issue, issue.manual_verification_required ? "resolved_manual" : "acknowledged")} className="shrink-0 text-[11px] px-2.5 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-50">{issue.manual_verification_required ? "Firmar check" : "Revisado"}</button>
              ) : <span className="text-[10px] text-emerald-300">{issue.status}</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap gap-2 mt-4">
        {safeActions.some((row) => ["text", "timing"].includes(row.domain)) && (
          <button disabled={busy === "apply-lyrics"} onClick={() => applySafeActions("lyrics")} className="btn-primary h-10 px-4 text-xs">Corregir texto/timing seguro</button>
        )}
        {safeActions.some((row) => row.domain === "metadata") && (
          <button disabled={busy === "apply-metadata"} onClick={() => applySafeActions("metadata")} className="btn-primary h-10 px-4 text-xs">Corregir metadata segura</button>
        )}
        <button onClick={onOpenEditor} className="btn-secondary h-10 px-4 text-xs">Abrir editor</button>
      </div>
      {error && <p className="text-xs text-red-300 mt-3">{String(error)}</p>}
      {report.mode === "observe" && <p className="text-[10px] text-ink-secondary mt-3">Modo observar: no bloquea ni modifica una entrega automáticamente.</p>}
    </section>
  );
}
