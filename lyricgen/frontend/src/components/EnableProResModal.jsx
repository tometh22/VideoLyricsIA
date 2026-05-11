import { useState, useRef, useEffect } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

// Opciones espejadas de _parse_umg_params + validate_umg_config en el
// backend. Si se agregan profiles/fps nuevos en backend, actualizar acá.
const FRAME_SIZES = [
  { value: "1920x1080", label: "1080p (1920×1080)" },
  { value: "3840x2160", label: "4K UHD (3840×2160)" },
  { value: "1280x720",  label: "720p (1280×720)" },
];

const FPS_OPTS = [
  { value: "23.976", label: "23.976 fps (cine)" },
  { value: "24",     label: "24 fps" },
  { value: "25",     label: "25 fps (PAL)" },
  { value: "29.97",  label: "29.97 fps (NTSC)" },
  { value: "30",     label: "30 fps" },
];

const PROFILE_OPTS = [
  { value: "1", label: "ProRes LT" },
  { value: "2", label: "ProRes Standard" },
  { value: "3", label: "ProRes 422 HQ (broadcast)" },
  { value: "4", label: "ProRes 4444 (master)" },
];

// Defaults broadcast estándar — confirmado con el usuario:
// 1080p, 29.97 fps, ProRes 422 HQ (profile=3).
const DEFAULTS = {
  umg_frame_size:    "1920x1080",
  umg_fps:           "29.97",
  umg_prores_profile: "3",
};

export default function EnableProResModal({ jobId, onClose, onSuccess }) {
  const { t } = useI18n();
  const [form, setForm] = useState(DEFAULTS);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  // El modal se desmonta apenas onSuccess flipea el state upstream; el
  // finally setState corre en componente desmontado. Mismo patrón que
  // EditRequestPanel.
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${API}/enable-prores/${jobId}`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      onSuccess?.(data);
    } catch (e) {
      if (mountedRef.current) setError(e.message || "Error al habilitar ProRes");
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md mx-4 bg-surface-1 ring-1 ring-white/[0.08] rounded-2xl p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-white mb-1">
          {t("prores.enable_title") || "Exportar a ProRes"}
        </h3>
        <p className="text-xs text-gray-400 mb-5">
          {t("prores.enable_desc") ||
            "Generamos un máster ProRes (.mov) a partir del MP4 ya renderizado. Tarda 1-5 minutos según la duración."}
        </p>

        <div className="space-y-4">
          <div>
            <label className="text-[11px] uppercase tracking-wider text-gray-500 block mb-1">
              {t("prores.frame_size") || "Resolución"}
            </label>
            <select
              value={form.umg_frame_size}
              onChange={(e) => setForm({ ...form, umg_frame_size: e.target.value })}
              disabled={submitting}
              className="w-full px-3 py-2 bg-surface-3/40 ring-1 ring-white/[0.06] rounded-md text-sm text-white"
            >
              {FRAME_SIZES.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-[11px] uppercase tracking-wider text-gray-500 block mb-1">
              {t("prores.fps") || "Frame rate"}
            </label>
            <select
              value={form.umg_fps}
              onChange={(e) => setForm({ ...form, umg_fps: e.target.value })}
              disabled={submitting}
              className="w-full px-3 py-2 bg-surface-3/40 ring-1 ring-white/[0.06] rounded-md text-sm text-white"
            >
              {FPS_OPTS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-[11px] uppercase tracking-wider text-gray-500 block mb-1">
              {t("prores.profile") || "Perfil ProRes"}
            </label>
            <select
              value={form.umg_prores_profile}
              onChange={(e) => setForm({ ...form, umg_prores_profile: e.target.value })}
              disabled={submitting}
              className="w-full px-3 py-2 bg-surface-3/40 ring-1 ring-white/[0.06] rounded-md text-sm text-white"
            >
              {PROFILE_OPTS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

          {error && (
            <div className="text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
              {error}
            </div>
          )}
        </div>

        <div className="flex gap-2 mt-6">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="flex-1 py-2 rounded-md text-sm font-medium text-gray-300 bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] transition-colors disabled:opacity-40"
          >
            {t("common.cancel") || "Cancelar"}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={submitting}
            className="flex-1 py-2 rounded-md text-sm font-medium text-white bg-brand hover:bg-brand-strong ring-1 ring-brand/30 transition-colors disabled:opacity-40"
          >
            {submitting
              ? (t("prores.submitting") || "Encolando…")
              : (t("prores.submit") || "Generar ProRes")}
          </button>
        </div>
      </div>
    </div>
  );
}
