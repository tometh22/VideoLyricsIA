import { useMemo, useState } from "react";
import { useI18n } from "../i18n.jsx";

/**
 * Filmstrip de escenas (add-on "Escenas" / multi-escena) para JobDetail.
 *
 * Presentacional: recibe el `scenePlan` del job (tal cual viaja en
 * job.scene_plan) y delega las acciones al padre. Una "card" por escena ÚNICA
 * (recurrence_key) — que es la unidad regenerable. Los coros recurrentes son
 * UNA card con un badge "↻N×" porque comparten clip: regenerarla cambia todas
 * sus apariciones (el padre lo confirma en el diálogo).
 *
 * Props:
 *   scenePlan     {bible, sections:[...], scenes:[...], params}
 *   thumbUrlFor   (scene) => string|null   URL del póster (o null → placeholder)
 *   onRegenerate  (scene) => void          "Otra toma" (cache-bust, mismo prompt)
 *   onEditPrompt  (scene) => void          abre el mini-panel de edición
 *   onSeek        (seconds) => void         salta el reproductor a ese tiempo
 *   busyKey       recurrence_key en regeneración (muestra spinner) | null
 *   disabled      deshabilita acciones (job no done / sin edits)
 */

const _TYPE_LABEL = {
  intro: "Intro",
  verso: "Verso",
  pre: "Pre",
  coro: "Coro",
  puente: "Puente",
  outro: "Outro",
};

function fmtTime(s) {
  const sec = Math.max(0, Math.round(s || 0));
  const m = Math.floor(sec / 60);
  const r = sec % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function sceneLabel(scene) {
  const base = _TYPE_LABEL[scene.section_type] || (scene.section_type || "Escena");
  // recurrence_key tipo "verso_2" → "Verso 2"; "coro_1"/único → solo "Coro".
  const m = /_(\d+)$/.exec(scene.recurrence_key || "");
  const n = m ? parseInt(m[1], 10) : null;
  return n && n > 1 ? `${base} ${n}` : base;
}

export default function ScenesFilmstrip({
  scenePlan,
  thumbUrlFor = () => null,
  onRegenerate,
  onEditPrompt,
  onSeek,
  busyKey = null,
  disabled = false,
}) {
  const { t } = useI18n();
  const [hoverKey, setHoverKey] = useState(null);

  const { scenes, sections } = scenePlan || {};
  // Apariciones por recurrence_key (para el badge ↻N× y los saltos del reproductor).
  const appearances = useMemo(() => {
    const map = {};
    for (const sec of sections || []) {
      (map[sec.recurrence_key] ||= []).push(sec);
    }
    for (const k of Object.keys(map)) map[k].sort((a, b) => a.start - b.start);
    return map;
  }, [sections]);

  if (!scenes || !scenes.length) return null;

  const totalCuts = (sections || []).length;
  const failed = scenes.filter((s) => s.status === "failed").length;

  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-[13px]">🎬</span>
          <h3 className="text-[13px] font-semibold text-gray-200">
            {t("scenes.title") || "Escenas"}
          </h3>
          <span className="text-[11px] text-gray-500">
            {t("scenes.count_fmt")
              ? t("scenes.count_fmt").replace("{cuts}", totalCuts).replace("{unique}", scenes.length)
              : `${totalCuts} cortes · ${scenes.length} únicas`}
          </span>
        </div>
        {failed > 0 && (
          <span className="text-[10px] font-semibold text-amber-300/90 bg-amber-400/10 rounded px-1.5 py-0.5">
            {t("scenes.failed_warn_fmt")
              ? t("scenes.failed_warn_fmt").replace("{n}", failed)
              : `${failed} escena(s) reusada(s) — regenerá`}
          </span>
        )}
      </div>

      <div className="flex gap-3 overflow-x-auto pb-1 -mx-1 px-1">
        {scenes.map((scene) => {
          const apps = appearances[scene.recurrence_key] || [];
          const firstStart = apps.length ? apps[0].start : 0;
          const recurrent = apps.length > 1;
          const isFailed = scene.status === "failed";
          const busy = busyKey === scene.recurrence_key;
          const thumb = thumbUrlFor(scene);
          const active = hoverKey === scene.recurrence_key;

          return (
            <div
              key={scene.recurrence_key}
              className="shrink-0 w-[150px]"
              onMouseEnter={() => setHoverKey(scene.recurrence_key)}
              onMouseLeave={() => setHoverKey(null)}
            >
              {/* Thumb */}
              <button
                type="button"
                onClick={() => onSeek && onSeek(firstStart)}
                title={t("scenes.seek_tooltip") || "Ver en el video"}
                className={`relative block w-full aspect-video rounded-lg overflow-hidden ring-1 transition-all ${
                  isFailed ? "ring-amber-400/40" : "ring-white/[0.08]"
                } ${active ? "ring-brand/50" : ""}`}
              >
                {thumb ? (
                  <img src={thumb} alt={sceneLabel(scene)} className="w-full h-full object-cover" loading="lazy" />
                ) : (
                  <div className="w-full h-full grid place-items-center bg-surface-3 text-gray-600 text-[18px]">🎞️</div>
                )}
                {busy && (
                  <div className="absolute inset-0 bg-black/60 grid place-items-center">
                    <div className="flex flex-col items-center gap-1">
                      <span className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                      <span className="text-[9px] text-white/80">{t("scenes.generating") || "Generando…"}</span>
                    </div>
                  </div>
                )}
                {/* badges */}
                <div className="absolute top-1 left-1 flex gap-1">
                  {recurrent && (
                    <span
                      className="text-[9px] font-bold text-white/90 bg-black/55 rounded px-1 py-0.5"
                      title={apps.map((a) => fmtTime(a.start)).join(" · ")}
                    >
                      ↻{apps.length}×
                    </span>
                  )}
                  {isFailed && (
                    <span className="text-[9px] font-bold text-amber-200 bg-amber-900/70 rounded px-1 py-0.5">⚠</span>
                  )}
                </div>
                <span className="absolute bottom-1 right-1 text-[9px] text-white/80 bg-black/55 rounded px-1 py-0.5">
                  {fmtTime(firstStart)}
                </span>
              </button>

              {/* Label */}
              <div className="mt-1.5 px-0.5">
                <div className="text-[11px] font-semibold text-gray-200 leading-tight">{sceneLabel(scene)}</div>
                <div className="text-[10px] text-gray-500">
                  {recurrent
                    ? (t("scenes.appears_fmt")
                        ? t("scenes.appears_fmt").replace("{n}", apps.length)
                        : `aparece ${apps.length}×`)
                    : apps.length
                    ? `${fmtTime(apps[0].start)}–${fmtTime(apps[0].end)}`
                    : ""}
                </div>
              </div>

              {/* Acciones (siempre visibles en touch; resaltan en hover) */}
              <div className={`mt-1.5 flex gap-1 transition-opacity ${active ? "opacity-100" : "opacity-70"}`}>
                <button
                  type="button"
                  disabled={disabled || busy}
                  onClick={() => onRegenerate && onRegenerate(scene)}
                  title={t("scenes.regen_tooltip") || "Generar otra versión de esta escena"}
                  className="flex-1 text-[10px] font-medium rounded-md px-1.5 py-1 bg-white/[0.04] hover:bg-white/[0.08] text-gray-200 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  ↻ {t("scenes.regen") || "Otra toma"}
                </button>
                <button
                  type="button"
                  disabled={disabled || busy}
                  onClick={() => onEditPrompt && onEditPrompt(scene)}
                  title={t("scenes.edit_tooltip") || "Editar el prompt de esta escena"}
                  className="text-[10px] font-medium rounded-md px-1.5 py-1 bg-white/[0.04] hover:bg-white/[0.08] text-gray-200 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  ✎
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
