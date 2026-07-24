import { useState, useEffect } from "react";
import { useI18n } from "../i18n.jsx";

const _TYPE_LABEL = { intro: "Intro", verso: "Verso", pre: "Pre", coro: "Coro", puente: "Puente", outro: "Outro" };
const _MOVES = [
  { v: "", labelKey: "scenes.move_keep", fallback: "Sin cambio" },
  { v: "estatico", labelKey: "scenes.move_static", fallback: "Estático" },
  { v: "sutil", labelKey: "scenes.move_subtle", fallback: "Sutil" },
  { v: "dinamico", labelKey: "scenes.move_dynamic", fallback: "Dinámico" },
];

/**
 * Modal "Editar escena": redirige una escena con un hint libre (hereda la
 * biblia visual) y/o cambia el movimiento de cámara. Cuesta 1 clip Veo y
 * cuenta como un edit — igual que "Regenerar fondo".
 *
 * Props: scene, onClose(), onSubmit({hint, movement_style})
 */
export default function SceneEditModal({ scene, onClose, onSubmit }) {
  const { t } = useI18n();
  const [hint, setHint] = useState("");
  const [movement, setMovement] = useState("");
  const [showPrompt, setShowPrompt] = useState(false);

  const label = _TYPE_LABEL[scene.section_type] || scene.section_type || "Escena";
  const canSubmit = hint.trim().length > 0 || (movement && movement !== "");

  // Escape cierra el modal (a11y — audit M6/NIT).
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={(t("scenes.edit_title") || "Editar escena: {label}").replace("{label}", label)}
        className="w-full max-w-md rounded-card bg-surface-2 ring-1 ring-white/[0.08] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-[14px] font-semibold text-white flex items-center gap-2">
            <span>🎬</span>
            {(t("scenes.edit_title") || "Editar escena: {label}").replace("{label}", label)}
          </h3>
          <button onClick={onClose} aria-label={t("common.cancel") || "Cancelar"} className="text-gray-400 hover:text-gray-200 text-lg leading-none outline-none focus-visible:ring-2 focus-visible:ring-brand/60 rounded">×</button>
        </div>

        <p className="text-[11px] text-gray-500 mb-3">
          {t("scenes.edit_help") ||
            "Describí qué querés ver en esta escena. Hereda el estilo visual del video (mismo mundo, paleta y cámara)."}
        </p>

        <textarea
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          rows={3}
          maxLength={2000}
          placeholder={t("scenes.edit_placeholder") || "Ej: un primer plano de gotas cayendo sobre la ventana, más oscuro"}
          className="w-full text-[12px] rounded-lg bg-surface-3 ring-1 ring-white/[0.06] focus:ring-brand/50 outline-none p-2.5 text-gray-200 placeholder:text-gray-600 resize-none"
        />

        <div className="mt-3 flex items-center gap-2">
          <label className="text-[11px] text-gray-400">{t("scenes.movement") || "Movimiento de cámara"}</label>
          <select
            value={movement}
            onChange={(e) => setMovement(e.target.value)}
            className="text-[11px] rounded-md bg-surface-3 ring-1 ring-white/[0.06] outline-none px-2 py-1 text-gray-200"
          >
            {_MOVES.map((m) => (
              <option key={m.v} value={m.v}>{t(m.labelKey) || m.fallback}</option>
            ))}
          </select>
        </div>

        {/* Prompt actual (avanzado, colapsado) — referencia para el operador. */}
        {scene.prompt && (
          <div className="mt-3">
            <button
              type="button"
              onClick={() => setShowPrompt((v) => !v)}
              className="text-[10px] text-gray-500 hover:text-gray-300"
            >
              {showPrompt ? "▾" : "▸"} {t("scenes.show_prompt") || "Ver el prompt actual"}
            </button>
            {showPrompt && (
              <p className="mt-1 text-[10px] text-gray-500 bg-surface-3/60 rounded p-2 max-h-24 overflow-y-auto">
                {scene.prompt}
              </p>
            )}
          </div>
        )}

        <div className="mt-4 flex items-center justify-between">
          <span className="text-[11px] text-ink-secondary">{t("scenes.edit_cost") || "~US$0.90 · cuenta como un edit"}</span>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="text-[12px] font-medium px-3 py-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-gray-300 outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
            >
              {t("common.cancel") || "Cancelar"}
            </button>
            <button
              disabled={!canSubmit}
              onClick={() => onSubmit({ hint: hint.trim(), movement_style: movement })}
              className="text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-brand hover:bg-brand-light text-white outline-none focus-visible:ring-2 focus-visible:ring-brand/60 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t("scenes.regen_submit") || "Regenerar escena"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
