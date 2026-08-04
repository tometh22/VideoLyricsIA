import { useState, useEffect, useRef } from "react";
import { useI18n } from "../i18n";
import { EFFECT_LABELS } from "../lib/optionLabels";
import { EFFECT_CODES } from "../lib/catalogCodes";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Panel de edición de un Art Track ya generado.
//
// Los art tracks ("official audio": cover + fondo blur + waveform, sin letra)
// no pasan por el wizard de letra —no hay letra que editar— pero SÍ tienen
// ejes visuales que el operador puede querer cambiar sin rehacer el video:
// la portada (se equivocó de imagen), el efecto de partículas (se lo olvidó),
// el título/artista y la línea legal ℗/©. Este panel reemplaza al
// EditRequestPanel (que abre el Studio Console de letra) para art tracks.
//
// El guardado hace UN POST multipart a /jobs/{id}/edit-art-track (portada
// opcional) y el backend re-renderiza vía run_pipeline(art_track=True) — el
// mismo camino que /retry, gratis (no consume cuota). Los valores se
// pre-cargan del job, así que el estado enviado es autoritativo.
export default function ArtTrackEditPanel({ job, onEdited }) {
  const { t } = useI18n();
  const labels = EFFECT_LABELS(t);
  const EFFECTS = [
    { code: "", label: labels[""] },
    ...EFFECT_CODES.map((code) => ({ code, label: labels[code] || code })),
  ];

  const rp = job.render_params || {};
  const [effect, setEffect] = useState(rp.effect || "");
  const [songTitle, setSongTitle] = useState(job.song_title || "");
  const [artist, setArtist] = useState(job.artist || "");
  const [labelLine, setLabelLine] = useState(rp.label_line || "");
  const [hoverEffect, setHoverEffect] = useState(null);

  // Portada: mostramos la actual (firmada desde R2) hasta que el operador
  // elija una nueva. `coverFile` null = conservar la portada actual.
  const [coverFile, setCoverFile] = useState(null);
  const [coverPreview, setCoverPreview] = useState(null); // object URL del archivo nuevo
  const [currentCoverUrl, setCurrentCoverUrl] = useState(null);
  const fileRef = useRef(null);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/jobs/${job.job_id}/background-url`, {
          headers: authHeaders(),
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setCurrentCoverUrl(data.url || null);
      } catch {
        /* preview opcional — si falla, no mostramos la portada actual */
      }
    })();
    return () => { cancelled = true; };
  }, [job.job_id]);

  // Limpiar el object URL del archivo nuevo al desmontar / cambiarlo.
  useEffect(() => {
    return () => { if (coverPreview) URL.revokeObjectURL(coverPreview); };
  }, [coverPreview]);

  const onPickCover = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (!/\.(jpe?g|png)$/i.test(f.name)) {
      setError(t("artedit.cover_type_error") || "La portada debe ser una imagen .jpg o .png.");
      return;
    }
    setError("");
    if (coverPreview) URL.revokeObjectURL(coverPreview);
    setCoverFile(f);
    setCoverPreview(URL.createObjectURL(f));
  };

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const fd = new FormData();
      fd.append("effect", effect || "");
      fd.append("song_title", songTitle || "");
      fd.append("artist", artist || "");
      fd.append("label_line", labelLine || "");
      if (coverFile) fd.append("background_file", coverFile);

      const res = await fetch(`${API}/jobs/${job.job_id}/edit-art-track`, {
        method: "POST",
        headers: authHeaders(),
        body: fd,
      });
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json())?.detail || ""; } catch { /* noop */ }
        throw new Error(detail || `HTTP ${res.status}`);
      }
      const resp = await res.json();
      if (onEdited) onEdited({ ...resp, edit_type: "art_track" });
    } catch (e) {
      setError(e.message || (t("artedit.error") || "No se pudo guardar la edición."));
    } finally {
      setSaving(false);
    }
  };

  const shownCover = coverPreview || currentCoverUrl;

  return (
    <div className="rounded-card p-5 mb-4 bg-surface-2/40 ring-1 ring-white/[0.05] animate-fade-in">
      <div className="mb-4">
        <h3 className="text-sm font-semibold tracking-tight">
          {t("artedit.title") || "Editar Art Track"}
        </h3>
        <p className="text-xs text-ink-secondary mt-0.5">
          {t("artedit.desc") || "Cambiá la portada, el efecto, el título o la línea legal y volvé a renderizar. Es gratis: no consume tu cuota."}
        </p>
      </div>

      <div className="grid gap-5 sm:grid-cols-[auto,1fr]">
        {/* Portada */}
        <div>
          <label className="block text-label text-ink-secondary mb-1.5">
            {t("artedit.cover") || "Portada"}
          </label>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            className="group relative w-32 h-32 rounded-xl overflow-hidden ring-1 ring-white/[0.08] bg-black grid place-items-center"
            title={t("artedit.change_cover") || "Cambiar portada"}
          >
            {shownCover ? (
              <img src={shownCover} alt="" className="h-full w-full object-cover" />
            ) : (
              <span className="text-[10px] text-gray-500 px-2 text-center">
                {t("artedit.no_cover") || "Sin portada"}
              </span>
            )}
            <span className="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity grid place-items-center text-[11px] font-medium text-white">
              {t("artedit.change_cover") || "Cambiar portada"}
            </span>
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".jpg,.jpeg,.png,image/jpeg,image/png"
            className="hidden"
            onChange={onPickCover}
          />
          {coverFile && (
            <p className="text-[11px] text-brand-light mt-1.5 max-w-[8rem] truncate" title={coverFile.name}>
              {coverFile.name}
            </p>
          )}
        </div>

        {/* Texto */}
        <div className="grid gap-3 content-start">
          <div>
            <label className="block text-label text-ink-secondary mb-1">
              {t("artedit.song_title") || "Título"}
            </label>
            <input
              type="text"
              value={songTitle}
              onChange={(e) => setSongTitle(e.target.value)}
              className="input-field text-sm w-full"
              placeholder={t("artedit.song_title") || "Título"}
            />
          </div>
          <div>
            <label className="block text-label text-ink-secondary mb-1">
              {t("artedit.artist") || "Artista"}
            </label>
            <input
              type="text"
              value={artist}
              onChange={(e) => setArtist(e.target.value)}
              className="input-field text-sm w-full"
              placeholder={t("artedit.artist") || "Artista"}
            />
          </div>
          <div>
            <label className="block text-label text-ink-secondary mb-1">
              {t("artedit.label_line") || "Línea legal (℗/©)"}
            </label>
            <input
              type="text"
              value={labelLine}
              maxLength={120}
              onChange={(e) => setLabelLine(e.target.value)}
              className="input-field text-sm w-full"
              placeholder={t("artedit.label_line_placeholder") || "℗ 2026 Sello / Universal Music"}
            />
            <p className="text-[11px] text-ink-secondary mt-1">
              {t("artedit.label_line_hint") || "Opcional. Se dibuja al pie del video. Dejala vacía para quitarla."}
            </p>
          </div>
        </div>
      </div>

      {/* Efecto */}
      <div className="mt-5">
        <label className="block text-label text-ink-secondary mb-1.5">
          {t("artedit.effect") || "Efecto"}
        </label>
        <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
          {EFFECTS.map((e) => {
            const active = (effect || "") === e.code;
            const sample = e.code ? `/fx_samples/${e.code}.mp4` : null;
            return (
              <button
                key={e.code || "none"}
                type="button"
                onClick={() => setEffect(e.code)}
                onMouseEnter={() => setHoverEffect(e.code)}
                onMouseLeave={() => setHoverEffect(null)}
                onFocus={() => setHoverEffect(e.code)}
                onBlur={() => setHoverEffect(null)}
                aria-label={e.label}
                title={e.label}
                className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                  active
                    ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                    : "border-white/[0.06] hover:border-white/[0.20]"
                }`}
              >
                <div className="aspect-video bg-black relative overflow-hidden">
                  {sample ? (
                    <>
                      <img
                        src={sample.replace(/\.mp4$/, ".jpg")}
                        alt=""
                        className="h-full w-full object-cover pointer-events-none"
                      />
                      {(active || hoverEffect === e.code) && (
                        <video
                          src={sample}
                          className="absolute inset-0 h-full w-full object-cover pointer-events-none"
                          autoPlay preload="auto" loop muted playsInline
                        />
                      )}
                    </>
                  ) : (
                    <div
                      className="w-full h-full grid place-items-center text-gray-500 text-[10px]"
                      style={{ background: "radial-gradient(120% 100% at 50% 0,#241a40,#0b0820)" }}
                    >
                      {t("upload.effect_none") || "Ninguno"}
                    </div>
                  )}
                  {active && (
                    <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-brand grid place-items-center shadow">
                      <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                    </div>
                  )}
                </div>
                <div className="px-2 py-1.5 bg-surface-1">
                  <p className={`text-label leading-tight truncate ${active ? "text-white" : "text-gray-200"}`}>{e.label}</p>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {error && (
        <p className="text-xs text-red-400 mt-4">{error}</p>
      )}

      <div className="flex items-center gap-3 mt-5">
        <button
          onClick={handleSave}
          disabled={saving}
          className="inline-flex items-center justify-center h-11 px-6 rounded-button text-sm font-semibold text-white bg-brand hover:bg-brand/90 disabled:opacity-50 transition-colors"
        >
          {saving ? (
            <>
              <span className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />
              {t("artedit.saving") || "Guardando…"}
            </>
          ) : (
            t("artedit.save") || "Guardar y re-renderizar"
          )}
        </button>
        <p className="text-[11px] text-ink-secondary">
          {t("artedit.free_hint") || "El re-render es gratis y no consume cuota."}
        </p>
      </div>
    </div>
  );
}
