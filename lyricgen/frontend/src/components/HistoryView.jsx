import { useState, useMemo, useEffect, useRef, useCallback, memo } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import { useLazyMediaUrl, useMediaUrl } from "../mediaUrl";
import MediaPreview from "./MediaPreview";
// Reusing the hero dropzone styles from the Dashboard's empty state so the
// app feels consistent when you land on an empty page (Inicio vs Historial).
import "./DashboardRich/DashboardRich.css";

const API = import.meta.env.VITE_API_URL || "";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

// Hora absoluta en zona AR para tooltips (cuando el operador necesita
// fecha exacta para reportar a UMG: "el video del 22 mayo 14:30").
function absoluteTimeAR(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return new Intl.DateTimeFormat("es-AR", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"
  }).format(d);
}

// 2026-05-27 perf audit: module-level formatters so we don't
// `new Intl.DateTimeFormat()` per bucketByDateAR() call. The history
// view re-buckets on every history mutation (SSE poll tick + filter
// change + sort change); each formatter creation is ~5-10 ms in
// Chrome so this saved ~20-30 ms per render for 200-item lists.
const BUCKET_DATE_FMT_AR = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Argentina/Buenos_Aires",
  year: "numeric", month: "2-digit", day: "2-digit",
});
const BUCKET_MONTH_FMT_AR = new Intl.DateTimeFormat("es-AR", {
  timeZone: "America/Argentina/Buenos_Aires",
  month: "long", year: "numeric",
});

// 2026-05-25 PR-3 — bucketing temporal por timezone AR (no UTC). El
// operador piensa en bloques cronológicos ("Hoy", "Esta semana") y
// scrolea por ahí, no por una lista plana de 200 cards. Returns lista
// ordenada de {key, label, jobs[]} para renderizar con sticky headers.
function bucketByDateAR(jobs) {
  if (!jobs?.length) return [];
  // Anclas calculadas una sola vez (no por job)
  const now = new Date();
  const todayAR = BUCKET_DATE_FMT_AR.format(now);
  const yesterdayAR = (() => {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    return BUCKET_DATE_FMT_AR.format(y);
  })();
  const startOfWeekAR = (() => {
    const d = new Date(now);
    const dayIdx = d.getDay() === 0 ? 6 : d.getDay() - 1; // Lunes = inicio
    d.setDate(d.getDate() - dayIdx);
    return BUCKET_DATE_FMT_AR.format(d);
  })();

  const buckets = new Map(); // ordered insertion
  for (const job of jobs) {
    const ts = job.created_at;
    if (!ts) continue;
    const d = new Date(ts * 1000);
    const dateAR = BUCKET_DATE_FMT_AR.format(d);
    let key, label;
    if (dateAR === todayAR) { key = "0-today"; label = "Hoy"; }
    else if (dateAR === yesterdayAR) { key = "1-yesterday"; label = "Ayer"; }
    else if (dateAR >= startOfWeekAR) { key = "2-this-week"; label = "Esta semana"; }
    else {
      const monthLabel = BUCKET_MONTH_FMT_AR.format(d);
      // Mes/año como key para que jobs del mismo mes caigan al mismo
      // bucket. Capitalize primera letra ("Mayo 2026").
      key = "3-" + monthLabel.replace(/\s+/g, "-").toLowerCase();
      label = monthLabel.charAt(0).toUpperCase() + monthLabel.slice(1);
    }
    if (!buckets.has(key)) buckets.set(key, { key, label, jobs: [] });
    buckets.get(key).jobs.push(job);
  }
  // Orden: today → yesterday → this week → meses (sort desc por timestamp del primer job)
  const out = [];
  ["0-today", "1-yesterday", "2-this-week"].forEach((k) => {
    if (buckets.has(k)) { out.push(buckets.get(k)); buckets.delete(k); }
  });
  // Resto (meses) ordenados desc por timestamp del job más reciente del bucket
  const remaining = [...buckets.values()].sort((a, b) =>
    (b.jobs[0]?.created_at || 0) - (a.jobs[0]?.created_at || 0)
  );
  return [...out, ...remaining];
}

function sortJobs(jobs, sortKey) {
  const arr = [...(jobs || [])];
  switch (sortKey) {
    case "oldest":
      return arr.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    case "artist_asc":
      return arr.sort((a, b) =>
        (a.artist || "").localeCompare(b.artist || "", "es-AR")
      );
    case "artist_desc":
      return arr.sort((a, b) =>
        (b.artist || "").localeCompare(a.artist || "", "es-AR")
      );
    case "newest":
    default:
      return arr.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }
}

// Persiste preferencias del operador (view + sort) entre sesiones.
// Linear/Notion pattern — defaults razonables, opt-in a custom.
function useLocalStorage(key, defaultValue) {
  const [value, setValue] = useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v !== null ? v : defaultValue;
    } catch (_) { return defaultValue; }
  });
  useEffect(() => {
    try { localStorage.setItem(key, value); } catch (_) {}
  }, [key, value]);
  return [value, setValue];
}

// Mini status indicator para la tabla densa (dot 8px + label opcional).
// No usa el StatusBadge full porque ocupa demasiado ancho en 44px row.
function StatusDot({ status }) {
  const map = {
    done:                 "bg-accent",
    pending_review:       "bg-amber-400",
    processing:           "bg-brand animate-pulse",
    queued:               "bg-gray-500",
    editing:              "bg-brand animate-pulse",
    transcribed:          "bg-gray-400",
    transcribed_pending:  "bg-gray-400",
    transcribing:         "bg-brand animate-pulse",
    transcribing_queued:  "bg-gray-500",
    awaiting_upload:      "bg-gray-500",
    transcription_failed: "bg-red-400",
    error:                "bg-red-400",
    validation_failed:    "bg-red-400",
    rejected:             "bg-red-400",
    bg_preview_queued:    "bg-gray-500",
    bg_preview_failed:    "bg-red-400",
  };
  return <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${map[status] || "bg-gray-500"}`} />;
}

function FilterPill({ active, count, onClick, children }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`flex items-center gap-2 h-9 px-4 rounded-full text-xs font-medium transition-all ${
        active
          ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
          : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
      }`}
    >
      {children}
      <span className={`text-[10px] tabular-nums ${active ? "text-brand-light/80" : "text-gray-500"}`}>
        {count}
      </span>
    </button>
  );
}

function StatusBadge({ status, t }) {
  const map = {
    done:               { label: t("history.done"),                       cls: "bg-accent/15 text-accent ring-1 ring-accent/30" },
    pending_review:     { label: t("batch.pending_review") || "Pendiente de revisión",  cls: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30" },
    processing:         { label: t("history.processing"),                 cls: "bg-brand/15 text-brand-light ring-1 ring-brand/30" },
    queued:             { label: "En cola",                                cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06]" },
    // Borrador transcripto que aún no se generó (o subida en curso). NO es
    // un render colgado — antes caía al default "Procesando" y parecía
    // trabado. Estilo neutro (no spinner) para distinguirlo de un render.
    transcribed_pending: { label: t("history.draft") || "Sin generar",       cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06] border border-dashed border-white/10" },
    // INCIDENT (audit 2026-05-24): historical jobs with `transcribed`,
    // `transcribing`, `transcribing_queued` weren't in the map → all
    // fell through to "Procesando", making 60 stale drafts look like
    // 60 stuck renders. The "Procesando" UI also lacks a "click to
    // continue" affordance — operator thought he had to wait for
    // hours.
    //
    // `transcribed` = transcripción terminó, operador NO dió "Generar
    // video". Es un draft listo para editar, no un render en curso.
    // Mismo estilo que `transcribed_pending` (dashed border, neutral)
    // pero etiqueta distinta para distinguir "subida en curso" de
    // "ya transcripto, abre y editá".
    transcribed:        { label: "Listo p/ editar",                        cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06] border border-dashed border-white/10" },
    transcribing:       { label: "Transcribiendo…",                        cls: "bg-brand/15 text-brand-light ring-1 ring-brand/30" },
    transcribing_queued: { label: "En cola",                                cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06]" },
    transcription_failed: { label: t("history.error") || "Error transcribiendo", cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
    awaiting_upload:    { label: t("history.uploading") || "Subiendo…",      cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06]" },
    editing:            { label: t("history.editing") || "Re-renderizando", cls: "bg-brand/15 text-brand-light ring-1 ring-brand/30" },
    error:              { label: t("history.error"),                      cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
    validation_failed:  { label: t("batch.validation_failed") || "Failed", cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
    rejected:           { label: "Rechazado",                              cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
    // bg_preview ghost jobs (Capa C feature) — no audio, no video.
    // Should NOT appear in the user's history normally (filter elsewhere),
    // but if one does, label honestly instead of falling through to
    // "Procesando".
    bg_preview_queued:  { label: "Fondo en preparación",                   cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06]" },
    bg_preview_failed:  { label: "Falló la preparación del fondo",         cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
  };
  // Honest fallback: instead of pretending an unknown status is
  // "Procesando", show the raw value in muted gray so the operator
  // can report a real string back. Caught 19/60 mislabeled jobs on
  // staging 2026-05-24.
  const cfg = map[status] || {
    label: status || "Desconocido",
    cls: "bg-surface-3/40 text-ink-secondary/60 ring-1 ring-white/[0.04]",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium backdrop-blur-md ${cfg.cls}`}>
      {cfg.label}
    </span>
  );
}

// Mantener sincronizado con backend/jobs.py:_DELETABLE_STATUSES.
// "editing" y "transcribed_pending" se agregaron para que el operador
// pueda matar jobs colgados sin pedirle al admin que actualice la DB.
const DELETABLE = new Set([
  "processing", "queued", "error", "validation_failed",
  "editing", "transcribed_pending",
]);

function VideoCardImpl({ job, onSelect, onDelete, selected, onToggleSelect, t }) {
  // Prefer the structured fields the operator filled in / the backend
  // backfilled from the filename. Fall back to the legacy filename split
  // only for jobs that pre-date the song_title column.
  const fallbackName = (job.filename || "").replace(/\.(mp3|wav|m4a|flac|aac|ogg)$/i, "");
  let songName = (job.song_title || "").trim();
  let artistName = (job.artist || "").trim();
  if (!songName) {
    if (fallbackName.includes(" - ")) {
      songName = fallbackName.split(" - ").slice(1).join(" - ");
    } else if (fallbackName.includes("_")) {
      songName = fallbackName.split("_")[0];
    } else {
      songName = fallbackName;
    }
  }
  if (!artistName) {
    if (fallbackName.includes(" - ")) {
      artistName = fallbackName.split(" - ")[0];
    } else if (fallbackName.includes("_")) {
      artistName = fallbackName.split("_").slice(1).join("_");
    }
  }
  // Show thumbnail for editing too — there's a prior rendered video on R2
  // (the user is requesting a re-render OF an existing video) so the
  // thumbnail is real, just temporarily stale.
  const showThumb = job.status === "done" || job.status === "pending_review" || job.status === "editing";
  // 2026-05-27 Phase-3 audit: lazy mode — only fetches the /media-token
  // once the card enters the viewport (200 px rootMargin so the
  // thumbnail usually finishes loading before the user actually sees
  // the card). For a 200-job history the operator who only scrolls to
  // card 20 saves 180 network round-trips.
  const { url: thumbSrc, ref: cardRef } = useLazyMediaUrl(
    showThumb ? job.job_id : "",
    "thumbnail",
    "preview",
    // version: edits overwrite the same R2 thumbnail key — bust the URL when
    // the render changes so the card doesn't keep the pre-edit image.
    { version: `${job.edit_count || 0}-${job.status || ""}` },
  );
  const canDelete = DELETABLE.has(job.status);

  const handleDelete = (e) => {
    e.stopPropagation();
    const songLabel = songName || job.filename || job.job_id;
    if (!confirm(`¿Eliminar "${songLabel}"? Esta acción no se puede deshacer.`)) return;
    onDelete(job.job_id);
  };

  const handleToggle = (e) => {
    e.stopPropagation();
    onToggleSelect(job.job_id);
  };

  return (
    <div
      ref={cardRef}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(job.job_id, job.status)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onSelect(job.job_id, job.status); }}
      className={`rounded-card overflow-hidden text-left group bg-surface-2/40 hover:bg-surface-2/70 ring-1 ring-white/[0.04] hover:ring-white/[0.10] transition-all cursor-pointer focus:outline-none focus:ring-brand/40
        ${selected ? "ring-2 ring-brand/60" : ""}`}
    >
      <div className="aspect-video bg-surface-3/30 relative overflow-hidden">
        {showThumb ? (
          <MediaPreview src={thumbSrc} status={job.status} className="absolute inset-0" alt={`Preview de ${songName || "video"}`} imageClassName="group-hover:scale-[1.04] transition-transform duration-500">
            {/* Re-render in progress sobre el thumb stale (status=editing) */}
            {job.status === "editing" && (
              <div className="absolute z-[2] inset-0 flex items-center justify-center bg-black/30 backdrop-blur-[1px]">
                <div className="w-7 h-7 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              </div>
            )}
          </MediaPreview>
        ) : (job.status === "error" || job.status === "validation_failed") ? (
          <div className="absolute inset-0 flex items-center justify-center bg-red-500/[0.04]">
            <svg className="w-7 h-7 text-red-400/60" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>
            </svg>
          </div>
        ) : (
          // Placeholder bonito (aurora + equalizer) — antes era un spinner
          // pelado sobre bg-surface-3/30 vacío. Mismo componente que MediaRow.
          <ProcessingThumbnail status={job.status} />
        )}

        <div className="absolute top-2.5 right-2.5">
          <StatusBadge status={job.status} t={t} />
        </div>

        {/* Multi-select checkbox + single-delete button — only on stuck/
            failed rows. Checkbox stays visible when ANY row is selected
            (so the operator sees the selection state at a glance) but
            otherwise reveals on hover like the trash icon. */}
        {canDelete && (
          <>
            <button
              type="button"
              onClick={handleToggle}
              className={`absolute top-2.5 left-2.5 w-6 h-6 rounded-md border-2 backdrop-blur-md
                flex items-center justify-center transition-all
                ${selected
                  ? "bg-brand border-brand opacity-100"
                  : "bg-black/50 border-white/40 hover:border-brand/70 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"}`}
              title={selected ? "Deseleccionar" : "Seleccionar para eliminar"}
              aria-label="Seleccionar"
              aria-pressed={selected}
            >
              {selected && (
                <svg className="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12"/>
                </svg>
              )}
            </button>
            <button
              type="button"
              onClick={handleDelete}
              className="absolute top-2.5 left-11 w-7 h-7 rounded-lg bg-black/50 hover:bg-red-500/80 backdrop-blur-md
                text-white/70 hover:text-white opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 transition-all
                flex items-center justify-center ring-1 ring-white/10 hover:ring-red-400/50"
              title={t("history.delete") || "Eliminar"}
              aria-label="Eliminar video"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6M10 11v6M14 11v6M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
            </button>
          </>
        )}

        {(job.status === "done" || job.status === "pending_review") && (
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
            <div className="w-11 h-11 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
              <svg className="w-4 h-4 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z"/>
              </svg>
            </div>
          </div>
        )}
      </div>

      <div className="px-3.5 py-3">
        <div className="flex items-start gap-2 min-w-0">
          <p className="text-[13px] font-medium text-white truncate flex-1 min-w-0">{songName || "Sin nombre"}</p>
          {/* ProResBadge intentionally NOT rendered here — it competes
              with StatusBadge ("Listo") on the thumbnail and reads as a
              contradictory status to the operator ("Listo" + "Generando
              ProRes…" at the same time = confusing). The ProRes prewarm
              is operational (caching for fast UMG download), not user-
              actionable from the list view. JobDetail surfaces ProRes
              state next to the download buttons, which is where it's
              relevant (the next click depends on it). */}
        </div>
        <p className="text-[11px] text-gray-500 truncate mt-0.5">
          {artistName}
          {job.created_at && <span className="ml-1.5 text-gray-600">· {timeAgo(job.created_at)}</span>}
        </p>
      </div>
    </div>
  );
}

// 2026-05-27 Phase-3 audit: VideoCard is rendered N times in HistoryView
// (one per job, up to 200). Without memoization every history mutation
// (one card status changing) re-renders ALL of them — wasting time
// re-running the song/artist parsing + producing a flicker on the
// virtualized lists. Memo with a custom comparator that ignores the
// onSelect/onDelete/onToggleSelect callbacks (which are stable closures
// in HistoryView's body) keeps re-renders to the cards that actually
// changed.
const VideoCard = memo(VideoCardImpl, (prev, next) => (
  prev.job === next.job &&
  prev.selected === next.selected &&
  prev.t === next.t
));

// ProcessingThumbnail — aurora + equalizer placeholder para jobs sin
// frame renderizado todavía. Operator feedback 2026-05-25: "las preview
// en historial no tienen miniatura (las que están procesando), creemos
// una linda para esas". Tres tiers visuales según status:
//   active  → processing / editing / transcribing   (brand violet, bars rápidas + shine)
//   waiting → queued / *_queued / awaiting_upload   (gris suave, bars lentas)
//   draft   → transcribed / transcribed_pending     (nota musical estática)
// Renderiza absoluto sobre el parent → funciona en 120×70 y aspect-video.
// Keyframes inline (mismo patrón que BatchProgress/WizardLivePreview).
function ProcessingThumbnail({ status }) {
  const isActive  = status === "processing" || status === "editing" || status === "transcribing";
  const isWaiting = status === "queued" || status === "transcribing_queued" || status === "awaiting_upload";
  const isStatic  = !isActive && !isWaiting;
  const barColor  = isActive ? "bg-brand-light" : isWaiting ? "bg-white/40" : "bg-white/20";
  const period    = isActive ? 850 : 1500;
  const peaks     = [40, 70, 100, 60, 35];
  return (
    <>
      <div className={`absolute inset-0 bg-gradient-to-br ${
        isActive  ? "from-brand/25 via-brand/[0.06] to-accent/10"
        : isWaiting ? "from-brand/[0.10] via-transparent to-white/[0.02]"
        : "from-white/[0.04] via-transparent to-white/[0.02]"
      }`} />
      <div className={`absolute -top-5 -left-5 w-16 h-16 rounded-full blur-2xl ${
        isActive ? "bg-brand/30" : isWaiting ? "bg-brand/15" : "bg-white/[0.04]"
      }`} />
      <div className={`absolute -bottom-5 -right-5 w-14 h-14 rounded-full blur-2xl ${
        isActive ? "bg-accent/20" : "bg-white/[0.03]"
      }`} />
      {isActive && (
        <div
          className="pointer-events-none absolute inset-y-0 -left-1/4 w-1/3 bg-gradient-to-r from-transparent via-white/10 to-transparent"
          style={{ animation: "hist-thumb-shine 3.2s ease-in-out infinite" }}
        />
      )}
      <div className="absolute inset-0 flex items-center justify-center">
        {isStatic ? (
          <svg className="w-5 h-5 text-white/30" fill="none" stroke="currentColor" strokeWidth="1.4" viewBox="0 0 24 24">
            <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
          </svg>
        ) : (
          <div className="flex items-end gap-[3px] h-7">
            {peaks.map((peak, i) => (
              <span
                key={i}
                className={`block w-[3px] rounded-full ${barColor}`}
                style={{
                  height: `${peak}%`,
                  transformOrigin: "bottom",
                  animation: `hist-eq-bounce ${period}ms ease-in-out ${i * 110}ms infinite`,
                }}
              />
            ))}
          </div>
        )}
      </div>
      <style>{`
        @keyframes hist-eq-bounce {
          0%, 100% { transform: scaleY(0.2); opacity: 0.55; }
          50%      { transform: scaleY(1);   opacity: 1; }
        }
        @keyframes hist-thumb-shine {
          0%   { transform: translateX(-160%) skewX(-12deg); }
          100% { transform: translateX(260%)  skewX(-12deg); }
        }
      `}</style>
    </>
  );
}

// MediaThumbnail — 120×70 aspect-video YouTube Studio style.
// Antes era MiniThumbnail 40×24 pero parecía tabla de log. Operator
// feedback 2026-05-25: "el historial parece backend, debería ser más
// lindo, sumarle thumbnails". Aumentamos 3× el tamaño + agregamos
// ring + overlay play sobre la card completa.
function MediaThumbnail({ jobId, status, editCount = 0 }) {
  // Solo pedir el thumbnail real para statuses que pueden tenerlo en R2.
  // Para los demás (processing/queued/etc), saltearse el /media-token
  // request y dibujar el ProcessingThumbnail directamente.
  const hasMedia = status === "done" || status === "pending_review" || status === "editing";
  // 2026-05-27 Phase-3 audit: lazy mode — token only fetched when the
  // thumbnail container intersects the viewport. The ref attaches to
  // the outer 120×70 wrapper.
  const { url: src, ref: thumbRef } = useLazyMediaUrl(
    hasMedia ? jobId : "",
    "thumbnail",
    "preview",
    // version: bust the URL when an edit re-renders (same R2 key overwritten).
    { version: `${editCount}-${status || ""}` },
  );
  const isReady = status === "done" || status === "pending_review";
  return (
    <MediaPreview ref={thumbRef} src={hasMedia ? src : ""} status={status} className="w-[120px] h-[70px] shrink-0 rounded-lg group/thumb">
      {isReady && src && (
        <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover/thumb:opacity-100 bg-black/40 transition-opacity">
          <div className="w-7 h-7 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
            <svg className="w-3 h-3 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z" />
            </svg>
          </div>
        </div>
      )}
    </MediaPreview>
  );
}

// 2026-05-25 v2 (operator feedback): TableRow → MediaRow estilo
// YouTube Studio. Thumb grande 120×70 a la izquierda, dos líneas de
// texto generosas en el centro, badge + time + acción a la derecha.
// Padding más respiratorio (~90px de altura total). Hover ring +
// fade-in del botón acción. Click en row → JobDetail.
function MediaRowImpl({ job, onSelect, onDelete, selected, onToggleSelect, t }) {
  const fallbackName = (job.filename || "").replace(/\.(mp3|wav|m4a|flac|aac|ogg)$/i, "");
  let songName = (job.song_title || "").trim();
  let artistName = (job.artist || "").trim();
  if (!songName && fallbackName.includes(" - ")) {
    songName = fallbackName.split(" - ").slice(1).join(" - ").trim();
    if (!artistName) artistName = fallbackName.split(" - ")[0].trim();
  } else if (!songName) {
    songName = fallbackName;
  }
  const canDelete = DELETABLE.has(job.status);
  const isPending = job.status === "pending_review";
  const isError = job.status === "error" || job.status === "transcription_failed";

  const handleRowClick = () => onSelect?.(job.job_id, job.status);

  return (
    <div
      className="history-media-row group relative"
    >
      <button
        type="button"
        className="history-media-row__open-hit"
        onClick={handleRowClick}
        aria-label={`Abrir ${artistName || "video"} — ${songName || "sin nombre"}. Estado ${job.status}. Creado ${timeAgo(job.created_at)}`}
      />
      {/* Checkbox — flotante arriba-izq del thumb, solo visible on hover
          o si seleccionado. NO interfiere con el layout cuando no se usa. */}
      {canDelete && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onToggleSelect(job.job_id); }}
          className={`absolute top-2 left-2 z-10 w-5 h-5 rounded border flex items-center justify-center backdrop-blur-md transition-all
            ${selected
              ? "bg-brand border-brand opacity-100"
              : "bg-black/40 border-white/30 hover:border-brand/70 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
            }`}
          aria-label={selected ? "Deseleccionar" : "Seleccionar"}
        >
          {selected && (
            <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          )}
        </button>
      )}

      {/* Media thumbnail (operator feedback 2026-05-25: "sumarle
          thumbnails a los videos"). 120×70 aspect-video, YouTube Studio. */}
      <MediaThumbnail jobId={job.job_id} status={job.status} editCount={job.edit_count || 0} />

      {/* Texto central — 2 líneas generosas */}
      <div className="history-media-row__title">
        <p className="text-[15px] font-semibold text-white truncate leading-tight" title={artistName}>
          {artistName || <span className="text-gray-600 font-normal">—</span>}
        </p>
        <p className="text-[13px] text-ink-secondary truncate leading-tight" title={songName}>
          {songName || <span className="text-gray-600">(sin nombre)</span>}
        </p>
        <div className="history-media-row__mobile-meta">
          <StatusDot status={job.status} />
          <StatusBadge status={job.status} t={t} />
          <span
            className="text-[11px] text-gray-500 font-mono tabular-nums"
            title={absoluteTimeAR(job.created_at)}
          >
            · {timeAgo(job.created_at)}
          </span>
        </div>
      </div>

      <div className="history-media-row__status">
        <StatusDot status={job.status} />
        <StatusBadge status={job.status} t={t} />
      </div>

      <time className="history-media-row__date" title={absoluteTimeAR(job.created_at)}>
        {timeAgo(job.created_at)}
      </time>

      {/* Action button derecha — contextual según status */}
      <div
        className="history-media-row__action"
        onClick={(e) => e.stopPropagation()}
      >
        {isPending ? (
          <button
            type="button"
            onClick={handleRowClick}
            className="flex items-center gap-1.5 px-3 h-9 rounded-lg bg-amber-500/15 ring-1 ring-amber-500/30 text-[12px] text-amber-200 hover:bg-amber-500/25 transition-colors"
          >
            Revisar
            <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        ) : job.status === "done" ? (
          <button
            type="button"
            onClick={handleRowClick}
            className="flex items-center gap-1.5 px-3 h-9 rounded-lg bg-accent/15 ring-1 ring-accent/30 text-[12px] text-accent hover:bg-accent/25 transition-colors opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Abrir
          </button>
        ) : isError && canDelete && onDelete ? (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); if (confirm("¿Eliminar este job fallido?")) onDelete(job.job_id); }}
            className="flex items-center gap-1.5 px-3 h-9 rounded-lg bg-white/[0.04] ring-1 ring-white/[0.06] text-[12px] text-red-400/80 hover:bg-red-500/15 hover:text-red-300 transition-colors opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Eliminar
          </button>
        ) : (
          <span className="text-gray-500 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 transition-opacity">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        )}
      </div>
    </div>
  );
}

// Same memoization rationale as VideoCard above — MediaRow is rendered
// up to 200× in table view, and per-row updates shouldn't fan out to
// the entire bucket.
const MediaRow = memo(MediaRowImpl, (prev, next) => (
  prev.job === next.job &&
  prev.selected === next.selected &&
  prev.t === next.t
));


const FILTERS = [
  { id: "all",     label: "Todos",     match: () => true },
  { id: "done",    label: "Listos",    match: (j) => j.status === "done" },
  // "Pendientes" now includes BOTH the broadcast-review queue
  // (pending_review) AND drafts the operator finished transcribing but
  // hasn't generated yet (transcribed / transcribed_pending). Pre-fix
  // those drafts were invisible — bucketed as "Procesando" with no
  // affordance to act on them.
  { id: "pending", label: "Pendientes", match: (j) => j.status === "pending_review" || j.status === "transcribed" || j.status === "transcribed_pending" },
  // "En curso" includes ACTIVE work the user can't yet act on:
  // transcription in flight, render in flight, edits being re-rendered.
  { id: "active",  label: "En curso",  match: (j) => j.status === "processing" || j.status === "queued" || j.status === "editing" || j.status === "transcribing" || j.status === "transcribing_queued" || j.status === "awaiting_upload" },
  { id: "failed",  label: "Fallidos",  match: (j) => j.status === "error" || j.status === "validation_failed" || j.status === "transcription_failed" || j.status === "rejected" },
];

function HistoryInspector({ job, onClose, onOpen, t }) {
  const thumbSrc = useMediaUrl(job?.job_id || "", "thumbnail", "preview");
  const closeRef = useRef(null);
  const panelRef = useRef(null);
  const previousFocusRef = useRef(null);
  const [modalLayout, setModalLayout] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 1100px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const sync = (event) => setModalLayout(event.matches);
    media.addEventListener?.("change", sync);
    return () => media.removeEventListener?.("change", sync);
  }, []);
  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    closeRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "Tab" && modalLayout) {
        const items = [...(panelRef.current?.querySelectorAll('button:not([disabled]), a[href], input, select, textarea') || [])];
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocusRef.current?.focus?.();
    };
  }, [onClose, modalLayout]);
  if (!job) return null;
  const title = job.song_title || (job.filename || "").replace(/\.(mp3|wav)$/i, "") || "Sin nombre";
  const hasProRes = ["umg", "both"].includes(job.delivery_profile) || job.prores_ready;
  return (
    <>
    <button type="button" className="history-inspector-backdrop" onClick={onClose} aria-label={t("history.inspector_close")} />
    <aside ref={panelRef} className="history-inspector" role="dialog" aria-modal={modalLayout || undefined} aria-labelledby="history-inspector-title">
      <div className="history-inspector__header">
        <div><p className="section-eyebrow">{t("history.inspector_detail")}</p><h2 id="history-inspector-title">{title}</h2></div>
        <button ref={closeRef} type="button" onClick={onClose} aria-label={t("history.inspector_close")}>×</button>
      </div>
      <MediaPreview src={thumbSrc} status={job.status} className="history-inspector__preview" alt={`Preview de ${title}`} />
      <dl>
        <div><dt>{t("history.inspector_artist")}</dt><dd>{job.artist || "—"}</dd></div>
        <div><dt>{t("history.inspector_format")}</dt><dd>{hasProRes ? t("history.inspector_prores") : t("history.inspector_mp4")}</dd></div>
        <div><dt>{t("history.inspector_status")}</dt><dd><StatusBadge status={job.status} t={t} /></dd></div>
        <div><dt>ID</dt><dd className="font-mono">{job.job_id}</dd></div>
      </dl>
      <button type="button" onClick={() => onOpen(job.job_id, job.status)} className="btn-primary w-full !h-11">{t("history.inspector_open")}</button>
    </aside>
    </>
  );
}

export default function HistoryView({
  history,
  historyError = false,
  historyLoaded = true,
  onRetryHistory,
  onSelect,
  onDelete,
  onBulkDelete,
  onBack,
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [filter, setFilter] = useState("all");
  // Archivado Fase 1: ver intentos fallidos archivados (default oculto).
  const [showArchived, setShowArchived] = useState(false);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [focusedId, setFocusedId] = useState(null);
  // 2026-05-25 PR-3 — Estado nuevo: search query (inline), sort key,
  // view toggle (table|grid). View y sort persisten en localStorage para
  // que el operador no tenga que re-configurarlos en cada sesión.
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useLocalStorage("genly_history_sort", "newest");
  const [view, setView] = useLocalStorage("genly_history_view", "table");
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const sortMenuRef = useRef(null);
  const sortTriggerRef = useRef(null);

  // The former card layout persisted in localStorage and could hide the
  // redesigned operator table indefinitely. Migrate it once, while keeping
  // the toggle available afterwards as an explicit user preference.
  useEffect(() => {
    if (localStorage.getItem("genly_history_layout_version") === "2") return;
    setView("table");
    localStorage.setItem("genly_history_layout_version", "2");
  }, [setView]);

  // Click-outside cierra el sort menu
  useEffect(() => {
    if (!sortMenuOpen) return;
    requestAnimationFrame(() => sortMenuRef.current?.querySelector('[role="menuitemradio"]')?.focus());
    const handler = (e) => {
      if (sortMenuRef.current && !sortMenuRef.current.contains(e.target)) {
        setSortMenuOpen(false);
      }
    };
    const escape = (e) => {
      if (e.key === "Escape") {
        setSortMenuOpen(false);
        sortTriggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", escape);
    };
  }, [sortMenuOpen]);

  const handleSortMenuKeyDown = (event) => {
    const items = [...(sortMenuRef.current?.querySelectorAll('[role="menuitemradio"]') || [])];
    if (!items.length) return;
    const current = items.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) event.preventDefault();
    if (event.key === "ArrowDown") items[(current + 1 + items.length) % items.length]?.focus();
    if (event.key === "ArrowUp") items[(current - 1 + items.length) % items.length]?.focus();
    if (event.key === "Home") items[0]?.focus();
    if (event.key === "End") items[items.length - 1]?.focus();
  };

  // Three terminal states share the "no rows" branch: still loading the
  // first fetch, fetch failed, or the tenant genuinely has zero jobs. We
  // must distinguish them — rendering "Aún no hay videos" while /jobs is
  // mid-retry or 5xx'ing is the bug that made operators think their
  // catalog was deleted.
  const isInitialLoading = !historyLoaded && !historyError;
  const isLoadFailed = historyError && history.length === 0;

  // Track how long the initial load has been spinning. After 15 s we
  // swap the skeleton's footer copy to acknowledge the slowness ("el
  // servidor está demorando…") + a manual reload button — saves the
  // operator from staring at a placeholder grid wondering if the page
  // is dead. Audit 2026-05-27 (UMG perf complaint).
  const [loadingElapsedSec, setLoadingElapsedSec] = useState(0);
  useEffect(() => {
    if (!isInitialLoading) {
      setLoadingElapsedSec(0);
      return;
    }
    const start = Date.now();
    const iv = setInterval(() => {
      setLoadingElapsedSec(Math.floor((Date.now() - start) / 1000));
    }, 1000);
    return () => clearInterval(iv);
  }, [isInitialLoading]);
  const showSlowHint = loadingElapsedSec >= 15;

  // GHOST jobs — bg_preview_* son ghost jobs del feature Capa C
  // (pre-render del fondo mientras editás lyrics). El comentario en
  // jobs.py:55 dice "Should NOT appear in the user's history normally
  // (filter elsewhere)". Filtramos acá hasta que el backend los
  // excluya en /jobs. 2026-05-25 operator feedback.
  const GHOST_STATUSES = useMemo(
    () => new Set(["bg_preview_queued", "bg_preview_done", "bg_preview_failed"]),
    [],
  );
  const nonGhostHistory = useMemo(
    () => history.filter((j) => !GHOST_STATUSES.has(j.status)),
    [history, GHOST_STATUSES],
  );

  // Archivado Fase 1 (2026-06-10): los intentos fallidos previos de un
  // audio que después salió `done` vienen con archived_at seteado (el
  // backend los marca al aprobar — nunca los borra). Por default no se
  // muestran; el toggle los trae de vuelta para auditoría.
  const archivedCount = useMemo(
    () => nonGhostHistory.filter((j) => j.archived_at).length,
    [nonGhostHistory],
  );
  const realHistory = useMemo(
    () => (showArchived ? nonGhostHistory : nonGhostHistory.filter((j) => !j.archived_at)),
    [nonGhostHistory, showArchived],
  );

  const counts = useMemo(() => {
    const c = {};
    for (const f of FILTERS) c[f.id] = realHistory.filter(f.match).length;
    return c;
  }, [realHistory]);

  const visible = useMemo(() => {
    const f = FILTERS.find((x) => x.id === filter) || FILTERS[0];
    let filtered = realHistory.filter(f.match);
    // Search query — filtra por artist + song_title + filename (case-insensitive)
    const q = query.trim().toLowerCase();
    if (q) {
      filtered = filtered.filter((j) => {
        const a = (j.artist || "").toLowerCase();
        const s = (j.song_title || "").toLowerCase();
        const fn = (j.filename || "").toLowerCase();
        return a.includes(q) || s.includes(q) || fn.includes(q);
      });
    }
    return sortJobs(filtered, sortKey);
  }, [realHistory, filter, query, sortKey]);
  const focusedJob = useMemo(() => visible.find((job) => job.job_id === focusedId) || null, [visible, focusedId]);
  const focusForInspector = (jobId, status) => {
    if (status === "transcribed") onSelect(jobId, status);
    else setFocusedId(jobId);
  };
  const closeInspector = useCallback(() => setFocusedId(null), []);

  // Buckets temporales — solo para vista tabla con date groups.
  // En grid mantenemos lista plana para no quebrar el flow visual.
  const buckets = useMemo(() => bucketByDateAR(visible), [visible]);

  // Sort options (label + key) — usados por el dropdown
  const SORT_OPTIONS = [
    { key: "newest",        label: "Más reciente" },
    { key: "oldest",        label: "Más antigua" },
    { key: "artist_asc",    label: "Artista A→Z" },
    { key: "artist_desc",   label: "Artista Z→A" },
  ];
  const currentSortLabel = SORT_OPTIONS.find((o) => o.key === sortKey)?.label || "Más reciente";

  // When the history list updates (e.g. row deleted), drop selections
  // pointing to job_ids that no longer exist.
  useEffect(() => {
    const live = new Set(history.map((j) => j.job_id));
    setSelectedIds((prev) => {
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [history]);

  const toggleSelect = (jobId) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(jobId)) next.delete(jobId);
      else next.add(jobId);
      return next;
    });
  };

  const visibleDeletableIds = useMemo(
    () => visible.filter((j) => DELETABLE.has(j.status)).map((j) => j.job_id),
    [visible],
  );
  const allVisibleSelected = visibleDeletableIds.length > 0 &&
    visibleDeletableIds.every((id) => selectedIds.has(id));

  const selectAllVisible = () => {
    if (allVisibleSelected) {
      // Toggle off — drop just the visible ones from selection
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of visibleDeletableIds) next.delete(id);
        return next;
      });
    } else {
      setSelectedIds((prev) => new Set([...prev, ...visibleDeletableIds]));
    }
  };

  const handleBulkDelete = async () => {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    if (!confirm(`¿Eliminar ${ids.length} ${ids.length === 1 ? "video" : "videos"}? Esta acción no se puede deshacer.`)) return;
    await onBulkDelete?.(ids);
    setSelectedIds(new Set());
  };

  const clearSelection = () => setSelectedIds(new Set());
  const selectedCount = selectedIds.size;

  return (
    <div className="w-full max-w-[1700px] mx-auto animate-fade-in">
      {/* ─── Header ─────────────────────────────────────────────── */}
      <div className="flex items-end justify-between mb-8">
        <div className="flex items-center gap-3">
          <button onClick={onBack} aria-label="Volver"
            className="w-9 h-9 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h1 className="text-[28px] leading-tight font-bold tracking-tight">{t("history.title")}</h1>
            <p className="text-sm text-ink-secondary mt-1">
              {isInitialLoading
                ? (t("history.loading") || "Cargando…")
                : isLoadFailed
                ? (t("history.load_failed_subtitle") || "No pudimos cargar tu historial")
                : realHistory.length === 0
                ? "Aún no hay videos"
                : `${realHistory.length} ${realHistory.length === 1 ? "video en total" : "videos en total"}`}
            </p>
          </div>
        </div>
      </div>

      {/* ─── Search + Sort + View Toggle (PR-3 2026-05-25) ─────────
            Top control row: search inline a la izquierda, sort dropdown
            y toggle tabla/grid a la derecha. Pattern Notion/Linear. */}
      {history.length > 0 && (
        <div className="flex items-center gap-2 mb-4">
          {/* Search inline */}
          <div className="flex-1 min-w-0 relative">
            <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500 pointer-events-none" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" strokeLinecap="round" />
            </svg>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("history.search_placeholder") || "Buscar artista, canción o ID…"}
              className="w-full h-9 pl-9 pr-9 rounded-lg bg-surface-2/40 ring-1 ring-white/[0.04] focus:ring-white/[0.12] hover:ring-white/[0.08] text-[13px] text-white placeholder:text-gray-500 outline-none transition-colors"
              spellCheck={false}
              autoComplete="off"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 w-5 h-5 rounded-full bg-white/[0.06] hover:bg-white/[0.12] flex items-center justify-center text-gray-400 hover:text-white transition-colors"
                aria-label="Limpiar búsqueda"
              >
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>

          {/* Sort dropdown */}
          <div className="relative" ref={sortMenuRef}>
            <button
              ref={sortTriggerRef}
              type="button"
              onClick={() => setSortMenuOpen((v) => !v)}
              aria-label={`Ordenar historial: ${currentSortLabel}`}
              aria-haspopup="menu"
              aria-expanded={sortMenuOpen}
              className="flex items-center gap-1.5 h-9 px-3 rounded-lg bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] text-[12px] text-gray-300 hover:text-white transition-colors"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M3 6h18M6 12h12M10 18h4" strokeLinecap="round" />
              </svg>
              <span className="hidden sm:inline">{currentSortLabel}</span>
              <svg className={`w-3 h-3 transition-transform ${sortMenuOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            {sortMenuOpen && (
              <div className="absolute right-0 top-full mt-1 w-44 rounded-lg bg-surface-2 ring-1 ring-white/10 shadow-xl py-1 z-20" role="menu" onKeyDown={handleSortMenuKeyDown}>
                {SORT_OPTIONS.map((o) => (
                  <button
                    key={o.key}
                    type="button"
                    role="menuitemradio"
                    aria-checked={sortKey === o.key}
                    onClick={() => { setSortKey(o.key); setSortMenuOpen(false); }}
                    className={`w-full text-left px-3 py-1.5 text-[12px] transition-colors flex items-center justify-between
                      ${sortKey === o.key ? "text-brand-light bg-white/[0.04]" : "text-gray-300 hover:bg-white/[0.04] hover:text-white"}`}
                  >
                    {o.label}
                    {sortKey === o.key && (
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* View toggle: tabla / grid */}
          <div className="flex items-center bg-surface-2/40 ring-1 ring-white/[0.04] rounded-lg overflow-hidden">
            <button
              type="button"
              onClick={() => setView("table")}
              className={`h-9 px-2.5 transition-colors ${view === "table" ? "bg-white/[0.08] text-white" : "text-gray-500 hover:text-gray-300"}`}
              aria-label="Vista tabla"
              aria-pressed={view === "table"}
              title="Vista tabla"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M3 6h18M3 12h18M3 18h18" strokeLinecap="round" />
              </svg>
            </button>
            <button
              type="button"
              onClick={() => setView("grid")}
              className={`h-9 px-2.5 transition-colors ${view === "grid" ? "bg-white/[0.08] text-white" : "text-gray-500 hover:text-gray-300"}`}
              aria-label="Vista cards"
              aria-pressed={view === "grid"}
              title="Vista cards"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
                <rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" />
              </svg>
            </button>
          </div>
        </div>
      )}

      {/* ─── Filters (segmented pills) ──────────────────────────── */}
      {history.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-6">
          {FILTERS.filter((f) => f.id === "all" || counts[f.id] > 0).map((f) => (
            <FilterPill
              key={f.id}
              active={filter === f.id}
              count={counts[f.id]}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </FilterPill>
          ))}
          {archivedCount > 0 && (
            <FilterPill
              active={showArchived}
              count={archivedCount}
              onClick={() => setShowArchived((v) => !v)}
            >
              {t("history.archived") || "Archivados"}
            </FilterPill>
          )}
        </div>
      )}

      {/* ─── Bulk action bar — appears when ≥1 deletable rows exist ── */}
      {selectedCount > 0 && (
        <div className="flex items-center justify-between gap-3 mb-4 px-3 py-2 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04]">
          <div className="flex items-center gap-3 min-w-0">
            <button
              onClick={selectAllVisible}
              className="text-label text-brand hover:text-brand-light transition-colors flex items-center gap-1.5 shrink-0"
            >
              <span className={`w-4 h-4 rounded border-2 flex items-center justify-center transition-colors
                ${allVisibleSelected ? "bg-brand border-brand" : "border-white/30 hover:border-brand/70"}`}>
                {allVisibleSelected && (
                  <svg className="w-2.5 h-2.5 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                )}
              </span>
              {allVisibleSelected
                ? (t("history.deselect_all") || "Deseleccionar todos")
                : (t("history.select_all_failed") || `Seleccionar ${visibleDeletableIds.length} eliminables`)}
            </button>
            {selectedCount > 0 && (
              <span className="text-[11px] text-ink-secondary">
                · {selectedCount} {selectedCount === 1 ? "seleccionado" : "seleccionados"}
              </span>
            )}
          </div>
          {selectedCount > 0 && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={clearSelection}
                className="text-[11px] text-gray-500 hover:text-white px-2 py-1.5 transition-colors"
              >
                {t("history.clear_selection") || "Limpiar"}
              </button>
              <button
                onClick={handleBulkDelete}
                className="text-label px-3 py-1.5 rounded-lg bg-red-500/15 text-red-300
                  ring-1 ring-red-500/30 hover:bg-red-500/25 transition-colors"
              >
                {t("history.delete_selected") || `Eliminar ${selectedCount}`}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ─── Grid ───────────────────────────────────────────────
          Order matters: loading > error > empty-but-loaded > populated.
          The error branch must beat the empty branch so a slow /jobs
          never silently masquerades as "you have no videos". */}
      {isInitialLoading ? (
        /* Skeleton — UI matches the actual layout (table vs grid) so the
           swap to real rows is shape-stable. After 15 s the footer
           gains a "tomando más de lo normal" hint + Reintentar button
           so the operator knows we're not dead. From staging PR #411. */
        <div>
          {view === "table" ? (
            <div className="space-y-1.5">
              {Array.from({ length: 8 }).map((_, i) => (
                <div
                  key={i}
                  className="h-16 rounded-card bg-surface-2/40 ring-1 ring-white/[0.03] animate-pulse"
                />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <div
                  key={i}
                  className="aspect-video rounded-card bg-surface-2/40 ring-1 ring-white/[0.03] animate-pulse"
                />
              ))}
            </div>
          )}
          {showSlowHint && (
            <div className="mt-6 rounded-card p-5 text-center bg-amber-500/[0.04] ring-1 ring-amber-500/15">
              <p className="text-sm text-ink-secondary mb-3">
                {t("history.loading_slow") ||
                  "El servidor está demorando más de lo normal. Probá recargar."}
              </p>
              {onRetryHistory && (
                <button
                  onClick={onRetryHistory}
                  className="text-xs text-brand-light hover:text-white transition-colors underline-offset-2 hover:underline"
                >
                  {t("dash.retry") || "Reintentar"}
                </button>
              )}
            </div>
          )}
        </div>
      ) : isLoadFailed ? (
        <div className="rounded-card p-10 text-center bg-amber-500/[0.06] ring-1 ring-amber-500/25">
          <div className="w-12 h-12 mx-auto mb-4 rounded-2xl bg-amber-500/15 ring-1 ring-amber-500/30 flex items-center justify-center">
            <svg className="w-6 h-6 text-amber-300" fill="none" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 24 24">
              <path d="M12 9v3.5m0 3.5h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h3 className="text-base font-semibold text-white mb-1.5 tracking-tight">
            {t("dash.history_error_title") || "No pudimos cargar tu historial"}
          </h3>
          <p className="text-sm text-ink-secondary mb-5">
            {t("dash.history_error_body") || "Puede ser una caída momentánea de la conexión. Probá de nuevo."}
          </p>
          {onRetryHistory && (
            <button onClick={onRetryHistory} className="btn-primary px-6">
              {t("dash.retry") || "Reintentar"}
            </button>
          )}
        </div>
      ) : visible.length === 0 ? (
        query.trim() ? (
          /* Search returned no results — keep the small card, the user
             is mid-query and a giant hero would feel jarring. */
          <div className="rounded-card p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04] max-w-2xl mx-auto">
            <p className="text-sm text-ink-secondary mb-3">
              No encontramos videos para <span className="font-mono text-white">"{query.trim()}"</span>
            </p>
            <button
              type="button"
              onClick={() => setQuery("")}
              className="text-xs text-brand-light hover:text-white transition-colors underline-offset-2 hover:underline"
            >
              Limpiar búsqueda
            </button>
          </div>
        ) : history.length === 0 ? (
          /* True empty state — first-time user lands here. Mirror the
             hero dropzone from the Dashboard so the CTA is unmistakable.
             UX 2026-05-29: replaced the chiquito card alineado izquierda
             that left ~1000px of void on wide viewports. */
          <button
            type="button"
            onClick={() => navigate("/new")}
            className="dr-hero-drop w-full block group"
            aria-label={t("history.empty_cta") || "Crear tu primer video"}
          >
            <div className="dr-hero-drop-inner">
              <div className="dr-hero-drop-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
                </svg>
              </div>
              <h2 className="dr-hero-drop-title">
                {t("history.empty_title") || "Tu historial está vacío"}
              </h2>
              <p className="dr-hero-drop-sub">
                {t("history.empty_sub") || "Cuando termines un lyric video va a aparecer acá, listo para descargar o aprobar."}
              </p>
              <span className="dr-hero-drop-cta">
                {t("history.empty_cta") || "Crear tu primer video"}
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </span>
            </div>
          </button>
        ) : (
          /* "Filter cleared this view but user has history elsewhere" —
             midweight card centrado. */
          <div className="rounded-card p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04] max-w-2xl mx-auto">
            <p className="text-sm text-ink-secondary">
              No hay videos en esta vista
            </p>
          </div>
        )
      ) : view === "table" ? (
        /* TABLA DENSA con sticky date group headers (PR-3 2026-05-25) */
        <div className={`history-results ${focusedJob ? "has-inspector" : ""}`}>
        <div className="min-w-0 history-table">
          <div className="history-table__head" aria-hidden="true">
            <span>{t("history.column_video")}</span>
            <span>{t("history.column_artist_song")}</span>
            <span>{t("history.column_status")}</span>
            <span>{t("history.column_created")}</span>
            <span />
          </div>
          {buckets.map((bucket) => (
            <div key={bucket.key}>
              <div className="history-table__bucket-head">
                <p className="text-section text-gray-500 uppercase tracking-[0.18em]">
                  {bucket.label} <span className="text-gray-600 font-mono tabular-nums normal-case">· {bucket.jobs.length}</span>
                </p>
              </div>
              <div className="history-table__body">
                {bucket.jobs.map((job) => (
                  <MediaRow
                    key={job.job_id}
                    job={job}
                    onSelect={focusForInspector}
                    onDelete={onDelete}
                    selected={selectedIds.has(job.job_id)}
                    onToggleSelect={toggleSelect}
                    t={t}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
        {focusedJob && <HistoryInspector job={focusedJob} onClose={closeInspector} onOpen={onSelect} t={t} />}
        </div>
      ) : (
        /* VISTA GRID original con date group headers */
        <div className="space-y-6">
          {buckets.map((bucket) => (
            <div key={bucket.key}>
              <div className="mb-3">
                <p className="text-section text-gray-500 uppercase tracking-[0.18em]">
                  {bucket.label} <span className="text-gray-600 font-mono tabular-nums normal-case">· {bucket.jobs.length}</span>
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {bucket.jobs.map((job) => (
                  <VideoCard
                    key={job.job_id}
                    job={job}
                    onSelect={onSelect}
                    onDelete={onDelete}
                    selected={selectedIds.has(job.job_id)}
                    onToggleSelect={toggleSelect}
                    t={t}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
