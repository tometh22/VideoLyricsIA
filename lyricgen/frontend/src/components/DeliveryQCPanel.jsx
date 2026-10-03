import { useEffect, useMemo, useRef, useState } from "react";

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

export default function DeliveryQCPanel({
  job, onJobUpdate, onSeek, onOpenEditor, forUmgDelivery = false,
  onManualReviewComplete,
  focusRequest = 0, focusTarget = "manual", preflightDisabled = false,
}) {
  const report = job?.delivery_qc;
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const manualSectionRef = useRef(null);
  const findingsRef = useRef(null);
  useEffect(() => {
    if (!focusRequest) return;
    const target = focusTarget === "findings" ? findingsRef.current : manualSectionRef.current;
    if (focusTarget !== "findings" && target) target.open = true;
    requestAnimationFrame(() => {
      const focusable = target?.querySelector("button:not(:disabled), [data-focus-first]");
      (focusable || target)?.focus?.();
    });
  }, [focusRequest, focusTarget]);
  const refresh = async () => {
    setBusy("refresh");
    setError("");
    try {
      const response = await fetch(`${API}/jobs/${job.job_id}/delivery-qc/recheck`, {
        method: "POST",
        headers: forUmgDelivery
          ? { ...authHeaders(), "Content-Type": "application/json" }
          : authHeaders(),
        ...(forUmgDelivery ? { body: JSON.stringify({ for_umg_delivery: true }) } : {}),
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
  if (!report) {
    if (!forUmgDelivery) return null;
    return (
      <section data-testid="delivery-qc-panel" className="rounded-card p-4 mb-6 bg-surface/80 ring-1 ring-white/10">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">Revisión antes de publicar</h3>
            <p className="text-xs text-ink-secondary mt-1">Analizá este corte y resolvé solo los puntos que necesiten atención.</p>
          </div>
          <button type="button" onClick={refresh} disabled={busy === "refresh" || preflightDisabled} aria-busy={busy === "refresh"} className="btn-primary h-10 px-4 text-xs">
            {busy === "refresh" ? "Analizando corte…" : "Analizar corte"}
          </button>
        </div>
        {error && <p role="alert" className="text-xs text-red-300 mt-3">{String(error)}</p>}
      </section>
    );
  }

  const isStale = report.status === "STALE";
  const isComplete = report.status === "COMPLETE" && !isStale;
  const state = isStale ? "STALE" : report.status === "FAILED" ? "BLOCK" : report.status === "RUNNING" ? "REVIEW" : (report.decision || "PASS");
  const stateLabel = isStale ? "Desactualizado" : report.status === "FAILED" ? "No se pudo analizar" : report.status === "RUNNING" ? "Analizando…" : state === "PASS" ? "Sin hallazgos" : state === "REVIEW" ? "Revisar" : state === "BLOCK" ? "Bloqueado" : "Desactualizado";
  const checks = report.checks || [];
  const issues = report.issues || [];
  const manualIssues = issues.filter((issue) => (
    issue.manual_verification_required || issue.detector === "mandatory_signed_reviewer_checklist"
  ));
  const standardIssues = issues.filter((issue) => !(
    issue.manual_verification_required || issue.detector === "mandatory_signed_reviewer_checklist"
  ));
  const pendingManualCount = manualIssues.filter((issue) => issue.status === "OPEN").length;
  const updateDecision = async (issue, decision) => {
    setBusy(issue.issue_id);
    setError("");
    try {
      const response = await fetch(
        `${API}/jobs/${job.job_id}/delivery-qc/issues/${issue.issue_id}/decision`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            reason: "reviewer_qc_panel",
            // Decisions are fenced to the exact preview the operator saw.
            // The API rotates report_id after each decision so another tab
            // cannot overwrite a newer review; generated_at is the supported
            // identity for older reports without a report_id.
            expected_report_id: report.report_id || report.generated_at,
          }),
        },
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail;
        const message = detail?.message || (detail === "delivery_qc_report_stale"
          ? "El corte cambió. Actualizá el preflight antes de firmar." : detail === "blocking_fail_requires_correction_and_new_preflight"
            ? "Este fallo requiere corregir el video y volver a analizarlo." : detail === "manual_resolution_requires_mandatory_reviewer_check"
              ? "Solo podés firmar los controles visuales indicados." : detail) || "No se pudo guardar la revisión";
        throw new Error(message);
      }
      onJobUpdate?.({ ...job, delivery_qc: data.delivery_qc });
      if (decision === "resolved_manual") onManualReviewComplete?.(data.delivery_qc);
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
        <span role="status" aria-live="polite" className={`px-2.5 py-1 rounded-full text-[11px] font-semibold ring-1 ${TONES[state] || TONES.STALE}`}>
          {stateLabel}
        </span>
      </div>

      <div className="grid grid-cols-1 min-[360px]:grid-cols-3 gap-2 mb-4 text-center">
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.check_summary?.fail ?? report.summary?.fail_count ?? 0}</div><div className="text-[10px] text-ink-secondary">fallos reales</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.check_summary?.review ?? report.summary?.warn_count ?? 0}</div><div className="text-[10px] text-ink-secondary">revisiones</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{report.check_summary?.not_run ?? 0}</div><div className="text-[10px] text-ink-secondary">no ejecutados</div></div>
      </div>

      {checks.length > 0 && (
        <details data-testid="delivery-qc-checks" className="mb-4 rounded-xl bg-white/[0.02] ring-1 ring-white/[0.06] p-3">
          <summary className="cursor-pointer list-none text-xs font-semibold">
            Detalle de controles <span className="ml-2 text-[10px] font-normal text-ink-secondary">
              {report.check_summary?.pass ?? checks.filter((check) => check.status === "PASS").length} correctos · {report.check_summary?.fail ?? 0} fallidos · {report.check_summary?.review ?? 0} por revisar
            </span>
          </summary>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {checks.map((check) => {
              const status = CHECK_LABELS[check.status] ? check.status : "NOT_RUN";
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
        </details>
      )}

      {!isComplete && <div className="mb-3 space-y-2">
        <p className="text-xs text-amber-200">{isStale ? "El corte cambió desde este análisis. Actualizá el preflight antes de seguir." : report.status === "FAILED" ? "No pudimos completar el análisis de este corte." : "El análisis todavía no está completo."}</p>
        <button type="button" disabled={busy === "refresh" || preflightDisabled} onClick={refresh} aria-busy={busy === "refresh"} className="btn-secondary h-9 px-3 text-xs">{busy === "refresh" ? "Analizando corte…" : "Actualizar preflight"}</button>
      </div>}
      <div ref={findingsRef} tabIndex={-1} className="space-y-2 outline-none">
        {standardIssues.map((issue) => (
          <div key={issue.issue_id} data-issue-id={issue.issue_id} className="rounded-xl bg-white/[0.03] ring-1 ring-white/[0.06] p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={issue.result_status === "FAIL" || (!issue.result_status && issue.severity === "FAIL" && !issue.manual_verification_required) ? "text-red-300 text-[10px] font-bold" : "text-amber-200 text-[10px] font-bold"}>{issue.result_status || (issue.manual_verification_required ? "REVIEW" : issue.severity)}</span>
                  <p className="text-xs font-medium">{issue.summary}</p>
                </div>
                {issue.description && <p className="text-[11px] text-ink-secondary mt-1">{issue.description}</p>}
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {(issue.seconds || []).slice(0, 8).map((seconds, index) => (
                    <button key={`${seconds}-${index}`} aria-label={`Ir a ${issue.timecodes?.[index] || `${Number(seconds).toFixed(2)} segundos`}: ${issue.summary}`} onClick={() => onSeek?.(Number(seconds))} className="text-[10px] px-2 py-1 rounded-lg bg-brand/10 text-brand-light hover:bg-brand/20">
                      {issue.timecodes?.[index] || `${Number(seconds).toFixed(2)}s`}
                    </button>
                  ))}
                </div>
              </div>
              {issue.status === "OPEN" && issue.result_status !== "FAIL" && issue.severity !== "FAIL" ? (
                <button type="button" aria-label={`${issue.manual_verification_required ? "Firmar" : "Marcar revisado"}: ${issue.summary}`} disabled={!isComplete || Boolean(busy)} onClick={() => updateDecision(issue, issue.manual_verification_required ? "resolved_manual" : "acknowledged")} className="shrink-0 text-[11px] px-2.5 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-50">{issue.manual_verification_required ? "Firmar check" : "Marcar revisado"}</button>
              ) : issue.status === "OPEN" ? <span className="shrink-0 text-[10px] font-medium text-red-200">Requiere corrección</span> : <span className="text-[10px] text-emerald-300">{issue.status === "RESOLVED_MANUAL" ? "Revisado" : issue.status === "ACKNOWLEDGED" ? "Visto" : issue.status === "REJECTED" ? "Descartado" : issue.status}</span>}
            </div>
            {issue.status === "OPEN" && issue.result_status === "FAIL" && <p className="mt-2 text-[11px] text-red-200">Corregí este punto en el video y volvé a analizar el corte.</p>}
          </div>
        ))}
        {manualIssues.length > 0 && (
          <details ref={manualSectionRef} className="rounded-xl bg-white/[0.03] ring-1 ring-white/[0.06] p-3">
            <summary className="cursor-pointer list-none text-xs font-semibold">
              Revisión final <span className="ml-2 text-[10px] font-normal text-ink-secondary">
                {pendingManualCount ? "pendiente" : "completa"}
              </span>
            </summary>
            <p className="mt-2 text-[11px] text-ink-secondary">Reproducí el corte completo y confirmá una sola vez imagen, fondo, continuidad, letra, sincronía y título.</p>
            <div className="mt-2 space-y-2">
              {manualIssues.map((issue) => (
                <div key={issue.issue_id} data-issue-id={issue.issue_id} className="flex items-center justify-between gap-3 rounded-lg bg-white/[0.03] p-2.5">
                  <div className="min-w-0">
                    <p className="text-xs font-medium">{issue.summary}</p>
                    {issue.description && <p className="mt-1 text-[11px] text-ink-secondary">{issue.description}</p>}
                  </div>
                  {issue.status === "OPEN" ? (
                    <button type="button" aria-label={`Confirmar revisión: ${issue.summary}`} disabled={!isComplete || Boolean(busy)} onClick={() => updateDecision(issue, "resolved_manual")}
                      className="shrink-0 rounded-lg bg-white/5 px-2.5 py-1.5 text-[11px] hover:bg-white/10 disabled:opacity-50">Confirmar revisión</button>
                  ) : <span className="shrink-0 text-[10px] text-emerald-300">Revisado</span>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mt-4">
        {isComplete && safeActions.some((row) => ["text", "timing"].includes(row.domain)) && (
          <button disabled={busy === "apply-lyrics"} onClick={() => applySafeActions("lyrics")} className="btn-primary h-10 px-4 text-xs">Corregir texto/timing seguro</button>
        )}
        {isComplete && safeActions.some((row) => row.domain === "metadata") && (
          <button disabled={busy === "apply-metadata"} onClick={() => applySafeActions("metadata")} className="btn-primary h-10 px-4 text-xs">Corregir metadata segura</button>
        )}
        {onOpenEditor && <button type="button" onClick={onOpenEditor} className="btn-secondary h-10 px-4 text-xs">Abrir editor</button>}
      </div>
      {error && <p role="alert" aria-live="assertive" className="text-xs text-red-300 mt-3">{String(error)}</p>}
      {report.mode === "observe" && !forUmgDelivery && <p className="text-[10px] text-ink-secondary mt-3">Modo observar: no bloquea ni modifica una entrega automáticamente.</p>}
    </section>
  );
}
