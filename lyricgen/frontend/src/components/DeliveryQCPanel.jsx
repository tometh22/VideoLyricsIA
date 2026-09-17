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

const CHECK_TONES = {
  PASS: "text-emerald-300 bg-emerald-500/10 ring-emerald-500/25",
  FAIL: "text-red-300 bg-red-500/10 ring-red-500/25",
  REVIEW: "text-amber-200 bg-amber-500/10 ring-amber-500/25",
  NOT_RUN: "text-ink-secondary bg-white/[0.04] ring-white/10",
};

const CHECK_LABELS = {
  PASS: "Pasó",
  FAIL: "Falló",
  REVIEW: "Revisión",
  NOT_RUN: "No ejecutado",
};

const GENERIC_REVIEW_DETECTOR = "mandatory_signed_reviewer_checklist";

function identityKey(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function isSwappedMetadataIssue(issue, job) {
  const actual = identityKey(issue.actual);
  const expected = identityKey(issue.expected);
  const artist = identityKey(job?.artist);
  const title = identityKey(job?.song_title);
  return Boolean(
    actual && expected && artist && title
    && ((actual === artist && expected === title)
      || (actual === title && expected === artist))
  );
}

export default function DeliveryQCPanel({ job, onJobUpdate, onSeek, onOpenEditor }) {
  const report = job?.delivery_qc;
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const refresh = async () => {
    setBusy("refresh");
    setError("");
    try {
      const response = await fetch(`${API}/jobs/${job.job_id}/delivery-qc/recheck`, {
        method: "POST",
        headers: authHeaders(),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || detail?.code || "No se pudo actualizar el preflight");
      }
      onJobUpdate?.({ ...job, delivery_qc: data.delivery_qc });
    } catch (requestError) {
      setError(String(requestError.message || requestError));
    } finally {
      setBusy("");
    }
  };
  const safeActions = useMemo(
    () => (report?.repairs?.actions || []).filter((row) => row.status === "APPLIED"),
    [report],
  );
  const rawState = report?.status === "STALE" ? "STALE" : (report?.decision || "PASS");
  // Delivery QC is currently an observation layer for interactive editing.
  // A stale/legacy report may still say BLOCK, but that must not make the
  // editor look unavailable unless the report explicitly runs in enforce mode.
  const isNonBlocking = report?.mode !== "enforce";
  const state = isNonBlocking && rawState === "BLOCK" ? "REVIEW" : rawState;
  const hiddenSwappedIssueIds = useMemo(() => new Set(
    (report?.issues || [])
      .filter((issue) => isSwappedMetadataIssue(issue, job))
      .map((issue) => String(issue.issue_id)),
  ), [job, report]);
  const checkStatus = (check) => {
    if (
      check.check_id === "metadata_title" || check.check_id === "metadata_artist"
    ) {
      const issueIds = (check.issue_ids || []).map(String);
      if (issueIds.length > 0 && issueIds.every((id) => hiddenSwappedIssueIds.has(id))) {
        return "PASS";
      }
    }
    return check.status;
  };
  const checks = useMemo(() => {
    const seenLabels = new Set();
    return (report?.checks || []).filter((check) => {
      if (check.detector === GENERIC_REVIEW_DETECTOR) return false;
      const label = String(check.label || check.check_id || "");
      if (seenLabels.has(label)) return false;
      seenLabels.add(label);
      return true;
    });
  }, [report]);
  const issues = useMemo(
    () => (report?.issues || []).filter((issue) => (
      issue.detector !== GENERIC_REVIEW_DETECTOR
      && issue.status === "OPEN"
      && !isSwappedMetadataIssue(issue, job)
    )),
    [job, report],
  );
  const visibleCheckSummary = useMemo(() => ({
    fail: checks.filter((check) => checkStatus(check) === "FAIL").length,
    review: checks.filter((check) => checkStatus(check) === "REVIEW").length,
    notRun: checks.filter((check) => checkStatus(check) === "NOT_RUN").length,
    pass: checks.filter((check) => checkStatus(check) === "PASS").length,
  }), [checks, hiddenSwappedIssueIds]);
  const hasCheckData = Array.isArray(report?.checks);
  const displayedFailCount = hasCheckData
    ? visibleCheckSummary.fail
    : (report?.summary?.fail_count ?? 0);
  if (!report) return null;
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
          <p className="text-xs text-ink-secondary mt-1">Resumen del render actual. No modifica la letra ni tus cambios.</p>
        </div>
        <div className="flex items-center gap-2">
          {onOpenEditor && (
            <button type="button" onClick={onOpenEditor} className="btn-primary h-9 px-3 text-xs">
              Editar cambios
            </button>
          )}
          <span className={`px-2.5 py-1 rounded-full text-[11px] font-semibold ring-1 ${TONES[state] || TONES.STALE}`}>
            {state === "PASS" ? "Sin hallazgos" : state === "REVIEW" ? (isNonBlocking ? "Revisión informativa" : "Revisar") : state === "BLOCK" ? "Bloqueado" : "Desactualizado"}
          </span>
        </div>
      </div>

      {isNonBlocking && (
        <div className="mb-4 rounded-xl bg-brand/10 px-3 py-2 text-xs text-brand-light ring-1 ring-brand/20">
          Estos checks son informativos por ahora y no bloquean la edición ni el avance del video.
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 mb-4 text-center">
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{displayedFailCount}</div><div className="text-[10px] text-ink-secondary">fallos objetivos</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{visibleCheckSummary.review}</div><div className="text-[10px] text-ink-secondary">revisiones</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{visibleCheckSummary.notRun}</div><div className="text-[10px] text-ink-secondary">no ejecutados</div></div>
      </div>

      {checks.length > 0 && (
        <div data-testid="delivery-qc-checks" className="mb-4 rounded-xl bg-white/[0.02] ring-1 ring-white/[0.06] p-3">
          <div className="flex items-center justify-between gap-3 mb-2">
            <p className="text-xs font-semibold">Checks automáticos y revisiones</p>
            <p className="text-[10px] text-ink-secondary">
              {visibleCheckSummary.pass} pasaron
            </p>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {checks.map((check) => {
              const status = CHECK_LABELS[checkStatus(check)] ? checkStatus(check) : "NOT_RUN";
              return (
                <div key={check.check_id} className="flex items-center justify-between gap-2 rounded-lg bg-white/[0.03] px-2.5 py-2">
                  <span className="min-w-0 truncate text-[11px] text-ink-secondary">{check.label}</span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${CHECK_TONES[status]}`}>
                    {CHECK_LABELS[status]}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {report.status === "STALE" && <div className="mb-3 space-y-2"><p className="text-xs text-amber-200">El reporte corresponde a una versión anterior o todavía no fue generado.</p><button disabled={busy === "refresh"} onClick={refresh} className="btn-secondary h-9 px-3 text-xs">{busy === "refresh" ? "Actualizando preflight…" : "Actualizar preflight"}</button></div>}
      <div className="space-y-2">
        {issues.map((issue) => (
          <div key={issue.issue_id} className="rounded-xl bg-white/[0.03] ring-1 ring-white/[0.06] p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={issue.result_status === "FAIL" || (!issue.result_status && issue.severity === "FAIL" && !issue.manual_verification_required) ? "text-red-300 text-[10px] font-bold" : "text-amber-200 text-[10px] font-bold"}>{issue.result_status || (issue.manual_verification_required ? "REVIEW" : issue.severity)}</span>
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

      {!issues.length && (
        <p className="text-xs text-ink-secondary mt-3">No hay observaciones accionables para mostrar.</p>
      )}

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
