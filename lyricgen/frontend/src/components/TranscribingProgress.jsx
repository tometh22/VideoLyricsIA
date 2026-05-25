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

// Rough typical durations per stage in seconds (sum ≈ 200 s p50).
// Used purely for ETA display. Telemetry-tuned 2026-05-24 from real
// jobs — the original numbers (60/50 for stages 3/4) were optimistic
// p50s; demucs alone hits 90-120 s in cold-start. Result was ETA
// hitting 0 well before demucs finished and the user seeing "viene
// muy lento". These numbers reflect the actual p50 wallclock.
const STAGE_DURATIONS_S = [3, 8, 100, 75, 5];

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
  //    >= threshold without movement, show a "Tardando más de lo
  //    normal" hint — usually means Replicate is degraded.
  //
  // INCIDENT (2026-05-24 #2): the original `STUCK_AFTER_S = 45` was
  // wrong for "Aislando voz" (demucs). The backend emits `progress=25`
  // at the START of demucs, then runs 60-180 s WITHOUT emitting any
  // intermediate progress (next `_step` jumps straight to 55%). So
  // every run hit the 45 s threshold during demucs and showed a fake
  // "viene muy lento" hint even when the system was healthy.
  //
  // Fix: per-stage threshold. The thresholds reflect the REAL p95
  // wallclock of each stage in healthy state — anything over that
  // means something IS wrong.
  //
  //   prepare       (stage 1): 30 s — ffmpeg/probe locally; should be
  //                              quick. >30 s means I/O contention.
  //   lyrics_lookup (stage 2): 30 s — lrclib + (optional) Gemini.
  //                              Both have 8 s timeouts; >30 s means
  //                              all 3 tries failed.
  //   isolate_vocals(stage 3): 150 s — demucs typical p95 is 90-120 s
  //                              + cache check. 150 s buffer covers
  //                              cold-start + first retry.
  //   align         (stage 4): 90 s — FA budget 60 s + warm whisperX
  //                              race + lrclib lookup. >90 s suggests
  //                              cascade or non-retryable error.
  //   done          (stage 5): no hint (already finalising).
  const STUCK_AFTER_BY_STAGE = [30, 30, 150, 90, 9999];
  const STUCK_AFTER_S = STUCK_AFTER_BY_STAGE[active - 1] ?? 45;
  const [displayEta, setDisplayEta] = useState(() => etaSeconds(active, progress));
  const [stuck, setStuck] = useState(false);
  const lastProgressRef = useRef({ value: progress, t: Date.now() });

  // Rotating sub-label for the "Aislando voz" stage (the long one).
  // Demucs runs on Replicate; the backend polls and emits intermediate
  // progress, but the bar can still spend 60-120 s in this single stage.
  // A 4-phrase cycle keeps the screen visually alive without overclaiming
  // step granularity ("Procesando…" → "Separando…" → "Aislando…" →
  // "Refinando…"). Re-mounted on every change so the CSS keyframe fires
  // its in/out fade fresh. Only active on stage 3 (other stages are fast).
  const SUBLABEL_KEYS = [
    "transcribe.isolate_vocals.sub1",
    "transcribe.isolate_vocals.sub2",
    "transcribe.isolate_vocals.sub3",
    "transcribe.isolate_vocals.sub4",
  ];
  const SUBLABEL_FALLBACKS = [
    "Procesando audio en la nube…",
    "Separando instrumentos de la voz…",
    "Aislando frecuencias vocales…",
    "Refinando el stem…",
  ];
  const [sublabelIdx, setSublabelIdx] = useState(0);
  useEffect(() => {
    if (active !== 3) return undefined;
    setSublabelIdx(0);
    const id = setInterval(() => {
      setSublabelIdx((i) => (i + 1) % SUBLABEL_KEYS.length);
    }, 3600);
    return () => clearInterval(id);
  }, [active]);

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

      {/* Top progress bar. The shimmer overlay (active < 5) sits on top
          of the filled portion and travels left-to-right continuously,
          giving the bar a "live" feel between real progress ticks. The
          width transitions over 700ms with an ease-out curve so each
          tick from the backend (every 3s during demucs) glides instead
          of stepping. */}
      <div className="relative h-1.5 bg-surface-1 rounded-full overflow-hidden mb-1">
        <div
          className={
            "relative h-full rounded-full bg-gradient-to-r from-brand to-brand-light " +
            "transition-[width] duration-700 ease-out " +
            (active < 5 ? "progress-shimmer" : "")
          }
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-[11px] text-gray-600 text-right mb-6">{pct}%</p>

      {/* Rotating sub-label, only during "Aislando voz" (the long stage).
          Fixed height so the layout never shifts when the phrase changes;
          a CSS keyframe fades each phrase in and out over 3.6s. */}
      {active === 3 && (
        <div className="h-5 text-center -mt-3 mb-4">
          <span
            key={sublabelIdx}
            className="sublabel-fade text-[12px] text-brand-light/80 tracking-wide"
          >
            {(t && t(SUBLABEL_KEYS[sublabelIdx])) || SUBLABEL_FALLBACKS[sublabelIdx]}
          </span>
        </div>
      )}

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
