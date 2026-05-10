import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import Listbox from "./Listbox";
import { UploadTour } from "./OnboardingTour";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenParam() {
  const token = localStorage.getItem("genly_token");
  return token ? `token=${encodeURIComponent(token)}` : "";
}

// Maximum tracks per batch. Aligned with the per-tenant backlog cap
// (TENANT_BACKLOG_LIMIT = 5 in main.py:464) — Tomi committed to UMG that
// 5 simultáneos is the launch-window throughput, so the staging UI should
// surface the same number rather than letting the operator queue 10 and
// hit a 429 on the 6th. The backend enforces this server-side regardless.
const MAX_BATCH_SIZE = 5;

// Max single-file size. Mirrors backend MAX_UPLOAD_MB default (100, raised
// from 50 to fit lossless WAV uploads — UMG sends WAV at 16/24-bit PCM,
// which can land at 30-50 MB for a 3-minute track). We reject client-side
// so the user gets immediate feedback instead of a 413 from the server
// after a long upload.
const MAX_FILE_MB = 100;
// Accepted extensions in lower-case (with leading dot). Must stay in sync
// with backend _AUDIO_EXTENSIONS.
const ACCEPTED_EXTS = [".mp3", ".wav"];

// WAV files above this threshold get an amber warning under the filename
// — they upload fine for a single user, but slow connections and
// concurrent batches can hit the API container's edge timeout / memory
// cap and fail with a 502. The warning is informative, not a block:
// UMG broadcast deliverables need lossless input.
const WAV_SOFT_WARN_MB = 30;

// Listbox-shape options (code/label) for the UMG ProRes triplet. The
// underlying values stay the same as before — `code` strings get parsed
// at submit time. Frame sizes are uppercase keys (HD, UHD-4K, …),
// FPS values are numeric strings, ProRes profile codes are integers
// stringified.
const UMG_FRAME_SIZES = [
  { code: "HD",     label: "HD 1920×1080 (16:9)" },
  { code: "UHD-4K", label: "UHD 4K 3840×2160 (16:9)" },
  { code: "DCI-2K", label: "DCI 2K 2048×1080 (256:135)" },
  { code: "DCI-4K", label: "DCI 4K 4096×2160 (256:135)" },
];
const UMG_FPS = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60].map((f) => ({
  code: String(f),
  label: `${f} fps`,
}));
const UMG_PROFILES = [
  { code: "3", label: "ProRes 422 HQ (recommended)" },
  { code: "4", label: "ProRes 4444" },
  { code: "5", label: "ProRes 4444 XQ" },
];

// Gradient swatches mirror _GRADIENT_PALETTES in pipeline.py exactly so
// the UI preview matches what the worker actually renders.
const STYLES = [
  {
    code: "oscuro",
    labelKey: "upload.style_dark",
    subKey: "upload.style_dark_sub",
    swatch: "linear-gradient(135deg, #0a0a1e 0%, #1e0f3c 40%, #501450 70%, #280a32 100%)",
  },
  {
    code: "neon",
    labelKey: "upload.style_neon",
    subKey: "upload.style_neon_sub",
    swatch: "linear-gradient(135deg, #0a0528 0%, #500078 40%, #006482 70%, #780050 100%)",
  },
  {
    code: "minimal",
    labelKey: "upload.style_minimal",
    subKey: "upload.style_minimal_sub",
    swatch: "linear-gradient(135deg, #b4b4c3 0%, #c8bed2 40%, #aab4c8 70%, #d2c8c3 100%)",
  },
  {
    code: "calido",
    labelKey: "upload.style_calido",
    subKey: "upload.style_calido_sub",
    swatch: "linear-gradient(135deg, #3c140a 0%, #8c3c0f 40%, #b45a14 70%, #641e0a 100%)",
  },
];

export default function UploadZone({
  files,
  onFiles,
  delivery,
  onDeliveryChange,
  style = "oscuro",
  onStyleChange,
  backgroundFile,
  onBackgroundFile,
  backgroundId,
  onBackgroundId,
  backgroundMode,
  onBackgroundMode,
  animateImage,
  onAnimateImage,
  allHaveArtist = false,
  onStartReview,
  onGenerateDirect,
  user,
  sidebarOpen = true,
}) {
  const { t } = useI18n();
  const inputRef = useRef();
  const bgInputRef = useRef();
  const [dragging, setDragging] = useState(false);
  // Seed delivery selectors from App-level state when present so coming
  // back from /review (or any remount) preserves the operator's choice
  // of "ProRes 422 HQ" / frame size / fps, not just the file list.
  const [deliveryProfile, setDeliveryProfile] = useState(delivery?.delivery_profile || "youtube");
  // umg_frame_size: now operator-selectable end-to-end. The pipeline
  // renders the source MP4 at the chosen UMG dims+fps (via
  // RenderSpec.umg_intermediate_master) so the lazy ProRes transcode
  // is a pure recode — no ffmpeg upscale, no fps interpolation —
  // guaranteed to satisfy UMG manual QC for any of the 4 frame sizes.
  const [umgFrameSize, setUmgFrameSize] = useState(
    delivery?.umg_frame_size || "HD",
  );
  const [umgFps, setUmgFps] = useState(delivery?.umg_fps || 24);
  const [umgProresProfile, setUmgProresProfile] = useState(delivery?.umg_prores_profile || 3);
  const [deliveryExpanded, setDeliveryExpanded] = useState(false);
  const [bgMode, setBgMode] = useState("auto"); // auto | library | custom
  const [libraryBgs, setLibraryBgs] = useState([]);
  const [libraryLoaded, setLibraryLoaded] = useState(false);
  const [libraryFetchFailed, setLibraryFetchFailed] = useState(false);
  // Library filter chip: all | image | video_cinematic | video_simple
  const [libraryFilter, setLibraryFilter] = useState("all");
  // Per-asset usage map keyed by asset id. Populated lazily once the
  // library list lands so we can paint "ya usado" badges without
  // blocking the picker render. Shape: { [id]: { used, last_used_at,
  // use_count } }.
  const [usageMap, setUsageMap] = useState({});
  // Batch-wide defaults applied to all tracks. Controls in _batchSettingsBlock
  // write here and fan out to every file entry. Per-track "Personalizar"
  // drawer lets an operator override individual fields without affecting others.
  const [batchDefaults, setBatchDefaults] = useState({
    genre: "", concept: "", movementStyle: "", font: "",
    textCase: "upper", fontScale: "1.0", lyricTransition: "cut", textMotion: "none",
  });
  const batchDefaultsRef = useRef(batchDefaults);
  useEffect(() => { batchDefaultsRef.current = batchDefaults; }, [batchDefaults]);

  const updateBatchDefault = (field, value) => {
    setBatchDefaults((prev) => ({ ...prev, [field]: value }));
    onFiles((prev) => prev.map((f) => ({ ...f, [field]: value })));
  };

  // Set of track indices with the inline "Personalizar" drawer open.
  const [expandedPersonalize, setExpandedPersonalize] = useState(() => new Set());
  const togglePersonalize = (idx) => {
    setExpandedPersonalize((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  };

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
        .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(data => {
          const list = Array.isArray(data) ? data : [];
          setLibraryBgs(list);
          setLibraryLoaded(true);
          // Fan out per-asset usage probes. The backend returns the
          // tenant-scoped count, so this is what powers the "ya usado"
          // chip. Failures are swallowed — a missing badge is fine.
          list.forEach((bg) => {
            fetch(`${API}/backgrounds/${bg.id}/usage`, { headers: authHeaders() })
              .then((r) => (r.ok ? r.json() : null))
              .then((u) => { if (u) setUsageMap((prev) => ({ ...prev, [bg.id]: u })); })
              .catch(() => {});
          });
        })
        .catch(() => { setLibraryFetchFailed(true); setLibraryLoaded(true); });
    }
  }, [bgMode, libraryLoaded]);

  // When the user clears the library selection (or switches mode),
  // reset the variation toggle so a fresh pick starts in the safe
  // "as-is" default rather than inheriting the previous song's choice.
  // Also reset when the selected asset is a still — variation requires
  // a video source.
  useEffect(() => {
    if (backgroundMode === "as_is") return;
    if (!backgroundId) {
      onBackgroundMode?.("as_is");
      return;
    }
    const sel = libraryBgs.find((b) => b.id === backgroundId);
    if (sel && sel.file_type !== "mp4") {
      onBackgroundMode?.("as_is");
    }
  }, [backgroundId, backgroundMode, libraryBgs, onBackgroundMode]);

  const _formatUsageDate = (iso) => {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
    } catch { return ""; }
  };

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
    { code: "",            label: t("upload.genre_auto") },
    { code: "rock",        label: t("upload.genre_rock") },
    { code: "pop",         label: t("upload.genre_pop") },
    { code: "ballad",      label: t("upload.genre_ballad") },
    { code: "latin",       label: t("upload.genre_latin") },
    { code: "reggaeton",   label: t("upload.genre_reggaeton") },
    { code: "hiphop",      label: t("upload.genre_hiphop") },
    { code: "electronic",  label: t("upload.genre_electronic") },
    { code: "indie",       label: t("upload.genre_indie") },
    { code: "folk",        label: t("upload.genre_folk") },
    { code: "metal",       label: t("upload.genre_metal") },
  ];

  // Font catalogue for the per-track typography picker. Mirrors the
  // backend's _FONT_CATALOGUE in pipeline.py — the `css` value is what
  // the browser uses to render the option label in its own face,
  // turning the dropdown into a live preview of every typography
  // option without needing a server-side render. UMG operator picks
  // one per song; "Auto" sends an empty value and the worker keeps the
  // existing random/deterministic pick.
  // Movement style for the AI background. Mirror of the backend
  // _MOVEMENT_STYLE_RULES — keep in sync. UMG showed 3 reference videos
  // (Sunset Sounds palm trees / Puro Rock photo+effects / Rebel Rock
  // animated illustration) so we expose 4 explicit options + Auto.
  // The visual sample MP4s for the gallery live at
  // /movement_samples/<id>.mp4 (Vite serves public/ as static).
  // NOTE: those MP4s are LIBRARY PLACEHOLDERS shipped with the first
  // deploy — Tomi swaps real ones in before UMG sees the feature.
  const MOVEMENT_STYLES = [
    { code: "",              label: t("upload.movement_auto") || "Auto",                         sample: null },
    { code: "sutil",         label: t("upload.movement_sutil") || "Sutil (mínimo movimiento)",   sample: "/movement_samples/sutil.mp4" },
    { code: "estandar",      label: t("upload.movement_estandar") || "Estándar (cinematográfico)", sample: "/movement_samples/estandar.mp4" },
    { code: "foto-parallax", label: t("upload.movement_foto_parallax") || "Foto + parallax",     sample: "/movement_samples/foto-parallax.mp4" },
    { code: "animado",       label: t("upload.movement_animado") || "Animado (ilustración)",     sample: "/movement_samples/animado.mp4" },
  ];

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
    { code: "",                label: t("upload.font_auto") || "Auto",     css: "" },
    { code: "jost-bold",       label: "Jost (estilo Futura)",              css: "'Jost', sans-serif",       weight: 700 },
    { code: "montserrat-bold", label: "Montserrat",                        css: "'Montserrat', sans-serif", weight: 700 },
    { code: "poppins-bold",    label: "Poppins",                           css: "'Poppins', sans-serif",    weight: 700 },
    { code: "outfit-bold",     label: "Outfit (estilo Gilroy)",            css: "'Outfit', sans-serif",     weight: 700 },
    { code: "roboto-bold",     label: "Roboto",                            css: "'Roboto', sans-serif",     weight: 700 },
    { code: "bebas-neue",      label: "Bebas Neue",                        css: "'Bebas Neue', sans-serif", weight: 400 },
    { code: "oswald-bold",     label: "Oswald",                            css: "'Oswald', sans-serif",     weight: 700 },
    { code: "anton",           label: "Anton",                             css: "'Anton', sans-serif",      weight: 400 },
  ];

  // Two upload conventions are supported:
  //   "Artist - Title.ext"  → artist=Artist, song=Title
  //   "Title_Artist.ext"    → song=Title,    artist=Artist
  // The `_` form is what Suno / YouTube exports emit, and what the
  // operator was uploading when the title was lost end-to-end. Stripping
  // " (Official Video)" / etc. keeps the lrclib lookup hitting.
  const parseFilename = (filename) => {
    const name = filename.replace(/\.(mp3|wav|m4a|flac|aac|ogg)$/i, "");
    let artist = "";
    let song = name.trim();
    if (name.includes(" - ")) {
      const [head, ...rest] = name.split(" - ");
      artist = head.trim();
      song = rest.join(" - ").trim();
    } else if (name.includes("_")) {
      const [head, ...rest] = name.split("_");
      song = head.trim();
      artist = rest.join("_").trim();
    }
    const noise = [
      "(Official Video)", "(Official Audio)", "(Lyric Video)",
      "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
      "(Live)", "(Lyrics)",
    ];
    for (const sfx of noise) song = song.replace(sfx, "").trim();
    return { artist, song };
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
      const newEntries = accepted.map((f) => {
        const { artist, song } = parseFilename(f.name);
        return {
          file: f,
          artist,
          songTitle: song,
          language: "",
          ...batchDefaultsRef.current,
        };
      });
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

  // Hoist sections into named variables so the JSX below can place them
  // in either single-column (mobile / md) or 2-column (lg+) flow without
  // duplication. The LEFT column owns the primary action (file drop +
  // per-track rows). The RIGHT column owns batch-wide settings (delivery
  // profile, movement-style gallery, background picker). On mobile they
  // stack: LEFT first, then RIGHT.
  // Delivery profile is collapsed by default — most operators never
  // change it from MP4/YouTube. The collapsed pill shows the current
  // value + a "Cambiar" affordance; click expands the listboxes.
  const _deliveryBlock = (
    <div
      data-tour="upload-delivery"
      className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3"
    >
      {!deliveryExpanded ? (
        <button
          type="button"
          onClick={() => setDeliveryExpanded(true)}
          className="w-full flex items-center justify-between text-left"
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
              {t("upload.delivery") || "Entrega"}
            </span>
            <span className="text-sm text-white truncate">
              {deliveryProfile === "youtube"
                ? "MP4 H.264 1080p"
                : `MP4 + ProRes ${umgFrameSize} · ${umgFps} fps`}
            </span>
          </div>
          <span className="text-xs text-brand-light hover:text-brand transition-colors shrink-0">
            {t("common.change") || "Cambiar"}
          </span>
        </button>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
              {t("upload.delivery") || "Entrega"}
            </span>
            <button
              type="button"
              onClick={() => setDeliveryExpanded(false)}
              className="text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
            >
              {t("common.collapse") || "Cerrar"}
            </button>
          </div>
          <div className="flex flex-col gap-2">
            <Listbox
              value={deliveryProfile}
              onChange={(v) => setDeliveryProfile(v)}
              options={[
                { code: "youtube", label: "MP4 H.264 1080p (YouTube / Instagram / TikTok)" },
                { code: "both", label: "MP4 + ProRes 422 HQ (broadcast master)" },
              ]}
              className="w-full sm:w-72"
              ariaLabel={t("upload.delivery") || "Entrega"}
            />
            {deliveryProfile !== "youtube" && (
              <>
                <Listbox
                  value={umgFrameSize}
                  onChange={(v) => setUmgFrameSize(v)}
                  options={UMG_FRAME_SIZES}
                  className="w-full sm:w-64"
                  ariaLabel="UMG frame size"
                />
                <div className="flex gap-2">
                  <Listbox
                    value={String(umgFps)}
                    onChange={(v) => setUmgFps(parseFloat(v))}
                    options={UMG_FPS}
                    className="flex-1"
                    ariaLabel="UMG fps"
                  />
                  <Listbox
                    value={String(umgProresProfile)}
                    onChange={(v) => setUmgProresProfile(parseInt(v, 10))}
                    options={UMG_PROFILES}
                    className="flex-1"
                    ariaLabel="ProRes profile"
                  />
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );

  const _dropZone = (
      <div
        data-tour="upload-dropzone"
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
                  className="mt-1 text-[11px] text-amber-400/60 hover:text-amber-300"
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
                  className="mt-1 text-[11px] text-red-400/60 hover:text-red-300"
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
              {t("upload.size_hint")}
            </p>
          </div>
        )}
      </div>
  );

  const _batchSettingsBlock = files.length > 0 ? (
    <div className="mt-3 glass rounded-card px-4 py-4">
      <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500 mb-3">
        {t("upload.batch_settings_title") || "Configuración del lote"}
      </p>

      {/* Movement gallery — click a card to apply to all tracks */}
      <div className="mb-4">
        <div className="flex items-baseline justify-between mb-2">
          <p className="text-[11px] text-gray-400 font-medium">
            {t("upload.movement_gallery_title") || "Movimiento del fondo"}
          </p>
          <p className="text-[10px] text-gray-600">
            {t("upload.movement_gallery_hint") || "Click para aplicar a todos · personalizable por canción"}
          </p>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {MOVEMENT_STYLES.filter((m) => m.sample).map((m) => {
            const active = batchDefaults.movementStyle === m.code;
            return (
              <button
                key={m.code}
                type="button"
                onClick={() => updateBatchDefault("movementStyle", m.code)}
                aria-label={`${t("upload.movement_apply_all") || "Aplicar a todas"}: ${m.label}`}
                className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer
                  ${active
                    ? "border-brand/60 shadow-glow ring-1 ring-brand/40"
                    : "border-white/[0.06] hover:border-white/[0.20] hover:scale-[1.02]"
                  }`}
              >
                <div className="aspect-video bg-black/30 relative">
                  <video
                    src={m.sample}
                    className="w-full h-full object-cover pointer-events-none"
                    muted autoPlay loop playsInline
                  />
                  {active && (
                    <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-brand flex items-center justify-center shadow">
                      <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                        <polyline points="20 6 9 17 4 12" />
                      </svg>
                    </div>
                  )}
                </div>
                <div className="px-2 py-1.5 bg-surface-1">
                  <p className={`text-[11px] truncate ${active ? "text-white font-medium" : "text-gray-300"}`}>
                    {m.label}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Genre + Concept — only meaningful in AI Auto mode */}
      {bgMode === "auto" && (
        <div className="flex flex-wrap gap-3 mb-4">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-gray-600 shrink-0">{t("upload.genre_label") || "Género:"}</span>
            <Listbox
              value={batchDefaults.genre}
              onChange={(v) => updateBatchDefault("genre", v)}
              options={GENRES}
              className="w-44"
              ariaLabel={t("upload.genre_label") || "Género"}
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-gray-600 shrink-0">{t("upload.concept_label") || "Concepto:"}</span>
            <Listbox
              value={batchDefaults.concept}
              onChange={(v) => updateBatchDefault("concept", v)}
              options={CONCEPTS}
              className="w-44"
              ariaLabel={t("upload.concept_label") || "Concepto"}
            />
          </div>
        </div>
      )}

      {/* Font */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_label") || "Tipografía:"}</span>
        <Listbox
          value={batchDefaults.font}
          onChange={(v) => updateBatchDefault("font", v)}
          options={FONTS}
          className="flex-1"
          ariaLabel={t("upload.font_label") || "Tipografía"}
        />
      </div>

      {/* Text case pill buttons: MAY / Aa / min / ori */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.text_case_label") || "Texto:"}</span>
        <div className="flex gap-1">
          {[
            { code: "upper",    d: "MAY" },
            { code: "title",    d: "Aa"  },
            { code: "lower",    d: "min" },
            { code: "original", d: "ori" },
          ].map((opt) => (
            <button
              key={opt.code}
              type="button"
              onClick={() => updateBatchDefault("textCase", opt.code)}
              className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                ${batchDefaults.textCase === opt.code
                  ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                  : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                }`}
            >{opt.d}</button>
          ))}
        </div>
      </div>

      {/* Font scale — 5 A's in increasing sizes */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_scale_label") || "Tamaño:"}</span>
        <div className="flex items-end gap-1">
          {[
            { code: "0.75", cls: "text-[9px]"  },
            { code: "0.9",  cls: "text-[11px]" },
            { code: "1.0",  cls: "text-[13px]" },
            { code: "1.15", cls: "text-[16px]" },
            { code: "1.3",  cls: "text-[19px]" },
          ].map((opt) => (
            <button
              key={opt.code}
              type="button"
              onClick={() => updateBatchDefault("fontScale", opt.code)}
              className={`w-7 h-7 flex items-center justify-center rounded-md font-bold transition-all ${opt.cls}
                ${batchDefaults.fontScale === opt.code
                  ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                  : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                }`}
            >A</button>
          ))}
        </div>
      </div>

      {/* Lyric transition icon buttons */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.transition_label") || "Transición:"}</span>
        <div className="flex gap-1">
          {[
            { code: "cut",       icon: "│",   label: t("upload.transition_cut")  || "Corte" },
            { code: "fade",      icon: "⟿",  label: t("upload.transition_fade") || "Fade"  },
            { code: "fade_slow", icon: "⟿⟿", label: t("upload.transition_slow") || "Lento" },
          ].map((opt) => (
            <button
              key={opt.code}
              type="button"
              title={opt.label}
              onClick={() => updateBatchDefault("lyricTransition", opt.code)}
              className={`px-2.5 py-1 rounded-md text-[13px] transition-all
                ${batchDefaults.lyricTransition === opt.code
                  ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                  : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                }`}
            >{opt.icon}</button>
          ))}
        </div>
      </div>

      {/* Text motion icon buttons */}
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.motion_label") || "Movimiento del texto:"}</span>
        <div className="flex gap-1">
          {[
            { code: "none",   icon: "·", label: t("upload.motion_none")   || "Estático" },
            { code: "subtle", icon: "↕", label: t("upload.motion_subtle") || "Sutil"    },
            { code: "float",  icon: "∿", label: t("upload.motion_float")  || "Flotante" },
          ].map((opt) => (
            <button
              key={opt.code}
              type="button"
              title={opt.label}
              onClick={() => updateBatchDefault("textMotion", opt.code)}
              className={`px-2.5 py-1 rounded-md text-[13px] transition-all
                ${batchDefaults.textMotion === opt.code
                  ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                  : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                }`}
            >{opt.icon}</button>
          ))}
        </div>
      </div>
    </div>
  ) : null;

  const _filesBlock = files.length > 0 ? (
    <div className="mt-3 space-y-2 max-h-[36rem] overflow-y-auto pr-1">
      {files.map((entry, i) => {
        const isPersonalizing = expandedPersonalize.has(i);
        const bd = batchDefaults;
        const hasDiff =
          (entry.genre        || "") !== (bd.genre        || "") ||
          (entry.concept      || "") !== (bd.concept      || "") ||
          (entry.movementStyle || "") !== (bd.movementStyle || "") ||
          (entry.font         || "") !== (bd.font         || "") ||
          (entry.textCase     || "upper") !== (bd.textCase     || "upper") ||
          (entry.fontScale    || "1.0")   !== (bd.fontScale    || "1.0")   ||
          (entry.lyricTransition || "cut") !== (bd.lyricTransition || "cut") ||
          (entry.textMotion   || "none")  !== (bd.textMotion   || "none");

        return (
          <div key={i} className="glass rounded-card px-4 py-3" {...(i === 0 ? { "data-tour": "upload-row" } : {})}>
            {/* Header */}
            <div className="flex items-center gap-3 mb-2">
              <div className="w-8 h-8 rounded-lg bg-brand/10 flex items-center justify-center shrink-0">
                <svg className="w-4 h-4 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
                </svg>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm text-white truncate">{entry.file.name}</p>
                {entry.file.name.toLowerCase().endsWith(".wav") &&
                 entry.file.size > WAV_SOFT_WARN_MB * 1024 * 1024 && (
                  <p className="text-[11px] text-amber-400/80 mt-0.5 truncate">
                    {t("batch.wav_warning_large", { sizeMB: Math.round(entry.file.size / (1024 * 1024)) })}
                  </p>
                )}
              </div>
              <button
                onClick={(e) => removeFile(i, e)}
                className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-500 hover:text-red-400 transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Core fields */}
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
                <p className="text-[11px] text-amber-400/80">
                  {t("upload.artist_required") || "Nombre del artista es requerido"}
                </p>
              )}
              <input
                type="text"
                value={entry.songTitle || ""}
                onChange={(e) => updateField(i, "songTitle", e.target.value)}
                placeholder={t("upload.song_title") || "Nombre de la canción"}
                className="w-full px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06]
                  focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500 transition-all"
              />
              {!(entry.songTitle || "").trim() && (
                <p className="text-[11px] text-gray-600">
                  {t("upload.song_title_hint") || "Si lo dejás vacío, lo inferimos del nombre del archivo"}
                </p>
              )}
              {/* Language pills */}
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-gray-600 mr-1">{t("lang.auto")}</span>
                {LANGUAGES.filter((l) => l.code).map((l) => (
                  <button
                    key={l.code}
                    type="button"
                    onClick={() => updateField(i, "language", entry.language === l.code ? "" : l.code)}
                    className={`text-[11px] font-bold px-2 py-1 rounded-md transition-all uppercase
                      ${entry.language === l.code
                        ? "bg-brand/20 text-brand"
                        : "text-gray-600 hover:text-gray-400 hover:bg-white/[0.03]"
                      }`}
                  >{l.code}</button>
                ))}
              </div>

              {/* Personalizar toggle */}
              <button
                type="button"
                onClick={() => togglePersonalize(i)}
                className="mt-1 flex items-center gap-1.5 text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
              >
                <span>
                  {isPersonalizing
                    ? (t("upload.fewer_options") || "Cerrar")
                    : (t("upload.personalize_track") || "Personalizar")}
                </span>
                <svg
                  className={`w-3 h-3 transition-transform ${isPersonalizing ? "rotate-180" : ""}`}
                  fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
                >
                  <polyline points="6 9 12 15 18 9" />
                </svg>
                {hasDiff && !isPersonalizing && (
                  <span
                    className="w-1.5 h-1.5 rounded-full bg-brand"
                    title={t("upload.track_overrides_active") || "Tiene ajustes propios"}
                  />
                )}
              </button>

              {/* Per-track override drawer */}
              {isPersonalizing && (
                <div className="mt-2 pt-2 border-t border-white/[0.06] space-y-2">
                  <p className="text-[10px] text-gray-600 uppercase tracking-[0.14em]">
                    {t("upload.personalize_track") || "Personalizar esta canción"}
                  </p>
                  {bgMode === "auto" && (
                    <>
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.genre_label") || "Género:"}</span>
                        <Listbox value={entry.genre || ""} onChange={(v) => updateField(i, "genre", v)} options={GENRES} className="flex-1" ariaLabel={t("upload.genre_label")} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.concept_label") || "Concepto:"}</span>
                        <Listbox value={entry.concept || ""} onChange={(v) => updateField(i, "concept", v)} options={CONCEPTS} className="flex-1" ariaLabel={t("upload.concept_label")} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.movement_label") || "Movimiento:"}</span>
                        <Listbox value={entry.movementStyle || ""} onChange={(v) => updateField(i, "movementStyle", v)} options={MOVEMENT_STYLES} className="flex-1" ariaLabel={t("upload.movement_label")} />
                      </div>
                    </>
                  )}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_label") || "Tipografía:"}</span>
                    <Listbox value={entry.font || ""} onChange={(v) => updateField(i, "font", v)} options={FONTS} className="flex-1" ariaLabel={t("upload.font_label")} />
                  </div>
                  {/* Text case pills */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.text_case_label") || "Texto:"}</span>
                    <div className="flex gap-1">
                      {[
                        { code: "upper", d: "MAY" }, { code: "title", d: "Aa" },
                        { code: "lower", d: "min" }, { code: "original", d: "ori" },
                      ].map((opt) => (
                        <button key={opt.code} type="button"
                          onClick={() => updateField(i, "textCase", opt.code)}
                          className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                            ${(entry.textCase || "upper") === opt.code
                              ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                              : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                            }`}
                        >{opt.d}</button>
                      ))}
                    </div>
                  </div>
                  {/* Font scale */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_scale_label") || "Tamaño:"}</span>
                    <div className="flex items-end gap-1">
                      {[
                        { code: "0.75", cls: "text-[9px]"  }, { code: "0.9",  cls: "text-[11px]" },
                        { code: "1.0",  cls: "text-[13px]" }, { code: "1.15", cls: "text-[16px]" },
                        { code: "1.3",  cls: "text-[19px]" },
                      ].map((opt) => (
                        <button key={opt.code} type="button"
                          onClick={() => updateField(i, "fontScale", opt.code)}
                          className={`w-7 h-7 flex items-center justify-center rounded-md font-bold transition-all ${opt.cls}
                            ${(entry.fontScale || "1.0") === opt.code
                              ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                              : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                            }`}
                        >A</button>
                      ))}
                    </div>
                  </div>
                  {/* Transition */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.transition_label") || "Transición:"}</span>
                    <div className="flex gap-1">
                      {[
                        { code: "cut",       icon: "│",   label: t("upload.transition_cut")  || "Corte" },
                        { code: "fade",      icon: "⟿",  label: t("upload.transition_fade") || "Fade"  },
                        { code: "fade_slow", icon: "⟿⟿", label: t("upload.transition_slow") || "Lento" },
                      ].map((opt) => (
                        <button key={opt.code} type="button" title={opt.label}
                          onClick={() => updateField(i, "lyricTransition", opt.code)}
                          className={`px-2.5 py-1 rounded-md text-[13px] transition-all
                            ${(entry.lyricTransition || "cut") === opt.code
                              ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                              : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                            }`}
                        >{opt.icon}</button>
                      ))}
                    </div>
                  </div>
                  {/* Text motion */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.motion_label") || "Movimiento del texto:"}</span>
                    <div className="flex gap-1">
                      {[
                        { code: "none",   icon: "·", label: t("upload.motion_none")   || "Estático" },
                        { code: "subtle", icon: "↕", label: t("upload.motion_subtle") || "Sutil"    },
                        { code: "float",  icon: "∿", label: t("upload.motion_float")  || "Flotante" },
                      ].map((opt) => (
                        <button key={opt.code} type="button" title={opt.label}
                          onClick={() => updateField(i, "textMotion", opt.code)}
                          className={`px-2.5 py-1 rounded-md text-[13px] transition-all
                            ${(entry.textMotion || "none") === opt.code
                              ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                              : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                            }`}
                        >{opt.icon}</button>
                      ))}
                    </div>
                  </div>
                  {bgMode !== "auto" && (
                    <p className="text-[11px] text-ink-secondary pt-1">
                      {t("upload.fields_baked_into_bg") || "Concepto y movimiento están horneados en el fondo elegido."}
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  ) : null;

  const _bgBlock = (
    <>
      {/* Background selector */}
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
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

          <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">Fondo del video</p>
          <p className="text-[11px] text-gray-600 mb-2 mt-0.5">
            {bgMode === "auto" ? "IA genera un fondo único por canción"
              : bgMode === "library" ? "Fondo compartido para todo el lote"
              : "Tu fondo personalizado"}
          </p>

          {/* Mode selector */}
          <div className="flex gap-1 p-1 glass rounded-xl w-fit mb-3" data-tour="upload-bg-tabs">
            {[
              { id: "auto", label: t("upload.bg_auto") || "IA Auto" },
              { id: "library", label: t("upload.bg_library") || "Library" },
              { id: "custom", label: t("upload.bg_custom_tab") || "Upload" },
            ].map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  setBgMode(m.id);
                  // Clear the OTHER mode's state on every switch — the
                  // upstream consumer (App.jsx ~285) prefers backgroundId
                  // over backgroundFile, so leaving a stale id behind
                  // silently overrides a fresh upload. Picking "library"
                  // discards a previously-uploaded custom file; picking
                  // "custom" discards a previously-selected library id;
                  // "auto" clears both.
                  if (m.id === "auto") {
                    onBackgroundFile?.(null);
                    onBackgroundId?.(null);
                  } else if (m.id === "library") {
                    onBackgroundFile?.(null);
                  } else if (m.id === "custom") {
                    onBackgroundId?.(null);
                  }
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
            <div className="glass rounded-card px-4 py-3">
              <p className="text-xs text-gray-400">
                <svg className="inline-block w-3.5 h-3.5 mr-1.5 -mt-0.5 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
                {t("upload.bg_auto_desc") || "AI will generate a unique background based on the song's mood and lyrics."}
              </p>
            </div>
          )}

          {/* Library mode — full-width grid + asset_type filter chips
              + hover-to-play on video thumbs. Tags carry the
              concept,asset_type pair (e.g. "naturaleza,image") so
              client-side filter is just a substring check. */}
          {bgMode === "library" && (() => {
            const FILTERS = [
              { id: "all",             label: t("upload.library_filter_all")       || "Todos",            match: () => true },
              { id: "image",           label: t("upload.library_filter_images")    || "Imágenes",         match: (b) => b.file_type === "jpg" || b.file_type === "png" },
              { id: "video_cinematic", label: t("upload.library_filter_cinematic") || "Cinematográfico", match: (b) => (b.tags || []).includes("video_cinematic") },
              { id: "video_simple",    label: t("upload.library_filter_animated")  || "Animado",          match: (b) => (b.tags || []).includes("video_simple") },
            ];
            const counts = FILTERS.reduce((acc, f) => {
              acc[f.id] = libraryBgs.filter(f.match).length;
              return acc;
            }, {});
            const visible = libraryBgs.filter(
              (FILTERS.find((f) => f.id === libraryFilter) || FILTERS[0]).match
            );
            return (
              <div>
                {libraryBgs.length === 0 ? (
                  <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-6 text-center">
                    {libraryFetchFailed ? (
                      <>
                        <p className="text-xs text-red-400 mb-2">{t("upload.bg_library_error") || "Error al cargar la biblioteca."}</p>
                        <button
                          type="button"
                          onClick={() => { setLibraryLoaded(false); setLibraryFetchFailed(false); }}
                          className="text-xs text-brand hover:text-brand-light transition-colors font-medium"
                        >
                          {t("upload.retry") || "Reintentar"}
                        </button>
                      </>
                    ) : (
                      <p className="text-xs text-gray-500">{t("upload.bg_library_empty") || "No pre-approved backgrounds available. Ask admin to upload some."}</p>
                    )}
                  </div>
                ) : (
                  <>
                    {/* Filter chips — match the History pill style. */}
                    <div className="flex flex-wrap gap-2 mb-3">
                      {FILTERS.filter((f) => f.id === "all" || counts[f.id] > 0).map((f) => (
                        <button
                          key={f.id}
                          type="button"
                          onClick={() => setLibraryFilter(f.id)}
                          className={`flex items-center gap-2 h-8 px-3 rounded-full text-[11px] font-medium transition-all ${
                            libraryFilter === f.id
                              ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
                              : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
                          }`}
                        >
                          {f.label}
                          <span className={`text-[10px] tabular-nums ${libraryFilter === f.id ? "text-brand-light/80" : "text-gray-500"}`}>
                            {counts[f.id]}
                          </span>
                        </button>
                      ))}
                    </div>
                    {/* Full-width grid; bigger thumbs at all sizes. */}
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                      {visible.map((bg) => {
                        const selected = backgroundId === bg.id;
                        return (
                          <button
                            key={bg.id}
                            onClick={() => { onBackgroundId?.(bg.id); onBackgroundFile?.(null); }}
                            className={`rounded-card overflow-hidden text-left group bg-surface-2/40 transition-all ${
                              selected
                                ? "ring-2 ring-brand shadow-glow"
                                : "ring-1 ring-white/[0.04] hover:ring-white/[0.10] hover:bg-surface-2/70"
                            }`}
                          >
                            <div className="aspect-video bg-black/30 relative">
                              {bg.file_type === "mp4" ? (
                                <video
                                  src={`${API}/backgrounds/${bg.id}/preview?${tokenParam()}`}
                                  className="w-full h-full object-cover"
                                  preload="metadata"
                                  muted loop playsInline
                                  onMouseEnter={(e) => { e.currentTarget.play().catch(() => {}); }}
                                  onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
                                />
                              ) : (
                                <img
                                  src={`${API}/backgrounds/${bg.id}/preview?${tokenParam()}`}
                                  className="w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-500"
                                  alt={bg.name}
                                />
                              )}
                              {selected && (
                                <span className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-brand text-white text-[10px] font-semibold uppercase tracking-wider shadow">
                                  <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                                  {t("upload.bg_selected") || "Seleccionado"}
                                </span>
                              )}
                              {usageMap[bg.id]?.used && (
                                <span
                                  className="absolute top-2 left-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-500/90 text-black text-[10px] font-semibold uppercase tracking-wider shadow"
                                  title={`${t("upload.bg_used_tooltip") || "Usado"} ${usageMap[bg.id].use_count}× — ${_formatUsageDate(usageMap[bg.id].last_used_at)}`}
                                >
                                  {t("upload.bg_used") || "Ya usado"}
                                  {usageMap[bg.id].last_used_at ? ` · ${_formatUsageDate(usageMap[bg.id].last_used_at)}` : ""}
                                </span>
                              )}
                            </div>
                            <div className="px-3 py-2">
                              <p className="text-[12px] text-white truncate">{bg.name}</p>
                            </div>
                          </button>
                        );
                      })}
                    </div>
                    {backgroundId && (() => {
                      const sel = libraryBgs.find((b) => b.id === backgroundId);
                      return sel && sel.file_type === "mp4";
                    })() && (
                      <div className="mt-3 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-3 py-2.5">
                        <p className="text-[11px] text-ink-secondary mb-2">
                          {t("upload.bg_variation_prompt") || "Cómo querés usar este fondo:"}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {[
                            { id: "as_is",     label: t("upload.bg_use_as_is")     || "Usar tal cual" },
                            { id: "variation", label: t("upload.bg_use_variation") || "Generar variación" },
                          ].map((m) => (
                            <button
                              key={m.id}
                              type="button"
                              onClick={() => onBackgroundMode?.(m.id)}
                              className={`h-8 px-3 rounded-full text-[11px] font-medium transition-all ${
                                backgroundMode === m.id
                                  ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
                                  : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
                              }`}
                            >
                              {m.label}
                            </button>
                          ))}
                        </div>
                        {backgroundMode === "variation" && (
                          <p className="mt-2 text-[10.5px] text-ink-secondary/80 leading-snug">
                            {t("upload.bg_variation_help") ||
                              "Veo usará un frame de este video como referencia visual y generará un clip nuevo derivado del original."}
                          </p>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            );
          })()}

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
                <>
                  <div className="glass rounded-card px-4 py-3 flex items-center gap-3">
                    <div className="w-8 h-8 rounded-lg bg-cyan-500/10 flex items-center justify-center shrink-0">
                      <svg className="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" />
                      </svg>
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-white truncate">{backgroundFile.name}</p>
                      <p className="text-[11px] text-cyan-400">{t("upload.custom_bg_active") || "Custom background - AI generation skipped"}</p>
                    </div>
                    <button
                      onClick={() => { onBackgroundFile?.(null); onAnimateImage?.(false); }}
                      className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-500 hover:text-red-400 transition-colors"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <path d="M18 6L6 18M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                  {/* "Animar con AI" — only meaningful for still images
                      (.jpg/.png). Veo 3.1 image-to-video animates the
                      uploaded still while preserving its identity. For
                      video uploads (.mp4/.mov) the toggle stays hidden
                      because the file is already a video. */}
                  {/\.(jpe?g|png)$/i.test(backgroundFile.name) && (
                    <label className="mt-2 flex items-center gap-3 px-3 py-2.5 rounded-xl bg-surface-1 border border-white/[0.06] hover:border-white/[0.12] cursor-pointer transition-colors">
                      {/* Custom iOS-style toggle. Hidden native checkbox
                          drives the state for accessibility; the visual
                          track + thumb are pure Tailwind so the look
                          matches the rest of the dark glassmorphism. */}
                      <input
                        type="checkbox"
                        checked={!!animateImage}
                        onChange={(e) => onAnimateImage?.(e.target.checked)}
                        className="peer sr-only"
                      />
                      <div className="relative w-9 h-5 rounded-full bg-surface-3 peer-checked:bg-brand transition-colors duration-200 shrink-0">
                        <div className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 peer-checked:translate-x-4" />
                      </div>
                      <div className="flex-1">
                        <p className="text-xs text-white font-medium">
                          {t("upload.animate_image_label") || "Animar con AI"}
                        </p>
                        <p className="text-[11px] text-gray-500">
                          {t("upload.animate_image_hint") || "Veo anima tu imagen en lugar de usar zoom/pan"}
                        </p>
                      </div>
                    </label>
                  )}
                </>
              )}
            </div>
          )}
        </div>
    </>
  );

  // Summary line for the sticky CTA bar.
  const summaryParts = [];
  if (files.length > 0) {
    summaryParts.push(`${files.length} ${files.length === 1 ? t("upload.file") : t("upload.files")}`);
  }
  summaryParts.push(deliveryProfile === "youtube" ? "MP4 1080p" : "MP4 + ProRes 1080p");
  if (bgMode === "library" && backgroundId) {
    const sel = libraryBgs.find((b) => b.id === backgroundId);
    if (sel) summaryParts.push(sel.name);
  } else if (bgMode === "custom" && backgroundFile) {
    summaryParts.push(backgroundFile.name.length > 28 ? backgroundFile.name.slice(0, 28) + "…" : backgroundFile.name);
  } else {
    summaryParts.push(t("upload.bg_auto") || "IA Auto");
  }
  const summary = summaryParts.join(" · ");

  return (
    <div className="w-full max-w-5xl mx-auto pb-28">
      <UploadTour user={user} />
      <div className="flex flex-col md:grid md:grid-cols-[minmax(0,1fr)_340px] lg:grid-cols-[minmax(0,1fr)_380px] gap-6 items-start">

        {/* LEFT COLUMN — drop zone + file list */}
        <div className="space-y-4 min-w-0">
          {_dropZone}
          {_filesBlock}
        </div>

        {/* RIGHT COLUMN — batch settings, sticky on md+ */}
        {files.length > 0 && (
          <div className="md:sticky md:top-4 md:self-start space-y-4">

            {/* Batch scope badge */}
            {files.length > 1 && (
              <div className="flex items-center gap-1.5 px-1">
                <span className="inline-flex items-center gap-1.5 text-[10px] text-gray-500 uppercase tracking-[0.16em]">
                  <svg className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                  </svg>
                  Aplica a los {files.length} tracks
                </span>
              </div>
            )}

            {/* 1. Visual style */}
            {onStyleChange && (
              <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
                <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("upload.style_label")}</p>
                <p className="text-[11px] text-gray-600 mb-2 mt-0.5">
                  {t("upload.style_desc") || "Paleta de colores del fondo IA y del gradiente animado"}
                </p>
                <div className="grid grid-cols-2 gap-2">
                  {STYLES.map((s) => (
                    <button
                      key={s.code}
                      type="button"
                      onClick={() => onStyleChange(s.code)}
                      className={`flex flex-col items-center gap-2 px-2 py-2.5 rounded-xl border text-[11px] font-medium transition-all duration-200
                        ${style === s.code
                          ? "border-brand/50 text-white ring-1 ring-brand/40 scale-[1.02]"
                          : "border-white/[0.06] text-gray-400 hover:border-white/[0.16] hover:text-white"
                        }`}
                    >
                      <span
                        className={`w-full h-7 rounded-lg block ring-1 transition-all duration-200 ${
                          style === s.code ? "ring-brand/50 shadow-[0_0_12px_2px_rgba(139,92,246,0.3)]" : "ring-white/[0.06]"
                        }`}
                        style={{ background: s.swatch }}
                      />
                      <span className="leading-tight text-center">
                        <span className="block font-semibold">{t(s.labelKey)}</span>
                        <span className="block text-[10px] text-gray-500">{t(s.subKey)}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 2. Background selector */}
            {_bgBlock}

            {/* 3. Batch-wide controls: movement gallery + text defaults */}
            {_batchSettingsBlock}

            {/* 4. Delivery selector — ProRes gated */}
            {user?.features?.prores_export && _deliveryBlock}
          </div>
        )}
      </div>

      {/* Sticky bottom CTA bar */}
      {files.length > 0 && (
        <div
          className={`fixed bottom-0 left-0 right-0 z-30 bg-surface-1/85 backdrop-blur-xl border-t border-white/[0.06] px-4 md:px-8 py-4 transition-all duration-300 ${sidebarOpen ? "md:left-64" : "md:left-0"}`}
          data-tour="upload-cta-bar"
        >
          <div className="max-w-5xl mx-auto flex flex-wrap items-center gap-3">
            <div className="flex-1 min-w-0">
              <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
                {t("upload.batch_summary") || "Lote"}
              </p>
              <p className="text-sm text-white truncate mt-0.5">{summary}</p>
              {!allHaveArtist && (
                <p className="text-[11px] text-amber-400/80 mt-0.5">
                  {t("upload.complete_artist") || "Completá el nombre del artista en cada archivo"}
                </p>
              )}
            </div>
            {onGenerateDirect && (
              <button
                onClick={onGenerateDirect}
                disabled={!allHaveArtist}
                className="btn-secondary text-xs h-11 px-4 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {t("upload.generate_direct") || "Generar directo"}
              </button>
            )}
            {onStartReview && (
              <button
                onClick={onStartReview}
                disabled={!allHaveArtist}
                className="btn-primary h-11 px-6 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {t("upload.review_lyrics") || "Revisar lyrics"}
                <svg className="inline-block ml-1.5 w-4 h-4 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M5 12h14M12 5l7 7-7 7" />
                </svg>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
