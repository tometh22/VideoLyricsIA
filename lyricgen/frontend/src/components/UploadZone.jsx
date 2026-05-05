import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenParam() {
  const token = localStorage.getItem("genly_token");
  return token ? `token=${encodeURIComponent(token)}` : "";
}

// Maximum tracks per batch. Mirrors the backend's DEFAULT_MAX_CONCURRENT_JOBS
// (10). The backend enforces this server-side too — this is the UX layer that
// stops the user from picking 50 files and getting 40 of them rejected.
const MAX_BATCH_SIZE = 10;

// Max single-file size. Mirrors backend MAX_UPLOAD_MB default (100, raised
// from 50 to fit lossless WAV uploads — UMG sends WAV at 16/24-bit PCM,
// which can land at 30-50 MB for a 3-minute track). We reject client-side
// so the user gets immediate feedback instead of a 413 from the server
// after a long upload.
const MAX_FILE_MB = 100;
// Accepted extensions in lower-case (with leading dot). Must stay in sync
// with backend _AUDIO_EXTENSIONS.
const ACCEPTED_EXTS = [".mp3", ".wav"];

const UMG_FRAME_SIZES = [
  { key: "HD",     label: "HD 1920×1080 (16:9)" },
  { key: "UHD-4K", label: "UHD 4K 3840×2160 (16:9)" },
  { key: "DCI-2K", label: "DCI 2K 2048×1080 (256:135)" },
  { key: "DCI-4K", label: "DCI 4K 4096×2160 (256:135)" },
];
const UMG_FPS = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60];
const UMG_PROFILES = [
  { value: 3, label: "ProRes 422 HQ (recommended)" },
  { value: 4, label: "ProRes 4444" },
  { value: 5, label: "ProRes 4444 XQ" },
];

export default function UploadZone({
  files,
  onFiles,
  onDeliveryChange,
  backgroundFile,
  onBackgroundFile,
  backgroundId,
  onBackgroundId,
}) {
  const { t } = useI18n();
  const inputRef = useRef();
  const bgInputRef = useRef();
  const [dragging, setDragging] = useState(false);
  const [deliveryProfile, setDeliveryProfile] = useState("youtube");
  const [umgFrameSize, setUmgFrameSize] = useState("HD");
  const [umgFps, setUmgFps] = useState(24);
  const [umgProresProfile, setUmgProresProfile] = useState(3);
  const [bgMode, setBgMode] = useState("auto"); // auto | library | custom
  const [libraryBgs, setLibraryBgs] = useState([]);
  const [libraryLoaded, setLibraryLoaded] = useState(false);

  useEffect(() => {
    if (!onDeliveryChange) return;
    onDeliveryChange({
      delivery_profile: deliveryProfile,
      umg_frame_size: umgFrameSize,
      umg_fps: umgFps,
      umg_prores_profile: umgProresProfile,
    });
  }, [deliveryProfile, umgFrameSize, umgFps, umgProresProfile, onDeliveryChange]);

  useEffect(() => {
    if (bgMode === "library" && !libraryLoaded) {
      fetch(`${API}/backgrounds`, { headers: authHeaders() })
        .then(r => r.json())
        .then(data => { setLibraryBgs(Array.isArray(data) ? data : []); setLibraryLoaded(true); })
        .catch(() => setLibraryLoaded(true));
    }
  }, [bgMode, libraryLoaded]);

  const LANGUAGES = [
    { code: "", label: t("lang.auto") },
    { code: "es", label: t("lang.es") },
    { code: "en", label: t("lang.en") },
    { code: "pt", label: t("lang.pt") },
    { code: "fr", label: t("lang.fr") },
    { code: "it", label: t("lang.it") },
    { code: "de", label: t("lang.de") },
  ];

  // Genre passes a hint to Gemini so the AI background lands in the right
  // visual register (rock → urban industrial, latin → tropical, metal →
  // volcanic, etc.). "Auto" lets Gemini classify from artist+title+lyrics.
  // Default is auto so users who don't care don't need to think about it.
  const GENRES = [
    { code: "",            label: t("upload.genre_auto") || "Auto" },
    { code: "rock",        label: "Rock" },
    { code: "pop",         label: "Pop" },
    { code: "ballad",      label: t("upload.genre_ballad") || "Balada" },
    { code: "latin",       label: t("upload.genre_latin") || "Latino" },
    { code: "reggaeton",   label: "Reggaeton" },
    { code: "hiphop",      label: "Hip-Hop / Trap" },
    { code: "electronic",  label: t("upload.genre_electronic") || "Electrónica" },
    { code: "indie",       label: "Indie" },
    { code: "folk",        label: "Folk" },
    { code: "metal",       label: "Metal" },
  ];

  // Font catalogue for the per-track typography picker. Mirrors the
  // backend's _FONT_CATALOGUE in pipeline.py — the `css` value is what
  // the browser uses to render the option label in its own face,
  // turning the dropdown into a live preview of every typography
  // option without needing a server-side render. UMG operator picks
  // one per song; "Auto" sends an empty value and the worker keeps the
  // existing random/deterministic pick.
  // Visual concept for the AI background. Operator-controlled; when set
  // it hard-overrides the genre's scene vocabulary. Mirror of the backend
  // _CONCEPT_SCENE_GUIDE keys in pipeline.py — keep in sync. UMG asked
  // for this on top of genre because the genre alone wasn't tight enough
  // to control the visual register.
  const CONCEPTS = [
    { code: "",             label: t("upload.concept_auto") || "Auto" },
    { code: "naturaleza",   label: t("upload.concept_naturaleza") || "Naturaleza" },
    { code: "tropical",     label: t("upload.concept_tropical") || "Tropical" },
    { code: "acuatico",     label: t("upload.concept_acuatico") || "Acuático" },
    { code: "ciudad",       label: t("upload.concept_ciudad") || "Ciudad" },
    { code: "urbano",       label: t("upload.concept_urbano") || "Urbano" },
    { code: "industrial",   label: t("upload.concept_industrial") || "Industrial" },
    { code: "abstracto",    label: t("upload.concept_abstracto") || "Abstracto" },
    { code: "cosmico",      label: t("upload.concept_cosmico") || "Cósmico" },
    { code: "atmosferico",  label: t("upload.concept_atmosferico") || "Atmosférico" },
    { code: "romantico",    label: t("upload.concept_romantico") || "Romántico" },
    { code: "vintage",      label: t("upload.concept_vintage") || "Vintage" },
    { code: "cinematic",    label: t("upload.concept_cinematic") || "Cinematic" },
    { code: "club",         label: t("upload.concept_club") || "Club" },
    { code: "lujo",         label: t("upload.concept_lujo") || "Lujo" },
    { code: "minimalista",  label: t("upload.concept_minimalista") || "Minimalista" },
  ];

  const FONTS = [
    { id: "",                  label: t("upload.font_auto") || "Auto",     css: "" },
    { id: "jost-bold",         label: "Jost (estilo Futura)",              css: "'Jost', sans-serif",       weight: 700 },
    { id: "montserrat-bold",   label: "Montserrat",                        css: "'Montserrat', sans-serif", weight: 700 },
    { id: "poppins-bold",      label: "Poppins",                           css: "'Poppins', sans-serif",    weight: 700 },
    { id: "outfit-bold",       label: "Outfit (estilo Gilroy)",            css: "'Outfit', sans-serif",     weight: 700 },
    { id: "roboto-bold",       label: "Roboto",                            css: "'Roboto', sans-serif",     weight: 700 },
    { id: "bebas-neue",        label: "Bebas Neue",                        css: "'Bebas Neue', sans-serif", weight: 400 },
    { id: "oswald-bold",       label: "Oswald",                            css: "'Oswald', sans-serif",     weight: 700 },
    { id: "anton",             label: "Anton",                             css: "'Anton', sans-serif",      weight: 400 },
  ];

  const extractArtist = (filename) => {
    const name = filename.replace(/\.(mp3|wav)$/i, "");
    if (name.includes(" - ")) return name.split(" - ")[0].trim();
    return "";
  };

  const [batchTruncated, setBatchTruncated] = useState(0);
  const [oversize, setOversize] = useState([]);

  const addFiles = (fileList) => {
    const mp3s = Array.from(fileList).filter((f) => {
      const lower = f.name.toLowerCase();
      return ACCEPTED_EXTS.some((ext) => lower.endsWith(ext));
    });
    if (!mp3s.length) return;

    const max = MAX_FILE_MB * 1024 * 1024;
    const tooBig = mp3s.filter((f) => f.size > max);
    const okSize = mp3s.filter((f) => f.size <= max);
    if (tooBig.length) setOversize(tooBig.map((f) => f.name));
    else setOversize([]);
    if (!okSize.length) return;

    onFiles((prev) => {
      const remaining = MAX_BATCH_SIZE - prev.length;
      if (remaining <= 0) {
        setBatchTruncated(okSize.length);
        return prev;
      }
      const accepted = okSize.slice(0, remaining);
      const dropped = okSize.length - accepted.length;
      if (dropped > 0) setBatchTruncated(dropped);
      const newEntries = accepted.map((f) => ({
        file: f,
        artist: extractArtist(f.name),
        language: "",
      }));
      return [...prev, ...newEntries];
    });
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const updateField = (idx, field, value) => {
    onFiles((prev) =>
      prev.map((entry, i) => (i === idx ? { ...entry, [field]: value } : entry))
    );
  };

  const removeFile = (idx, e) => {
    e.stopPropagation();
    onFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <div>
      {/* Delivery profile selector — applied to every file in this batch.
          ProRes paths exist in the backend but the multi-GB upload-to-R2
          step is still being hardened, so we expose youtube as the only
          selectable option for now and label the rest "próximamente". */}
      <div className="glass rounded-2xl px-4 py-3 mb-3">
        <div className="flex flex-wrap gap-2 items-center">
          <label className="text-xs text-gray-400 mr-1">{t("upload.delivery") || "Entrega:"}</label>
          <select
            value={deliveryProfile}
            onChange={(e) => setDeliveryProfile(e.target.value)}
            className="px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white"
          >
            <option value="youtube">MP4 H.264 1080p (YouTube / Instagram / TikTok)</option>
            <option value="umg" disabled>ProRes 422 HQ master — próximamente</option>
            <option value="both" disabled>MP4 + ProRes — próximamente</option>
          </select>
          {deliveryProfile !== "youtube" && (
            <>
              <select
                value={umgFrameSize}
                onChange={(e) => setUmgFrameSize(e.target.value)}
                className="px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white"
              >
                {UMG_FRAME_SIZES.map((f) => (
                  <option key={f.key} value={f.key}>{f.label}</option>
                ))}
              </select>
              <select
                value={umgFps}
                onChange={(e) => setUmgFps(parseFloat(e.target.value))}
                className="px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white"
              >
                {UMG_FPS.map((f) => (
                  <option key={f} value={f}>{f} fps</option>
                ))}
              </select>
              <select
                value={umgProresProfile}
                onChange={(e) => setUmgProresProfile(parseInt(e.target.value, 10))}
                className="px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white"
              >
                {UMG_PROFILES.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </>
          )}
        </div>
      </div>

      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current.click()}
        className={`group relative rounded-3xl p-8 text-center cursor-pointer transition-all duration-300
          ${dragging ? "bg-brand/10 border-brand shadow-glow" : files.length > 0 ? "glass" : "glass glass-hover"}
          border-2 ${dragging ? "border-brand" : files.length > 0 ? "border-white/[0.06]" : "border-dashed border-white/[0.08]"}
        `}
      >
        <input
          ref={inputRef} type="file" accept=".mp3,.wav,audio/mpeg,audio/wav,audio/x-wav" multiple className="hidden"
          onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
        />

        {files.length > 0 ? (
          <div onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-gray-400">
                {files.length}/{MAX_BATCH_SIZE} {files.length > 1 ? t("upload.files") : t("upload.file")}
                {files.length >= MAX_BATCH_SIZE && (
                  <span className="ml-2 text-[11px] text-amber-400/80">
                    {t("upload.batch_full") || "batch full"}
                  </span>
                )}
              </span>
              {files.length < MAX_BATCH_SIZE && (
                <button
                  onClick={(e) => { e.stopPropagation(); inputRef.current.click(); }}
                  className="text-xs text-brand hover:text-brand-light transition-colors"
                >{t("upload.add_more")}</button>
              )}
            </div>
            {batchTruncated > 0 && (
              <div className="mt-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20">
                <p className="text-[11px] text-amber-300">
                  {t("upload.batch_truncated", { dropped: batchTruncated, max: MAX_BATCH_SIZE })
                    || `${batchTruncated} file(s) ignored — max ${MAX_BATCH_SIZE} per batch. Process this batch first, then upload the rest.`}
                </p>
                <button
                  onClick={(e) => { e.stopPropagation(); setBatchTruncated(0); }}
                  className="mt-1 text-[10px] text-amber-400/60 hover:text-amber-300"
                >{t("common.dismiss") || "dismiss"}</button>
              </div>
            )}
            {oversize.length > 0 && (
              <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
                <p className="text-[11px] text-red-300">
                  {t("upload.oversize", { max: MAX_FILE_MB }) ||
                    `${oversize.length} archivo(s) excede(n) ${MAX_FILE_MB} MB y fueron ignorados: ${oversize.slice(0,3).join(", ")}${oversize.length > 3 ? "…" : ""}`}
                </p>
                <button
                  onClick={(e) => { e.stopPropagation(); setOversize([]); }}
                  className="mt-1 text-[10px] text-red-400/60 hover:text-red-300"
                >{t("common.dismiss") || "dismiss"}</button>
              </div>
            )}
          </div>
        ) : (
          <div className="py-4">
            <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-surface-3/80 flex items-center justify-center group-hover:bg-brand/10 transition-colors duration-300">
              <svg className="w-7 h-7 text-gray-400 group-hover:text-brand transition-colors duration-300" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            </div>
            <p className="text-gray-300 font-medium mb-1">{t("upload.drag")}</p>
            <p className="text-gray-600 text-sm">{t("upload.drag_sub")}</p>
            <p className="text-gray-700 text-[11px] mt-2">
              {t("upload.size_hint", { max: MAX_FILE_MB }) || `MP3 o WAV, máx ${MAX_FILE_MB} MB por archivo, hasta ${MAX_BATCH_SIZE} por lote`}
            </p>
          </div>
        )}
      </div>

      {files.length > 0 && (
        <div className="mt-3 space-y-2 max-h-96 overflow-y-auto pr-1">
          {files.map((entry, i) => (
            <div key={i} className="glass rounded-2xl px-4 py-3">
              <div className="flex items-center gap-3 mb-2">
                <div className="w-8 h-8 rounded-lg bg-brand/10 flex items-center justify-center shrink-0">
                  <svg className="w-4 h-4 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
                  </svg>
                </div>
                <p className="text-sm text-white truncate flex-1 min-w-0">{entry.file.name}</p>
                <button
                  onClick={(e) => removeFile(i, e)}
                  className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-500 hover:text-red-400 transition-colors"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="space-y-2">
                <input
                  type="text"
                  value={entry.artist}
                  onChange={(e) => updateField(i, "artist", e.target.value)}
                  placeholder={t("upload.artist") + " *"}
                  required
                  className={`w-full px-3 py-1.5 rounded-lg bg-surface-1 border
                    focus:outline-none text-sm text-white placeholder-gray-500 transition-all
                    ${entry.artist.trim() ? "border-white/[0.06] focus:border-brand/50" : "border-amber-500/40 focus:border-amber-400"}`}
                />
                {!entry.artist.trim() && (
                  <p className="text-[10px] text-amber-400/80">
                    {t("upload.artist_required") || "Nombre del artista es requerido"}
                  </p>
                )}
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-gray-600 mr-1">{t("lang.auto")}</span>
                  {LANGUAGES.filter(l => l.code).map((l) => (
                    <button
                      key={l.code}
                      type="button"
                      onClick={() => updateField(i, "language", entry.language === l.code ? "" : l.code)}
                      className={`text-[10px] font-bold px-2 py-1 rounded-md transition-all uppercase
                        ${entry.language === l.code
                          ? "bg-brand/20 text-brand"
                          : "text-gray-600 hover:text-gray-400 hover:bg-white/[0.03]"
                        }`}
                    >
                      {l.code}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <span className="text-[10px] text-gray-600 shrink-0">
                    {t("upload.genre_label") || "Género:"}
                  </span>
                  <select
                    value={entry.genre || ""}
                    onChange={(e) => updateField(i, "genre", e.target.value)}
                    className="flex-1 px-2 py-1 rounded-md bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-[11px] text-white"
                    title={t("upload.genre_hint") || "Ayuda al AI a elegir el fondo correcto"}
                  >
                    {GENRES.map((g) => (
                      <option key={g.code} value={g.code}>{g.label}</option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <span className="text-[10px] text-gray-600 shrink-0">
                    {t("upload.concept_label") || "Concepto:"}
                  </span>
                  <select
                    value={entry.concept || ""}
                    onChange={(e) => updateField(i, "concept", e.target.value)}
                    className="flex-1 px-2 py-1 rounded-md bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-[11px] text-white"
                    title={t("upload.concept_hint") || "Categoría visual del fondo (sobreescribe el género)"}
                  >
                    {CONCEPTS.map((c) => (
                      <option key={c.code || "auto"} value={c.code}>{c.label}</option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <span className="text-[10px] text-gray-600 shrink-0">
                    {t("upload.font_label") || "Tipografía:"}
                  </span>
                  <select
                    value={entry.font || ""}
                    onChange={(e) => updateField(i, "font", e.target.value)}
                    className="flex-1 px-2 py-1 rounded-md bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-[11px] text-white"
                    title={t("upload.font_hint") || "Tipografía del texto en el video"}
                    style={{
                      // Render the selected option's value in its own face
                      // so the field shows the chosen typography directly.
                      fontFamily: (FONTS.find((f) => f.id === (entry.font || ""))?.css) || undefined,
                      fontWeight: (FONTS.find((f) => f.id === (entry.font || ""))?.weight) || undefined,
                    }}
                  >
                    {FONTS.map((f) => (
                      <option
                        key={f.id || "auto"}
                        value={f.id}
                        style={{ fontFamily: f.css || undefined, fontWeight: f.weight || undefined }}
                      >
                        {f.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Background selector */}
      {files.length > 0 && (
        <div className="mt-4">
          <input
            ref={bgInputRef}
            type="file"
            accept=".mp4,.mov,.jpg,.jpeg,.png"
            className="hidden"
            onChange={(e) => {
              if (e.target.files[0]) { onBackgroundFile?.(e.target.files[0]); onBackgroundId?.(null); }
              e.target.value = "";
            }}
          />

          <p className="text-[10px] text-gray-600 uppercase tracking-wider mb-2">{t("upload.bg_label") || "Background"}</p>

          {/* Mode selector */}
          <div className="flex gap-1 p-1 glass rounded-xl w-fit mb-3">
            {[
              { id: "auto", label: t("upload.bg_auto") || "IA Auto" },
              { id: "library", label: t("upload.bg_library") || "Library" },
              { id: "custom", label: t("upload.bg_custom_tab") || "Upload" },
            ].map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  setBgMode(m.id);
                  if (m.id === "auto") { onBackgroundFile?.(null); onBackgroundId?.(null); }
                }}
                className={`px-4 py-1.5 rounded-lg text-[11px] font-medium transition-all ${
                  bgMode === m.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>

          {/* Auto mode */}
          {bgMode === "auto" && (
            <div className="glass rounded-2xl px-4 py-3">
              <p className="text-xs text-gray-400">
                <svg className="inline-block w-3.5 h-3.5 mr-1.5 -mt-0.5 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
                {t("upload.bg_auto_desc") || "AI will generate a unique background based on the song's mood and lyrics."}
              </p>
            </div>
          )}

          {/* Library mode */}
          {bgMode === "library" && (
            <div>
              {libraryBgs.length === 0 ? (
                <div className="glass rounded-2xl px-4 py-6 text-center">
                  <p className="text-xs text-gray-500">{t("upload.bg_library_empty") || "No pre-approved backgrounds available. Ask admin to upload some."}</p>
                </div>
              ) : (
                <div className="grid grid-cols-3 gap-2 max-h-48 overflow-y-auto pr-1">
                  {libraryBgs.map((bg) => (
                    <button
                      key={bg.id}
                      onClick={() => { onBackgroundId?.(bg.id); onBackgroundFile?.(null); }}
                      className={`rounded-xl overflow-hidden border-2 transition-all ${
                        backgroundId === bg.id ? "border-brand shadow-glow" : "border-transparent hover:border-white/10"
                      }`}
                    >
                      <div className="aspect-video bg-black/30">
                        {bg.file_type === "mp4" ? (
                          <video
                            src={`${API}/backgrounds/${bg.id}/preview?${tokenParam()}`}
                            className="w-full h-full object-cover"
                            muted autoPlay loop playsInline
                          />
                        ) : (
                          <img
                            src={`${API}/backgrounds/${bg.id}/preview?${tokenParam()}`}
                            className="w-full h-full object-cover"
                            alt={bg.name}
                          />
                        )}
                      </div>
                      <div className="px-2 py-1.5 bg-surface-1">
                        <p className="text-[10px] text-white truncate">{bg.name}</p>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Custom upload mode */}
          {bgMode === "custom" && (
            <div>
              {!backgroundFile ? (
                <button
                  onClick={() => bgInputRef.current.click()}
                  className="w-full rounded-2xl border border-dashed border-white/[0.06] px-4 py-4 text-center hover:border-brand/30 hover:bg-brand/5 transition-all"
                >
                  <p className="text-xs text-gray-500">
                    <svg className="inline-block w-3.5 h-3.5 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" />
                    </svg>
                    {t("upload.custom_bg") || "Custom Background"} — MP4, MOV, JPG, PNG
                  </p>
                </button>
              ) : (
                <div className="glass rounded-2xl px-4 py-3 flex items-center gap-3">
                  <div className="w-8 h-8 rounded-lg bg-cyan-500/10 flex items-center justify-center shrink-0">
                    <svg className="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" />
                    </svg>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-white truncate">{backgroundFile.name}</p>
                    <p className="text-[10px] text-cyan-400">{t("upload.custom_bg_active") || "Custom background - AI generation skipped"}</p>
                  </div>
                  <button
                    onClick={() => onBackgroundFile?.(null)}
                    className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-500 hover:text-red-400 transition-colors"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
