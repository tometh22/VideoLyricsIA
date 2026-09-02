import { useEffect, useMemo, useState } from "react";

const CLOSED_STATUSES = new Set(["applied", "dismissed", "rejected", "cancelled", "observed"]);

const REASON_LABELS = {
  acoustic_cardinality_disagreement: "La cantidad de frases no coincide con el audio",
  event_count: "Cantidad de frases dudosa",
  low_asr_content_confidence: "Contenido con baja confianza",
  low_ctc_timing_confidence: "Timing con baja confianza",
  text_audio_mismatch: "La letra no coincide claramente con el audio",
  text_mismatch: "Posible diferencia entre letra y audio",
  voiced_gap: "Hay voz detectada sin una frase asignada",
  vocalization: "Posible vocalización faltante o incorrecta",
};

function asFiniteNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatTime(value) {
  const seconds = asFiniteNumber(value);
  if (seconds == null || seconds < 0) return "--:--.-";
  const minutes = Math.floor(seconds / 60);
  const remainder = (seconds % 60).toFixed(1).padStart(4, "0");
  return `${minutes}:${remainder}`;
}

function reasonLabel(reason) {
  if (reason == null) return "Revisión de calidad recomendada";
  if (typeof reason === "object") {
    return reasonLabel(reason.label || reason.message || reason.code || reason.reason);
  }
  const value = String(reason).trim();
  if (!value) return "Revisión de calidad recomendada";
  return REASON_LABELS[value] || `${value.charAt(0).toUpperCase()}${value.slice(1).replaceAll("_", " ")}`;
}

function segmentView(segment, fallbackStart, fallbackEnd) {
  if (typeof segment === "string" || typeof segment === "number") {
    return { text: String(segment), start: null, end: null };
  }
  const value = segment && typeof segment === "object" ? segment : {};
  return {
    text: value.text == null ? "" : String(value.text),
    start: asFiniteNumber(value.start ?? value.start_time ?? value.startTime ?? fallbackStart),
    end: asFiniteNumber(value.end ?? value.end_time ?? value.endTime ?? fallbackEnd),
  };
}

function SegmentList({ label, segments, fallbackStart, fallbackEnd, tone }) {
  const values = Array.isArray(segments) ? segments : [];
  const toneClass = tone === "proposed"
    ? "border-emerald-400/20 bg-emerald-950/20"
    : "border-white/10 bg-black/20";

  return (
    <div className={`min-w-0 rounded-xl border p-3 ${toneClass}`}>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-gray-400">
        {label}
      </h4>
      {values.length === 0 ? (
        <p className="text-sm italic text-gray-500">Sin segmentos</p>
      ) : (
        <ol className="space-y-2" aria-label={label}>
          {values.map((rawSegment, index) => {
            const segment = segmentView(rawSegment, fallbackStart, fallbackEnd);
            const hasTiming = segment.start != null || segment.end != null;
            return (
              <li
                key={`${segment.start ?? "x"}-${segment.end ?? "x"}-${index}`}
                className="rounded-lg bg-white/[0.035] px-2.5 py-2"
              >
                {hasTiming && (
                  <span className="mb-1 block font-mono text-[11px] text-gray-500">
                    {formatTime(segment.start)} – {formatTime(segment.end)}
                  </span>
                )}
                <span className="block whitespace-pre-wrap break-words text-sm leading-relaxed text-gray-100">
                  {segment.text || "(sin texto)"}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function parseExpiry(value) {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Review-only UI for editor v6 quality proposals.
 *
 * Callbacks are intentionally user-driven:
 * - onSeek(startSeconds, window)
 * - onApplySelected(windowIds, proposal)
 * - onDismiss(proposal)
 * - onObserve(windowId, verdict, proposal)
 * - onRejectWindow(windowId, reason, proposal)
 */
export default function QualityProposalPanel({
  proposal,
  currentRevision,
  onSeek,
  onApplySelected,
  onDismiss,
  onObserve,
  onRejectWindow,
  applying = false,
  dismissing = false,
  observing = false,
  rejecting = false,
}) {
  const [selectedKeys, setSelectedKeys] = useState(() => new Set());
  const [clock, setClock] = useState(() => Date.now());

  const windows = useMemo(
    () => (Array.isArray(proposal?.windows) ? proposal.windows : []),
    [proposal?.windows],
  );
  const windowEntries = useMemo(
    () => windows.map((window, index) => ({
      window,
      index,
      key: String(window?.id ?? `window-${index + 1}`),
    })),
    [windows],
  );
  const expiry = parseExpiry(proposal?.expires_at);
  const status = String(proposal?.status || "pending").toLowerCase();
  const observationOnly = proposal?.observation_only === true;
  const operatorOnly = proposal?.operator_suggestion_only === true;
  const closed = CLOSED_STATUSES.has(status);
  const expired = status === "expired" || (expiry != null && expiry <= clock);
  const stale = status === "stale" || (
    !closed
    && !expired
    && currentRevision != null
    && proposal?.base_revision != null
    && String(currentRevision) !== String(proposal.base_revision)
  );
  const unavailable = stale || expired || closed;

  useEffect(() => {
    setSelectedKeys(new Set());
  }, [proposal?.id, proposal?.base_revision]);

  useEffect(() => {
    const validKeys = new Set(windowEntries.map((entry) => entry.key));
    setSelectedKeys((previous) => {
      const next = unavailable
        ? new Set()
        : new Set([...previous].filter((key) => validKeys.has(key)));
      if (next.size === previous.size && [...next].every((key) => previous.has(key))) return previous;
      return next;
    });
  }, [unavailable, windowEntries]);

  useEffect(() => {
    if (expiry == null || expiry <= clock) return undefined;
    const delay = Math.min(expiry - clock + 25, 2_147_483_647);
    const timeout = window.setTimeout(() => setClock(Date.now()), delay);
    return () => window.clearTimeout(timeout);
  }, [clock, expiry]);

  if (!proposal) return null;

  const selectedEntries = windowEntries.filter((entry) => selectedKeys.has(entry.key));
  const busy = applying || dismissing || observing || rejecting;
  const applyDisabled = unavailable || busy || selectedEntries.length === 0;
  const dismissDisabled = unavailable || busy;

  const toggleWindow = (key) => {
    if (unavailable || busy) return;
    setSelectedKeys((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleApply = () => {
    if (applyDisabled || typeof onApplySelected !== "function") return;
    onApplySelected(selectedEntries.map(({ window }) => window?.id), proposal);
  };

  const handleDismiss = () => {
    if (dismissDisabled || typeof onDismiss !== "function") return;
    onDismiss(proposal);
  };

  const stateMessage = stale
    ? "Esta propuesta corresponde a una versión anterior de la letra. Actualizá el análisis para continuar."
    : expired
      ? "Esta propuesta venció. Solicitá un nuevo análisis antes de aplicar cambios."
      : closed
        ? status === "applied"
          ? "Esta propuesta ya fue aplicada."
          : status === "observed"
            ? "Todas las sugerencias quedaron calificadas para la calibración. No se aplicó ningún cambio."
          : "Esta propuesta ya no está disponible."
        : null;

  return (
    <section
      className="rounded-2xl border border-cyan-400/20 bg-slate-950/80 p-4 text-gray-100 shadow-xl shadow-black/20"
      aria-labelledby={`quality-proposal-title-${proposal.id}`}
      data-testid="quality-proposal-panel"
      data-proposal-state={stale ? "stale" : expired ? "expired" : closed ? status : "active"}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-cyan-300">
            {observationOnly
              ? "Calibración observable"
              : operatorOnly ? "Sugerencias de un clic" : "Revisión de calidad"}
          </p>
          <h2 id={`quality-proposal-title-${proposal.id}`} className="mt-1 text-base font-semibold text-white">
            {observationOnly
              ? "Ayudanos a medir estas sugerencias"
              : operatorOnly ? "Corregí más rápido sin perder control" : "Compará los cambios sugeridos"}
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-gray-400">
            {observationOnly
              ? "Escuchá cada zona y calificá si la sugerencia coincide con el audio. Este modo nunca modifica la letra ni el timing."
              : operatorOnly
                ? "Escuchá el tramo y aceptá o rechazá. Cada decisión queda medida; nada se corrige automáticamente."
                : "Escuchá cada zona y elegí solamente las correcciones que quieras aplicar. Nada se aplica automáticamente."}
          </p>
        </div>
        <span className="w-fit rounded-full border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-[11px] text-gray-400">
          Revisión base {proposal.base_revision ?? "—"}
        </span>
      </div>

      {stateMessage && (
        <div
          className="mt-4 rounded-xl border border-amber-400/30 bg-amber-950/30 px-3 py-2 text-sm text-amber-100"
          role="alert"
        >
          {stateMessage}
        </div>
      )}

      <fieldset className="mt-5 space-y-4" disabled={unavailable || busy}>
        <legend className="sr-only">Ventanas de corrección disponibles</legend>
        {windowEntries.length === 0 ? (
          <p className="rounded-xl border border-white/10 bg-white/[0.025] p-4 text-sm text-gray-400">
            No hay ventanas sugeridas para revisar.
          </p>
        ) : windowEntries.map(({ window: proposalWindow, index, key }) => {
          const label = `Zona ${index + 1}`;
          const range = `${formatTime(proposalWindow?.start)} a ${formatTime(proposalWindow?.end)}`;
          const reasons = Array.isArray(proposalWindow?.reasons) ? proposalWindow.reasons : [];
          const checked = selectedKeys.has(key);
          const suggestionType = String(proposalWindow?.suggestion_type || "text");
          const confidence = String(proposalWindow?.confidence || "medium");
          const currentEnd = asFiniteNumber(proposalWindow?.current_end);
          const proposedEnd = asFiniteNumber(proposalWindow?.proposed_end);
          const previewStart = asFiniteNumber(proposalWindow?.preview_start ?? proposalWindow?.start);

          return (
            <article
              key={key}
              className={`rounded-2xl border p-3 transition-colors sm:p-4 ${
                checked ? "border-cyan-300/50 bg-cyan-950/20" : "border-white/10 bg-white/[0.025]"
              }`}
              data-testid={`quality-proposal-window-${key}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-3">
                <label className="flex min-w-0 items-center gap-3 text-sm font-semibold text-white">
                  {!observationOnly && <input
                    type="checkbox"
                    className="h-4 w-4 shrink-0 accent-cyan-400"
                    checked={checked}
                    onChange={() => toggleWindow(key)}
                    disabled={unavailable || busy}
                    aria-label={`Seleccionar ${label.toLowerCase()}, ${range}`}
                  />}
                  <span>
                    {label}
                    <span className="ml-2 font-mono text-xs font-normal text-gray-400">
                      {formatTime(proposalWindow?.start)} – {formatTime(proposalWindow?.end)}
                    </span>
                  </span>
                </label>
                <button
                  type="button"
                  className="rounded-lg border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-medium text-gray-200 hover:border-cyan-300/40 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => {
                    const start = previewStart;
                    if (start != null && typeof onSeek === "function") onSeek(start, proposalWindow);
                  }}
                  disabled={previewStart == null}
                  aria-label={`Escuchar ${label.toLowerCase()} desde ${formatTime(previewStart)}`}
                >
                  Escuchar
                </button>
              </div>

              {reasons.length > 0 && (
                <ul className="mt-3 flex flex-wrap gap-1.5" aria-label={`Motivos de revisión de ${label.toLowerCase()}`}>
                  {reasons.map((reason, reasonIndex) => (
                    <li
                      key={`${reasonLabel(reason)}-${reasonIndex}`}
                      className="rounded-full bg-amber-300/10 px-2 py-1 text-[11px] text-amber-200"
                    >
                      {reasonLabel(reason)}
                    </li>
                  ))}
                </ul>
              )}

              {operatorOnly && (
                <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
                  <span className="rounded-full bg-cyan-300/10 px-2 py-1 font-semibold uppercase tracking-wide text-cyan-200">
                    {suggestionType === "timing" ? "Timing" : suggestionType === "vocalization" ? "Vocalización" : "Texto"}
                  </span>
                  <span className="rounded-full bg-white/5 px-2 py-1 text-gray-300">
                    Confianza {confidence === "high" ? "alta" : confidence === "low" ? "baja" : "media"}
                  </span>
                  {proposalWindow?.impact_ms != null && (
                    <span className="rounded-full bg-white/5 px-2 py-1 text-gray-400">
                      Impacto {Math.round(Number(proposalWindow.impact_ms))} ms
                    </span>
                  )}
                </div>
              )}

              {operatorOnly && suggestionType === "timing" && currentEnd != null && proposedEnd != null && (
                <div className="mt-3 rounded-xl border border-cyan-300/15 bg-cyan-950/15 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-gray-400">Fin actual <b className="font-mono text-white">{formatTime(currentEnd)}</b></span>
                    <span className="text-cyan-200">Fin propuesto <b className="font-mono text-white">{formatTime(proposedEnd)}</b></span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10" aria-hidden="true">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-gray-400 to-cyan-300"
                      style={{ width: `${Math.max(12, Math.min(100, 50 + Math.abs(proposedEnd - currentEnd) * 12))}%` }}
                    />
                  </div>
                </div>
              )}

              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <SegmentList
                  label="Antes"
                  segments={proposalWindow?.current_segments}
                  fallbackStart={proposalWindow?.start}
                  fallbackEnd={proposalWindow?.end}
                  tone="current"
                />
                <SegmentList
                  label="Propuesta"
                  segments={proposalWindow?.proposed_segments}
                  fallbackStart={proposalWindow?.start}
                  fallbackEnd={proposalWindow?.end}
                  tone="proposed"
                />
              </div>
              {observationOnly && (
                <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/10 pt-3">
                  {proposalWindow?.human_verdict ? (
                    <p className="text-xs font-medium text-emerald-300">
                      Calificada: {proposalWindow.human_verdict === "correct" ? "correcta" : proposalWindow.human_verdict === "incorrect" ? "incorrecta" : "dudosa"}
                    </p>
                  ) : [
                    ["correct", "Correcta"],
                    ["incorrect", "Incorrecta"],
                    ["uncertain", "No se distingue"],
                  ].map(([verdict, text]) => (
                    <button
                      key={verdict}
                      type="button"
                      className="rounded-lg border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-medium text-gray-200 hover:border-cyan-300/40 disabled:cursor-not-allowed disabled:opacity-50"
                      onClick={() => onObserve?.(proposalWindow?.id, verdict, proposal)}
                      disabled={unavailable || busy || typeof onObserve !== "function"}
                    >
                      {text}
                    </button>
                  ))}
                </div>
              )}
              {operatorOnly && (
                <div className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-white/10 pt-3">
                  <button
                    type="button"
                    className="rounded-lg border border-rose-300/25 bg-rose-950/20 px-3 py-1.5 text-xs font-semibold text-rose-100 hover:border-rose-300/50 disabled:opacity-50"
                    onClick={() => onRejectWindow?.(proposalWindow?.id, "operator_rejected", proposal)}
                    disabled={unavailable || busy || typeof onRejectWindow !== "function"}
                  >
                    Rechazar
                  </button>
                  <button
                    type="button"
                    className="rounded-lg bg-cyan-400 px-3 py-1.5 text-xs font-semibold text-slate-950 hover:bg-cyan-300 disabled:bg-gray-700 disabled:text-gray-400"
                    onClick={() => onApplySelected?.([proposalWindow?.id], proposal)}
                    disabled={unavailable || busy || typeof onApplySelected !== "function"}
                  >
                    Aceptar
                  </button>
                </div>
              )}
            </article>
          );
        })}
      </fieldset>

      {!observationOnly && <div className="mt-5 flex flex-col-reverse gap-2 border-t border-white/10 pt-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-gray-400" aria-live="polite">
          {selectedEntries.length === 0
            ? "No seleccionaste ninguna corrección."
            : `${selectedEntries.length} ${selectedEntries.length === 1 ? "corrección seleccionada" : "correcciones seleccionadas"}.`}
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <button
            type="button"
            className="rounded-lg border border-white/15 px-3.5 py-2 text-sm font-medium text-gray-300 hover:border-white/30 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
            onClick={handleDismiss}
            disabled={dismissDisabled}
          >
            {dismissing ? "Descartando…" : "Descartar propuesta"}
          </button>
          <button
            type="button"
            className="rounded-lg bg-cyan-400 px-3.5 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
            onClick={handleApply}
            disabled={applyDisabled}
          >
            {applying ? "Aplicando…" : `Aplicar seleccionadas (${selectedEntries.length})`}
          </button>
        </div>
      </div>}
    </section>
  );
}
