/**
 * Modern multi-step progress for the transcription phase.
 *
 * Backend emits `current_step` (i18n key like `transcribe.isolate_vocals`)
 * and `progress` (0-100) via SSE. The 8 fine-grained backend steps map to
 * 5 visible UI stages: Preparando audio → Buscando letra → Aislando voz →
 * Alineando con la canción → Finalizando.
 *
 * Active stage pulses, done stages get ✓, pending stay outlined. ETA is a
 * rough remaining-time estimate from typical stage durations (the value
 * comes from telemetry medians, hard-coded here for now).
 */

import { useEffect, useRef, useState } from "react";
import useJobProgress from "../hooks/useJobProgress";

// Map backend label keys → 1-based stage index (1..5). Anything 50-85% pct
// counts toward "Alineando con la canción" because that's the umbrella that
// covers Whisper / forced-align / whisperX.
const STAGE_BY_BACKEND_LABEL = {
  "transcribe.prepare":          1,
  "transcribe.lyrics_lookup":    2,
  "transcribe.isolate_vocals":   3,
  "transcribe.align":            4,
  "transcribe.verify":           4,
  "transcribe.transcribe":       4,
  "transcribe.transcribe_word":  4,
  "transcribe.recover":          5,
  "transcribe.done":             5,
};

const STAGES = [
  { i: 1, key: "transcribe.steps.prepare",         fallback: "Preparando audio" },
  { i: 2, key: "transcribe.steps.lyrics_lookup",   fallback: "Buscando letra" },
  { i: 3, key: "transcribe.steps.isolate_vocals",  fallback: "Aislando voz" },
  { i: 4, key: "transcribe.steps.align",           fallback: "Alineando con la canción" },
  { i: 5, key: "transcribe.steps.done",            fallback: "Finalizando" },
];

// Rough typical durations per stage in seconds (sum ≈ 120s in good case).
// Used purely for ETA display. Telemetry-tuned later.
const STAGE_DURATIONS_S = [3, 4, 60, 50, 5];

function activeStageFromState(currentStep, progress) {
  if (currentStep && STAGE_BY_BACKEND_LABEL[currentStep] != null) {
    return STAGE_BY_BACKEND_LABEL[currentStep];
  }
  // Progress-based fallback when the label isn't recognised.
  if (progress >= 100) return 5;
  if (progress >= 85)  return 5;
  if (progress >= 50)  return 4;
  if (progress >= 25)  return 3;
  if (progress >= 15)  return 2;
  return 1;
}

function etaSeconds(activeStage, progress) {
  // Time remaining = full duration of remaining stages + leftover of current.
  let remaining = 0;
  for (let s = activeStage; s <= 5; s++) {
    remaining += STAGE_DURATIONS_S[s - 1];
  }
  // Subtract a rough fraction of the active stage that's already done.
  const stageStartPct = [0, 15, 25, 50, 85][activeStage - 1] ?? 0;
  const stageEndPct   = [15, 25, 50, 85, 100][activeStage - 1] ?? 100;
  const stageSpan = Math.max(1, stageEndPct - stageStartPct);
  const stageProgress = Math.min(1, Math.max(0, (progress - stageStartPct) / stageSpan));
  remaining -= Math.floor(STAGE_DURATIONS_S[activeStage - 1] * stageProgress);
  return Math.max(5, remaining);
}

export default function TranscribingProgress({
  jobId,
  api,
  token,
  t,                     // i18n hook from useI18n()
  fileName = "",
  queueIndex = 1,
  queueTotal = 1,
}) {
  const { currentStep, progress } = useJobProgress(jobId, { api, token });
  const active = activeStageFromState(currentStep, progress);
  const pct = Math.max(0, Math.min(100, Math.round(progress || 0)));

  // INCIDENT (2026-05-24): the ETA used to display `etaSeconds(active,
  // progress)` directly. That value is STATIC — it only changes when
  // `progress` or `active` changes. A stuck job (backend not emitting
  // updates) left the ETA frozen at the same number ("122s y no se
  // mueve" was the operator's exact complaint). Two fixes:
  //
  // 1. Real countdown via `useState + setInterval`: decrement the
  //    displayed ETA by 1 every second on the client. Re-syncs to
  //    `etaSeconds(...)` whenever progress changes (so it never goes
  //    out of sync with the real backend state for long).
  //
  // 2. Stuck detection: track the last time `progress` advanced. If
  //    >= 45 s without movement, show a "Tardando más de lo normal"
  //    hint — usually means Replicate is degraded. Operator at least
  //    knows the system isn't lying.
  const STUCK_AFTER_S = 45;
  const [displayEta, setDisplayEta] = useState(() => etaSeconds(active, progress));
  const [stuck, setStuck] = useState(false);
  const lastProgressRef = useRef({ value: progress, t: Date.now() });

  // Re-sync ETA + reset stuck timer whenever real progress moves.
  useEffect(() => {
    if (progress !== lastProgressRef.current.value) {
      lastProgressRef.current = { value: progress, t: Date.now() };
      setStuck(false);
      setDisplayEta(etaSeconds(active, progress));
    }
  }, [progress, active]);

  // 1-second tick: countdown the displayed ETA + flag stuck.
  useEffect(() => {
    if (active >= 5) return undefined;        // finalising — no countdown
    const id = setInterval(() => {
      setDisplayEta((prev) => Math.max(0, prev - 1));
      const idle = (Date.now() - lastProgressRef.current.t) / 1000;
      if (idle >= STUCK_AFTER_S) setStuck(true);
    }, 1000);
    return () => clearInterval(id);
  }, [active]);

  return (
    <div className="w-full max-w-md mx-auto mt-12 animate-fade-in">
      {/* Title + queue position */}
      <div className="text-center mb-6">
        <h2 className="text-lg font-bold text-white">
          {t("transcribe.title") || "Transcribiendo lyrics"}
        </h2>
        {fileName && (
          <p className="text-xs text-gray-500 mt-1 truncate">{fileName}</p>
        )}
        {queueTotal > 1 && (
          <p className="text-[11px] text-gray-600 mt-1">
            {t("transcribe.song") || "Canción"} {queueIndex} {t("editor.song_of") || "de"} {queueTotal}
          </p>
        )}
      </div>

      {/* Top progress bar */}
      <div className="h-1.5 bg-surface-1 rounded-full overflow-hidden mb-1">
        <div
          className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-[11px] text-gray-600 text-right mb-6">{pct}%</p>

      {/* Stages list */}
      <ol className="space-y-3">
        {STAGES.map((s) => {
          const state =
            s.i < active ? "done"
            : s.i === active ? "active"
            : "pending";
          const label = (t && t(s.key)) || s.fallback;
          return (
            <li key={s.i} className="flex items-center gap-3">
              {/* Icon */}
              <span
                className={
                  "w-6 h-6 rounded-full flex items-center justify-center transition-all duration-500 " +
                  (state === "done"
                    ? "bg-brand/20 text-brand"
                    : state === "active"
                      ? "bg-brand text-white shadow-[0_0_0_4px_rgba(124,58,237,0.18)]"
                      : "bg-surface-1 text-gray-600 ring-1 ring-white/[0.06]")
                }
              >
                {state === "done" ? (
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                ) : state === "active" ? (
                  <span className="w-2 h-2 rounded-full bg-white animate-pulse" />
                ) : (
                  <span className="w-1.5 h-1.5 rounded-full bg-current opacity-60" />
                )}
              </span>
              {/* Label */}
              <span
                className={
                  "text-sm transition-colors duration-500 " +
                  (state === "done"
                    ? "text-gray-400"
                    : state === "active"
                      ? "text-white font-medium"
                      : "text-gray-600")
                }
              >
                {label}
              </span>
            </li>
          );
        })}
      </ol>

      {/* ETA: client-side countdown (re-syncs on every progress move) */}
      {active < 5 && (
        <p className="text-[11px] text-gray-600 text-center mt-6">
          {(t && t("transcribe.eta", { seconds: displayEta })) || `Tiempo restante: ~${displayEta}s`}
        </p>
      )}
      {/* Stuck hint: shown when backend hasn't moved progress for >= 45s.
          Tells the operator the system isn't lying — Replicate is slow. */}
      {stuck && active < 5 && (
        <p className="text-[11px] text-amber-400/80 text-center mt-2">
          {(t && t("transcribe.stuck")) ||
            "Esto está tomando más tiempo de lo normal. Nuestro motor de transcripción está procesando muchas canciones — refrescá en un minuto o cancelá y reintentá."}
        </p>
      )}
    </div>
  );
}
