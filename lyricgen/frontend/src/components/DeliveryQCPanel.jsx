import { useEffect, useMemo, useRef, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";
function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const TONES = {
  PASS: "text-emerald-300 bg-emerald-500/10 ring-emerald-500/25",
  BYPASSED: "text-amber-100 bg-amber-500/10 ring-amber-400/25",
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
  NOT_RUN: "No verificado",
  NOT_APPLICABLE: "No aplica",
};

const MANUAL_REVIEW_CODES = new Set([
  "UMG_BLACK_BARS", "UMG_BACKGROUND_TEXT", "UMG_SCENE_CHANGE",
  "UMG_LUMINANCE_STABLE", "UMG_MOBILE_CONTRAST", "UMG_LYRIC_NOT_LATE",
  "UMG_TITLE_METADATA", "UMG_IMAGE_NOT_STRETCHED",
]);

export default function DeliveryQCPanel({ job, onJobUpdate, onSeek, onOpenEditor, forUmgDelivery = false, focusRequest = 0, onContinueToPublish, onReturnToPublish }) {
  const report = job?.delivery_qc;
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const firstActionRef = useRef(null);
  const latestJob = useRef(job);
  latestJob.current = job;
  const requestRef = useRef(null);
  const identity = JSON.stringify([job?.job_id, job?.segments_revision, job?.status,
    report]);
  const identityRef = useRef(identity);
  identityRef.current = identity;
  useEffect(() => {
    requestRef.current = null;
    setBusy(""); setError(""); setReviewConfirmed(false);
    return () => { requestRef.current = null; };
  }, [identity]);
  useEffect(() => {
    if (!focusRequest) return;
    requestAnimationFrame(() => firstActionRef.current?.focus?.());
  }, [focusRequest]);
  const beginRequest = (label) => {
    if (requestRef.current?.identity === identity) return null;
    const request = { identity };
    requestRef.current = request;
    setBusy(label); setError("");
    return request;
  };
  const currentRequest = request => requestRef.current === request && identityRef.current === request.identity;
  const finishRequest = request => {
    if (currentRequest(request)) { requestRef.current = null; setBusy(""); }
  };
  const updateReport = (request, data) => {
    if (!data?.delivery_qc || typeof data.delivery_qc !== "object") throw new Error("El servidor no confirmó un informe verificable. Actualizá el estado antes de reintentar.");
    if (currentRequest(request)) onJobUpdate?.({ ...latestJob.current, delivery_qc: data.delivery_qc });
  };
  const refresh = async () => {
    const request = beginRequest("refresh");
    if (!request) return;
    try {
      const response = await fetch(`${API}/jobs/${job.job_id}/delivery-qc/recheck`, {
        method: "POST",
        headers: forUmgDelivery ? { ...authHeaders(), "Content-Type": "application/json" } : authHeaders(),
        ...(forUmgDelivery ? { body: JSON.stringify({ for_umg_delivery: true }) } : {}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || detail?.code || "No se pudo actualizar el preflight");
      }
      updateReport(request, data);
    } catch (requestError) {
      if (currentRequest(request)) setError(String(requestError.message || requestError));
    } finally {
      finishRequest(request);
    }
  };
  const safeActions = useMemo(
    () => (report?.repairs?.actions || []).filter((row) => row.status === "APPLIED"),
    [report],
  );
  const isPreflightBypassed = report?.status === "BYPASSED"
    && report?.approval?.staging_preflight_bypass === true;
  const rawState = isPreflightBypassed ? "BYPASSED" : report?.status === "STALE" ? "STALE" : (report?.decision || "UNKNOWN");
  // Delivery QC is currently an observation layer for interactive editing.
  // A stale/legacy report may still say BLOCK, but that must not make the
  // editor look unavailable unless the report explicitly runs in enforce mode.
  const isNonBlocking = report?.mode === "observe" && report?.approval?.blocked !== true;
  const state = isPreflightBypassed ? "BYPASSED" : report?.approval?.blocked === true ? "BLOCK" : rawState;
  // Rendering must not change the server's findings: an apparent OCR swap is
  // evidence to review, never permission to promote FAIL to PASS locally.
  const checkStatus = (check) => check.status;
  const checks = report?.checks || [];
  const issues = useMemo(
    () => (report?.issues || []).filter((issue) => (
      issue.status === "OPEN"
    )),
    [report],
  );
  const visibleCheckSummary = useMemo(() => ({
    fail: checks.filter((check) => checkStatus(check) === "FAIL").length,
    review: checks.filter((check) => checkStatus(check) === "REVIEW").length,
    notRun: checks.filter((check) => checkStatus(check) === "NOT_RUN").length,
    pass: checks.filter((check) => checkStatus(check) === "PASS").length,
  }), [checks]);
  const hasCheckData = Array.isArray(report?.checks);
  const displayedFailCount = hasCheckData
    ? visibleCheckSummary.fail
    : (report?.summary?.fail_count ?? 0);
  const effectiveBlockers = report?.approval?.blocked ? (report.approval.issue_ids || []) : [];
  const reportToken = report?.report_id || report?.generated_at;
  const reportReviewable = Boolean(reportToken) && report?.status === "COMPLETE";
  const isManualIssue = issue => issue.manual_verification_required ||
    issue.detector === "mandatory_signed_reviewer_checklist" || MANUAL_REVIEW_CODES.has(String(issue.code || "").toUpperCase());
  const allManualIssues = (report?.issues || []).filter(issue => isManualIssue(issue));
  const pendingManualIssues = allManualIssues.filter(issue => issue.status !== "RESOLVED_MANUAL");
  const pendingManualCount = pendingManualIssues.length;
  const manualIssues = pendingManualIssues;
  const reviewedManualCount = allManualIssues.filter(issue => issue.status === "RESOLVED_MANUAL").length;
  const objectiveFailures = issues.filter((issue) => issue.status === "OPEN" &&
    (issue.result_status === "FAIL" || (issue.severity === "FAIL" && !isManualIssue(issue))));
  if (!report) {
    if (!forUmgDelivery) return null;
    return (
      <section data-testid="delivery-qc-panel" className="rounded-card p-4 mb-6 bg-surface/80 ring-1 ring-white/10">
        <h3 className="text-sm font-semibold">Revisión antes de publicar</h3>
        <p className="text-xs text-ink-secondary mt-1">Analizá este corte y resolvé solo los puntos que necesiten atención.</p>
        <button ref={firstActionRef} type="button" onClick={refresh} disabled={Boolean(busy)} className="btn-primary h-10 px-4 mt-3 text-xs">
          {busy === "refresh" ? "Analizando corte…" : "Analizar corte"}
        </button>
        {error && <p role="alert" className="text-xs text-red-300 mt-3">{error}</p>}
      </section>
    );
  }
  const updateDecision = async (issue, decision) => {
    if (!reportReviewable) {
      setError("Actualizá el preflight antes de firmar: hace falta un informe completo con identidad verificable.");
      return;
    }
    const request = beginRequest(issue.issue_id);
    if (!request) return;
    try {
      const response = await fetch(
        `${API}/jobs/${job.job_id}/delivery-qc/issues/${issue.issue_id}/decision`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ decision, reason: "reviewer_qc_panel", expected_report_id: reportToken }),
        },
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(response.status === 409
        ? "El informe cambió mientras revisabas. Actualizá el preflight y revisá el nuevo corte antes de firmar; no repetimos tu decisión automáticamente."
        : data.detail?.message || data.detail || "No se pudo guardar la decisión");
      updateReport(request, data);
    } catch (requestError) {
      if (currentRequest(request)) setError(requestError.message);
    } finally {
      finishRequest(request);
    }
  };

  const attestFullReview = async () => {
    if (!reviewConfirmed || !reportReviewable) return;
    const request = beginRequest("attest-review");
    if (!request) return;
    try {
      const response = await fetch(`${API}/jobs/${job.job_id}/delivery-qc/review-attestation`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true, expected_report_id: reportToken }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(response.status === 409
        ? "El corte cambió mientras lo revisabas. Actualizá el preflight y revisá el nuevo render."
        : data.detail?.message || data.detail || "No se pudo guardar la revisión");
      updateReport(request, data);
      setReviewConfirmed(false);
    } catch (requestError) {
      if (currentRequest(request)) setError(String(requestError.message || requestError));
    } finally {
      finishRequest(request);
    }
  };

  const applySafeActions = async (domain) => {
    if (!reportReviewable) {
      setError("Actualizá el preflight y esperá un informe completo antes de aplicar sus sugerencias.");
      return;
    }
    const actions = safeActions.filter((row) => domain === "metadata" ? row.domain === "metadata" : ["text", "timing"].includes(row.domain));
    if (!actions.length) return;
    const request = beginRequest(`apply-${domain}`);
    if (!request) return;
    try {
      const payload = {
        edit_type: domain === "metadata" ? "metadata" : "lyrics",
        base_revision: job.segments_revision || 0,
        delivery_qc_action_ids: actions.map((row) => row.action_id),
        expected_delivery_qc_report_id: reportToken,
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
      if (data.ok !== true && !["editing", "queued"].includes(data.status)) throw new Error("No pudimos confirmar la edición. Actualizá el estado antes de reintentar.");
      if (currentRequest(request)) onJobUpdate?.({ ...latestJob.current, status: "editing", delivery_qc: { ...report, status: "STALE", stale_reason: "edit_render_pending" } });
    } catch (requestError) {
      if (currentRequest(request)) setError(requestError.message);
    } finally {
      finishRequest(request);
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
            {state === "PASS" ? "Sin hallazgos" : state === "BYPASSED" ? "Omitido temporalmente" : state === "REVIEW" ? (isNonBlocking ? "Revisión informativa" : "Revisar") : state === "BLOCK" ? (isNonBlocking ? "Hallazgos del informe" : "Bloqueado") : state === "STALE" ? "Desactualizado" : "Verificación pendiente"}
          </span>
        </div>
      </div>

      {isNonBlocking && (
        <div className="mb-4 rounded-xl bg-brand/10 px-3 py-2 text-xs text-brand-light ring-1 ring-brand/20">
          Este informe es informativo. Podés editar; la aprobación y publicación validan sus requisitos por separado.
        </div>
      )}

      {isPreflightBypassed && (
        <div role="status" data-testid="delivery-qc-staging-bypass" className="mb-4 rounded-xl bg-amber-500/[0.08] px-3 py-2 text-xs text-amber-100 ring-1 ring-amber-400/20">
          La revisión preflight está omitida temporalmente para esta campaña de staging. La aprobación de letra, los archivos requeridos y ProRes mantienen sus controles habituales.
        </div>
      )}

      {!reportToken && !isPreflightBypassed && (
        <div role="alert" className="mb-4 rounded-xl bg-amber-500/10 p-3 text-xs text-amber-200">
          Este informe no tiene una identidad verificable. Actualizá el preflight antes de firmar una revisión.
          <button type="button" onClick={refresh} disabled={Boolean(busy)} className="ml-2 underline">Actualizar informe para revisar</button>
        </div>
      )}

      {forUmgDelivery && !isPreflightBypassed && (report.approval?.blocked || report.approval?.reason === "fresh_preflight_required") && (
        <div role="alert" aria-label="Motivo del bloqueo de aprobación"
          className={`mb-4 rounded-xl p-3 text-xs ring-1 ${objectiveFailures.length ? "bg-red-500/10 text-red-200 ring-red-400/20" : "bg-amber-500/10 text-amber-100 ring-amber-400/20"}`}>
          <p className="font-semibold">{report.approval.reason === "fresh_preflight_required"
            ? "Este corte todavía no tiene una verificación vigente."
            : objectiveFailures.length
              ? `${objectiveFailures.length} ${objectiveFailures.length === 1 ? "falla detectada" : "fallas detectadas"}: corregilas y generá un nuevo render.`
              : report.approval.reason === "manual_review_required"
                ? "No hay fallas automáticas abiertas. Falta confirmar la revisión del video completo."
                : "La publicación sigue bloqueada por una validación pendiente."}</p>
          {effectiveBlockers.length > 0 && objectiveFailures.length > 0 && <p className="mt-1">Puntos a corregir: {objectiveFailures.map(row => row.summary).join(" · ")}</p>}
          {report.status === "COMPLETE" && <button type="button" onClick={refresh} disabled={Boolean(busy)}
            className="mt-2 underline">Actualizar preflight</button>}
        </div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mb-4 text-center">
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{displayedFailCount}</div><div className="text-[10px] text-ink-secondary">fallas detectadas</div></div>
        <div className="rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{pendingManualCount}</div><div className="text-[10px] text-ink-secondary">controles para revisar</div></div>
        <div className="col-span-2 sm:col-span-1 rounded-xl bg-white/[0.03] p-2"><div className="text-lg font-semibold">{visibleCheckSummary.notRun}</div><div className="text-[10px] text-ink-secondary">sin verificación automática</div></div>
      </div>

      {checks.length > 0 && (
        <details data-testid="delivery-qc-checks" open={visibleCheckSummary.fail > 0} className="mb-4 rounded-xl bg-white/[0.02] ring-1 ring-white/[0.06] p-3">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-xs font-semibold">
            <span>Verificaciones automáticas</span>
            <span className="text-[10px] font-normal text-ink-secondary">
              {visibleCheckSummary.fail > 0
                ? `${visibleCheckSummary.fail} ${visibleCheckSummary.fail === 1 ? "falla" : "fallas"}`
                : `${visibleCheckSummary.pass} pasaron · ${visibleCheckSummary.notRun} sin verificar`}
            </span>
          </summary>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {checks.map((check) => {
              const status = CHECK_LABELS[checkStatus(check)] ? checkStatus(check) : "NOT_RUN";
              if (check.detector === "mandatory_signed_reviewer_checklist" || MANUAL_REVIEW_CODES.has(String(check.check_id || "").toUpperCase())) return null;
              return (
                <div key={check.check_id} title={check.reason || undefined} className="flex items-center justify-between gap-2 rounded-lg bg-white/[0.03] px-2.5 py-2">
                  <span className="min-w-0 text-[11px] text-ink-secondary">{check.label}{check.reason && <span className="block text-[10px] opacity-70">{check.reason}</span>}</span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${CHECK_TONES[status] || CHECK_TONES.NOT_RUN}`}>
                    {CHECK_LABELS[status]}
                  </span>
                </div>
              );
            })}
          </div>
        </details>
      )}
      {forUmgDelivery && pendingManualCount > 0 && (
        <div className="mb-4 rounded-xl bg-amber-500/[0.07] p-4 ring-1 ring-amber-400/20" data-testid="delivery-qc-human-review">
          <div className="flex items-start gap-3">
            <span aria-hidden="true" className="mt-0.5 text-amber-200">◉</span>
            <div className="min-w-0 flex-1">
              <h4 className="text-sm font-semibold text-amber-100">Revisión final del video</h4>
              <p className="mt-1 text-xs text-ink-secondary">Mirá el render completo. Los controles de abajo requieren criterio humano; el sistema no puede confirmarlos con seguridad.</p>
              <ul className="mt-3 grid gap-2 sm:grid-cols-2">
                {manualIssues.map(issue => (
                  <li key={issue.issue_id} className="rounded-lg bg-black/10 px-3 py-2 text-[11px] text-ink-secondary">{issue.summary}</li>
                ))}
              </ul>
              <label className="mt-4 flex cursor-pointer items-start gap-2 text-xs text-ink-primary">
                <input type="checkbox" checked={reviewConfirmed} onChange={event => setReviewConfirmed(event.target.checked)} disabled={!reportReviewable || Boolean(busy)} className="mt-0.5 accent-amber-400" />
                <span>Revisé el corte actual completo y confirmé estos controles.</span>
              </label>
              <button ref={firstActionRef} type="button" disabled={!reviewConfirmed || !reportReviewable || Boolean(busy)} onClick={attestFullReview} className="btn-primary mt-3 h-10 px-4 text-xs disabled:opacity-50">
                {busy === "attest-review" ? "Guardando revisión…" : "Confirmar revisión del video"}
              </button>
              <p className="mt-2 text-[10px] text-ink-secondary">La confirmación queda asociada a este render y a tu usuario.</p>
            </div>
          </div>
        </div>
      )}
      {forUmgDelivery && pendingManualCount === 0 && reviewedManualCount > 0 && (
        <div role="status" className="mb-4 rounded-xl bg-emerald-500/[0.08] p-3 text-xs text-emerald-100 ring-1 ring-emerald-400/20">
          <p className="font-semibold">Revisión guardada para este render.</p>
          <p className="mt-1 text-emerald-100/80">{objectiveFailures.length
            ? "Todavía hay fallas automáticas que corregir antes de publicar."
            : "La confirmación quedó asociada a tu usuario y a este corte."}</p>
        </div>
      )}

      {!isPreflightBypassed && report.status !== "COMPLETE" && <div className="mb-3 space-y-2"><p className="text-xs text-amber-200">El informe no está vigente o completo. Actualizá el preflight y esperá a que termine antes de firmar o aplicar sugerencias.</p><button disabled={Boolean(busy)} onClick={refresh} className="btn-secondary h-9 px-3 text-xs">{busy === "refresh" ? "Actualizando preflight…" : "Actualizar preflight"}</button></div>}
      <div className="space-y-2">
        {issues.filter(issue => !isManualIssue(issue)).map((issue) => (
          <div key={issue.issue_id} data-issue-id={issue.issue_id}
            data-effective-blocker={effectiveBlockers.includes(issue.issue_id) ? "true" : undefined}
            className="rounded-xl bg-white/[0.03] ring-1 ring-white/[0.06] p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={issue.result_status === "FAIL" || (!issue.result_status && issue.severity === "FAIL" && !issue.manual_verification_required) ? "text-red-300 text-[10px] font-bold" : "text-amber-200 text-[10px] font-bold"}>{issue.result_status || (issue.manual_verification_required ? "REVIEW" : issue.severity)}</span>
                  <p className="text-xs font-medium">{issue.summary}</p>
                </div>
                {issue.description && <p className="text-[11px] text-ink-secondary mt-1">{issue.description}</p>}
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {(issue.seconds || []).slice(0, 8).map((seconds, index) => (
                    <button key={`${seconds}-${index}`} aria-label={`Ir a ${issue.summary}, ${issue.timecodes?.[index] || `${Number(seconds).toFixed(2)} segundos`}`} onClick={() => onSeek?.(Number(seconds))} className="text-[10px] px-2 py-1 rounded-lg bg-brand/10 text-brand-light hover:bg-brand/20">
                      {issue.timecodes?.[index] || `${Number(seconds).toFixed(2)}s`}
                    </button>
                  ))}
                </div>
              </div>
              {issue.status === "OPEN" && (issue.result_status === "FAIL" || issue.severity === "FAIL") ? (
                <button type="button" onClick={onOpenEditor} className="shrink-0 text-[11px] px-2.5 py-1.5 rounded-lg bg-red-500/10 text-red-200 hover:bg-red-500/20">Corregir video</button>
              ) : issue.status === "OPEN" ? (
                <button disabled={Boolean(busy) || !reportReviewable} onClick={() => updateDecision(issue, "acknowledged")} className="shrink-0 text-[11px] px-2.5 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-50">Revisado</button>
              ) : <span className="text-[10px] text-emerald-300">{issue.status}</span>}
            </div>
          </div>
        ))}
      </div>

      {!issues.length && (
        <p className="text-xs text-ink-secondary mt-3">No hay observaciones accionables para mostrar.</p>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        {forUmgDelivery && reportReviewable && report.approval?.can_approve && onContinueToPublish && (
          <button type="button" onClick={onContinueToPublish} className="btn-primary h-10 px-4 text-xs">Continuar a publicar</button>
        )}
        {forUmgDelivery && reportReviewable && report.approval?.can_approve && onReturnToPublish && (
          <button type="button" onClick={onReturnToPublish} className="btn-primary h-10 px-4 text-xs">Volver al pedido para publicar</button>
        )}
        {safeActions.some((row) => ["text", "timing"].includes(row.domain)) && (
          <button disabled={Boolean(busy) || !reportReviewable} onClick={() => applySafeActions("lyrics")} className="btn-primary h-10 px-4 text-xs">Corregir texto/timing seguro</button>
        )}
        {safeActions.some((row) => row.domain === "metadata") && (
          <button disabled={Boolean(busy) || !reportReviewable} onClick={() => applySafeActions("metadata")} className="btn-primary h-10 px-4 text-xs">Corregir metadata segura</button>
        )}
        <button onClick={onOpenEditor} className="btn-secondary h-10 px-4 text-xs">Abrir editor</button>
      </div>
      {error && <p role="alert" className="text-xs text-red-300 mt-3">{String(error)}</p>}
      {report.mode === "observe" && <p className="text-[10px] text-ink-secondary mt-3">Modo observar: no bloquea ni modifica una entrega automáticamente.</p>}
    </section>
  );
}
