import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import Listbox from "./Listbox";
import { UploadTour } from "./OnboardingTour";
import WizardLivePreview from "./WizardLivePreview";
import TitleCardPreview, { AUTO_INTRO_THRESHOLD_S } from "./TitleCardPreview";
import HelpTip from "./HelpCenter/HelpTip";
import ContentValidationToggle, { isUniversalAccount } from "./ContentValidationToggle";
import { track } from "../lib/telemetryTrack";
import { inspiredByLyricsForSceneMode } from "../lib/sceneMode";
import { CONCEPT_CODES, MOVEMENT_CODES } from "../lib/catalogCodes";
import useBackgroundPreviewTokens, { backgroundPreviewUrl } from "../hooks/useBackgroundPreviewTokens";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Maximum tracks per batch. Aligned with the per-tenant backlog cap
// (TENANT_BACKLOG_LIMIT = 5 in main.py:464) — Tomi committed to UMG that
// 5 simultáneos is the launch-window throughput, so the staging UI should
// surface the same number rather than letting the operator queue 10 and
// hit a 429 on the 6th. The backend enforces this server-side regardless.
const MAX_BATCH_SIZE = 5;

// SHOW_MOTION_PICKER (legacy text_motion) eliminado 2026-05-23 — campo
// deprecado upstream; ver pipeline.py:run_pipeline.

// Typography (font / case / size / transition / contrast) is now chosen
// LIVE in the editor + preview, where you see the real result. Hidden in
// the upload step to remove the confusing duplication. batchDefaults still
// supplies sensible defaults for the "Generar directo" path.
const SHOW_UPLOAD_TYPOGRAPHY = false;

// Field sets used by the central preview auto-switch (UI v1.1, 2026-05-30):
// touching one of these flips the preview face so the operator sees the
// change without having to click anything. Keep in sync with the actual
// controls inside the Portada and typography sections below.
const TITLE_CARD_FIELDS = new Set([
  "titleTemplate", "titleSize", "titleArtistFont", "titleSongFont",
  "titleSongBreak",
]);
const LYRIC_RENDER_FIELDS = new Set([
  "font", "textCase", "fontScale", "textContrast", "frameFormat",
  "lyricsAnimation", "lineTransition", "lyricColor", "lyricSungColor",
]);

function applyTextCase(text, c) {
  if (c === "upper") return text.toUpperCase();
  if (c === "title") return text.replace(/\b\w/g, (ch) => ch.toUpperCase());
  if (c === "lower") return text.toLowerCase();
  if (c === "sentence") {
    return text.toLowerCase().split("\n").map(
      (ln) => ln.replace(/[a-zà-ÿ]/i, (ch) => ch.toUpperCase())
    ).join("\n");
  }
  return text;
}
const TEXT_CASE_OPTS = [
  { code: "upper",    d: "MAY", label: "Todo en MAYÚSCULAS" },
  { code: "title",    d: "Aa",  label: "Primera letra de Cada Palabra" },
  { code: "lower",    d: "min", label: "todo en minúsculas" },
  { code: "sentence", d: "Abc", label: "Primera letra de cada Línea" },
  { code: "original", d: "ori", label: "Sin cambios (como está escrito)" },
];
// Frame format: full 16:9 (default) vs cinemascope 2.39:1 letterbox. The
// letterbox is applied deterministically in post (see pipeline._apply_frame_format)
// — an intentional filmic look, not Veo's stochastic bars.
const FRAME_FORMAT_OPTS = [
  { code: "full", d: "16:9", label: "Pantalla completa (16:9)" },
  { code: "cine", d: "2.39", label: "Cine — franjas (2.39:1)" },
];

// Max single-file size. Backend MAX_UPLOAD_MB default is 500 and the
// audio body goes browser->R2 (presigned), so the API never sees these
// bytes — this client-side cap only bounds what we let the operator
// queue. Raised 100 -> 150 on 2026-07-02: UMG uploads lossless WAV
// masters (24-bit/48 kHz stereo ≈ 17 MB/min) and a 6-min track already
// blew past 100 (real case: 107 MB Intoxicados WAV rejected silently).
// We reject client-side so the user gets immediate feedback instead of
// a 413 from the server after a long upload.
const MAX_FILE_MB = 150;
// Accepted extensions in lower-case (with leading dot). Must stay in sync
// with backend _AUDIO_EXTENSIONS.
const ACCEPTED_EXTS = [".mp3", ".wav"];

// Caps del FONDO custom. A diferencia del audio (directo a R2), el fondo
// viaja en el FormData de /generate a través del edge proxy — un MOV de
// 300 MB muere en el timeout del edge con "Failed to fetch" opaco, o
// sube entero para comerse un 413. Video 150 MB (coherente con el cap
// de audio), imagen 25 MB.
const MAX_BG_VIDEO_MB = 150;
const MAX_BG_IMAGE_MB = 25;

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
  style = "auto",
  onStyleChange,
  customColors = "",
  onCustomColorsChange,
  // Add-on premium "Escenas" (multi-escena). Sólo se renderiza el toggle si
  // user.features.scenes; el backend re-valida igual.
  enableScenes = false,
  onEnableScenesChange,
  // Art track ("official audio"): tipo de video = master audio + cover, sin
  // letra. Cuando está activo se oculta la parte de letra y el CTA genera
  // directo (art_track=true).
  artTrack = false,
  onArtTrackChange,
  onGenerateArtTrack,
  backgroundFile,
  onBackgroundFile,
  backgroundId,
  onBackgroundId,
  backgroundMode,
  onBackgroundMode,
  // Bug Ana M. 2026-06-09: el tab de fondo (auto/library/custom) ahora es
  // estado de App, no local. Con useState local, un remount del wizard
  // volvía el tab a "auto" visualmente mientras App conservaba el
  // backgroundFile del batch anterior — y lo mandaba a /generate igual.
  bgMode = "auto",
  onBgMode,
  animateImage,
  onAnimateImage,
  inspiredByLyrics = true,
  onInspiredByLyricsChange,
  allHaveArtist = false,
  onStartReview,
  onGenerateDirect,
  user,
  sidebarOpen = true,
  // Prefetch de transcripción disparado al AVANZAR del paso "Subí" (no al
  // drop): cuando el operador deja el paso 1 hacia adelante, la fuente de
  // letra + la letra oficial ya están elegidas por canción, así el POST
  // /transcribe-uploaded sale con el anchor_lyrics correcto. App corre las
  // uploads + transcripciones en background. La status map per file viene
  // del App via filenamekey.
  onUploadAdvance,
  // Edge case (a): editar la fuente de letra / letra oficial de una canción
  // tras haber avanzado invalida el prefetch de ESE archivo (App borra su
  // entrada de cache) para forzar el re-transcribe con el estado nuevo.
  onInvalidatePrefetch,
  transcribeStatusByFile = {},
  // Phase 2 (2026-05-25): render prop para el paso 6 ("Lyrics"). App.jsx
  // sigue siendo dueño del state machine de review (transcribing,
  // currentReview, readyToGenerate, empty state) — UploadZone solo le
  // presta el layout de 3 columnas del wizard. Cuando hasReviewableContent
  // se prende, el wizard avanza a step 6 automáticamente y renderiza
  // renderStep6() en la columna derecha. WizardLivePreview persiste
  // sticky en el centro durante todo el flow.
  hasReviewableContent = false,
  renderStep6 = null,
  // Phase 3 (2026-05-25): segments de la canción en review. Si vienen,
  // el WizardLivePreview central muestra una línea real de la letra en
  // vez del título genérico — el operador ve cómo se va a ver el video
  // con su propia letra durante toda la review.
  reviewSegments = null,
  // Phase C 2026-05-25: ref que apunta a {activeLine, activeStart, activeEnd,
  // currentTime}. Cuando el operador clickea play en LyricsEditor (paso 6),
  // el ref se actualiza a 60fps con la línea que está sonando. El
  // WizardLivePreview lo lee con su propio rAF para renderizar word-jump
  // sincronizado al audio real, sin causar re-renders de UploadZone.
  playbackTickRef = null,
  // 2026-07-16 (idea de Tomi): callback ref que recibe el <div> slot que
  // montamos bajo el video en el paso 6. LyricsEditor portalea ahí su player
  // bar, así la columna de la letra queda full y se scrollea menos.
  onPlayerSlotRef = null,
  // Post-render edit mode (App.jsx EditLyricsRoute):
  // - lockedSteps: IDs de pasos no navegables (típicamente [1,2,3,5] en
  //   modo edición de un job ya renderizado — esos cambios requieren
  //   regenerar fondo y los cubre el modo "background" de EditRequestPanel).
  //   Los pasos lockeados se ven greyed y el botón hace bail-out.
  // - renderedVideoUrl: MP4 ya renderizado del job; cuando viene se
  //   forwardea al WizardLivePreview que muta a modo "Resultado actual".
  lockedSteps = [],
  renderedVideoUrl = null,
  // UI F5 (2026-05-26): status del pre-gen del fondo. Si !== "done"
  // mientras estamos en paso 6, el preview muestra el badge "(muestra)"
  // en vez del "EN VIVO" pulsante — el operador ve un fondo placeholder
  // hasta que apruebe y genere.
  bgStatus = null,
  // QA fix 2026-05-27 (edit-wizard mode): cuando el operador entra al
  // wizard via /videos/:id/edit-lyrics, ciertos campos structural
  // (paleta, custom_colors) no pueden cambiarse post-render aunque su
  // step (Modo / step 2) sea navegable. `editMode` controla locks por
  // field — el wizard sigue mostrando el control para que el operador
  // VEA el valor actual, pero con overlay + tooltip "no editable".
  editMode = false,
  // Wizard de "Crear variante" (App.jsx VariantWizardRoute). Es un
  // sub-modo de `editMode`: los dos montan el wizard sobre un job que ya
  // existe, así que `editMode` sigue siendo el flag de "modo
  // no-creación" (locks de pasos, semilla de campos, sin tab "Subir el
  // mío", sin Multi-escena) y `variantMode` sólo cambia lo que difiere:
  //   - el costo NO es "1 de tus 3 ediciones" sino 1 video del plan;
  //   - no hay toggle "Generar otra versión" (una variante SIEMPRE
  //     genera fondo nuevo, es su razón de ser);
  //   - la paleta (style) SÍ es editable — /variant acepta `style`,
  //     /edit no.
  variantMode = false,
  // Callback opcional para wizard en edit mode: cuando un control de
  // step 2/3/4 escribe a batchDefaults (el path normal new-job), también
  // forward el cambio a currentReview vía este callback. Sin esto, los
  // cambios de background_hint/movement/effect no llegan al diff de
  // submitEdit porque batchDefaults sólo fan-out a files[] y en edit
  // mode files=[].
  onEditFieldChange = null,
  // En edición, valores persistidos del job (de currentReview) para sembrar los
  // controles de escena que leen batchDefaults (género/concepto/prompt) + el
  // modo de escena. Sin esto mostraban el sticky de localStorage o el prompt
  // vacío, no lo que el video realmente tiene. { jobId, genre, concept,
  // backgroundHint, bgVerbatim, matchLyrics }. No toca r.* (el diff usa
  // initialFields), sólo el display.
  editSeed = null,
  // Cómo está RENDERIZADO el video hoy (currentReview.baseline). Alimenta el
  // chip "EN EL VIDEO" de las galerías: el anillo violeta dice "lo que elegí",
  // el chip ámbar dice "lo que el video tiene". Cuando coinciden, una tarjeta
  // lleva los dos; cuando no, se marcan dos y el cambio queda dibujado.
  // El anillo solo era la señal que engañó al operador del reclamo original.
  editBaseline = null,
  // UI v1.1 (2026-05-30): artist/song to render inside the central
  // title-card preview. Caller (App.jsx) passes these from the
  // currentReview when in edit mode, or from the first file when in
  // batch mode. Empty strings render the preview's "— —" placeholder.
  titlePreviewArtist = "",
  titlePreviewSong = "",
}) {
  const { t } = useI18n();
  const inputRef = useRef();
  const bgInputRef = useRef();
  const [dragging, setDragging] = useState(false);
  // Seed delivery selectors from App-level state when present so coming
  // back from /review (or any remount) preserves the operator's choice
  // of "ProRes 422 HQ" / frame size / fps, not just the file list.
  const [deliveryProfile, setDeliveryProfile] = useState(delivery?.delivery_profile || "youtube");
  // Art track: línea legal opcional en pantalla (℗/© sello), per-batch.
  const [labelLine, setLabelLine] = useState(delivery?.label_line || "");
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
  // Aviso de fondo custom rechazado por tamaño (nombre + límite aplicado).
  const [bgOversize, setBgOversize] = useState(null);
  // bgMode viene de App via props (ver header) — acá sólo un alias para
  // que el resto del componente siga llamando setBgMode.
  // Audit A2: multi-escena sólo aplica a "Generar con IA" (genera clips Veo).
  // Al cambiar a Biblioteca/Subir (fondo fijo), apagamos el toggle para que no
  // quede estado muerto (el backend ya lo ignora si hay bg_image_path, pero
  // dejarlo ON sin control visible confunde).
  const setBgMode = (m) => {
    if (m !== "auto" && enableScenes) onEnableScenesChange && onEnableScenesChange(false);
    onBgMode?.(m);
  };
  const [libraryBgs, setLibraryBgs] = useState([]);
  const libraryPreviewTokens = useBackgroundPreviewTokens(libraryBgs.map((bg) => bg.id), API);
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
  //
  // Persistence: we mirror the picks to localStorage so a re-upload (same
  // browser, same user) restores the operator's last choices instead of
  // resetting font/style/concept/etc. to hardcoded defaults. Confirmed in
  // prod that without this, the same song uploaded twice ends up with
  // completely different render_params each time (font, text_case,
  // concept all rotated arbitrarily) — which is why Mujer Amante kept
  // losing the "ROMANTICO" concept across Agus's re-uploads.
  const BATCH_DEFAULTS_STORAGE_KEY = "genly:wizardBatchDefaultsV1";
  const HARDCODED_BATCH_DEFAULTS = {
    genre: "", concept: "", movementStyle: "", effect: "", font: "",
    // lyricTransition + textMotion: deprecados 2026-05-23 (no se persisten).
    textCase: "upper", fontScale: "1.0", lyricsAnimation: "none", lineTransition: "none", textContrast: "medium",
    frameFormat: "full",
    // Lyric color customization 2026-05-25:
    // - lyricColor: color del texto (no-cantada para karaoke; texto único para
    //   none/pop/glow/word_reveal).
    // - lyricSungColor: solo aplica a karaoke = color de la palabra cantada.
    // Default blanco para no romper jobs viejos sin estos params.
    lyricColor: "#FFFFFF", lyricSungColor: "#FFFFFF",
    // Escena axis: optional free-text prompt ("Mi prompt"). When non-empty it
    // overrides genre/concept/lyrics. bgVerbatim TRUE by default = use the
    // operator's text as-is (people expect their prompt used, not rewritten);
    // the "Mejorar con IA" toggle opts INTO a Gemini rewrite (bgVerbatim=false).
    backgroundHint: "", bgVerbatim: true,
    // Title card customization (Full Rotor v1). Defaults = historical look:
    // auto layout, no size change, artist ExtraBold, song = lyric font.
    titleTemplate: "auto", titleSize: "1.0", titleArtistFont: "", titleSongFont: "",
    // UI v1.1 (2026-05-30): manual song-title break. "" = auto wrap.
    titleSongBreak: "",
  };
  const loadStoredBatchDefaults = () => {
    try {
      const raw = localStorage.getItem(BATCH_DEFAULTS_STORAGE_KEY);
      if (!raw) return HARDCODED_BATCH_DEFAULTS;
      const parsed = JSON.parse(raw);
      // Merge keeps any future fields safe-defaulted when the user has an
      // older saved object missing the new key.
      //
      // PERO el "Mi prompt" (backgroundHint + bgVerbatim) es POR-CANCIÓN, no
      // un sticky de estilo: un prompt de escena que el operador escribió para
      // una canción reaparecía en el batch SIGUIENTE (y se aplicaba a temas que
      // no le pegan, o forzaba el modo "Mi prompt" sin querer). Persistimos los
      // sticky de estilo (tipografía/tamaño/movimiento/case/…) pero forzamos el
      // prompt LIMPIO en cada carga. El override va DESPUÉS del spread de
      // `parsed`, así también limpia el localStorage ya contaminado.
      return {
        ...HARDCODED_BATCH_DEFAULTS,
        ...parsed,
        backgroundHint: HARDCODED_BATCH_DEFAULTS.backgroundHint,
        bgVerbatim: HARDCODED_BATCH_DEFAULTS.bgVerbatim,
      };
    } catch {
      return HARDCODED_BATCH_DEFAULTS;
    }
  };
  const [batchDefaults, setBatchDefaults] = useState(loadStoredBatchDefaults);
  const batchDefaultsRef = useRef(batchDefaults);
  useEffect(() => { batchDefaultsRef.current = batchDefaults; }, [batchDefaults]);

  // UI v1.1 (2026-05-30): which face of the preview is showing on the
  // central sticky slot — the lyric (default) or the title card. We
  // auto-flip to "title" when the operator touches any control in the
  // Portada section so the effect is immediately visible without them
  // having to click a tab. Resets to "lyrics" when they touch a lyric/
  // animation control again. The toggle pill above the preview lets
  // the operator override either way.
  const [previewFace, setPreviewFace] = useState("lyrics");
  // Discoverability fix (incidente Clari 19-jul, "no encuentro dónde
  // agrandar el título"): los controles de la portada (Disposición +
  // Tamaño del título) viven AL FONDO del panel de tipografía, debajo
  // de todos los controles de la letra — hay que scrollear para verlos.
  // Cuando el operador mira la cara "Portada" del preview (por la
  // pestaña o por auto-flip), llevamos su vista a esos controles y los
  // resaltamos un instante, así el control existente (que ya es fiel y
  // llega al render) queda a la vista.
  const portadaControlsRef = useRef(null);
  const [portadaControlsPulse, setPortadaControlsPulse] = useState(false);
  useEffect(() => {
    if (previewFace !== "title") return;
    const el = portadaControlsRef.current;
    if (!el) return; // controles no montados en este paso — no-op
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setPortadaControlsPulse(true);
    const tid = setTimeout(() => setPortadaControlsPulse(false), 1800);
    return () => clearTimeout(tid);
  }, [previewFace]);
  // Wrap updateBatchDefault below so any field that belongs to the
  // Portada bucket flips the preview. Defined inline to capture the
  // setter without juggling refs.

  const updateBatchDefault = (field, value) => {
    setBatchDefaults((prev) => {
      const next = { ...prev, [field]: value };
      // El sticky es para BATCHES NUEVOS. Editar un video existente no puede
      // reescribirlo: si no, arreglar el fondo de un video contamina el
      // siguiente upload con los ajustes de ESE video — y encima al volver a
      // editar otro job el sticky contaminado es lo que se pintaba (el bug que
      // originó esto). En edición/variante los controles se siembran del job.
      if (!editMode) {
        try {
          localStorage.setItem(BATCH_DEFAULTS_STORAGE_KEY, JSON.stringify(next));
        } catch {
          // Quota exceeded / private-mode storage off — the picks still work
          // in-session, they just won't survive a refresh. Don't block the UI.
        }
      }
      return next;
    });
    onFiles((prev) => prev.map((f) => ({ ...f, [field]: value })));
    // QA fix 2026-05-27: en edit mode files=[] así que el fan-out de
    // arriba es no-op. Sin esto, los cambios de background_hint /
    // movement / effect / typography que el operador hace en steps 2-4
    // nunca llegan a currentReview, y submitEdit (handleApproveLyrics)
    // los pierde al computar el diff. App.jsx mapea field→currentReview.
    if (editMode && onEditFieldChange) {
      onEditFieldChange(field, value);
    }
    // UI v1.1 (2026-05-30): when the operator touches a Portada field,
    // flip the central preview to "title" so the change is visible
    // immediately. When they touch a lyric / animation field, flip back
    // to "lyrics" — that's where the karaoke + drag controls live.
    if (TITLE_CARD_FIELDS.has(field)) {
      setPreviewFace("title");
    } else if (LYRIC_RENDER_FIELDS.has(field)) {
      setPreviewFace("lyrics");
    }
  };

  const [hoverCaseBatch, setHoverCaseBatch] = useState(null);
  const [hoverCaseRow, setHoverCaseRow] = useState(null); // { idx, code }
  // "Regenerar fondo (nueva versión)" en edición: intención explícita de re-tirar
  // el fondo con el mismo hint. Se propaga a currentReview vía onEditFieldChange
  // (NO updateBatchDefault: no debe persistir en localStorage — es una acción,
  // no un sticky default). Fixea el caso "quería otra versión y decía 'No
  // cambiaste nada'".
  const [regenRequested, setRegenRequested] = useState(false);
  const toggleRegenRequested = () => {
    const next = !regenRequested;
    setRegenRequested(next);
    onEditFieldChange?.("forceBackgroundRegen", next);
  };
  // Fondo-libre (bypass del validador de contenido) en la edición del fondo.
  // Como forceBackgroundRegen es ACCIÓN (no sticky default): se propaga a
  // currentReview vía onEditFieldChange y App lo inyecta en el payload sólo si
  // el edit es un regen IA (edit_type="background"). true = validar (default);
  // false = fondo-libre (bypass, sólo cuentas no-UMG; UMG tiene política fija).
  // El MOTOR (Veo/Imagen) NO se elige acá: lo define el estilo de Movimiento
  // ("Foto fija"→Imagen, resto→Veo), así que no duplicamos el control.
  const [regenValidation, setRegenValidation] = useState(true);
  const setRegenValidationChoice = (v) => {
    setRegenValidation(v);
    onEditFieldChange?.("bgRegenValidation", v);
  };

  // ── Scene MODE (Studio Console redesign) ────────────────────────────────
  // The 3 modes map onto existing state (no new backend contract):
  //   auto   → match_lyrics=false, no hint
  //   lyrics → match_lyrics=true,  no hint  (our edge: scene from the lyrics)
  //   prompt → background_hint present (+ optional bgVerbatim "usar tal cual")
  // Derive the current mode from that state, with a local override so the
  // operator can open "Mi prompt" before typing anything.
  const _hint = (batchDefaults.backgroundHint || "").trim();
  const [sceneMode, setSceneMode] = useState(_hint ? "prompt" : (inspiredByLyrics ? "lyrics" : "auto"));
  // En edición, sembrar los controles de escena (leen batchDefaults) con los
  // valores REALES del job — género/concepto/prompt mostraban el sticky de
  // localStorage (o prompt vacío por el force-clear), no lo del video. Keyed en
  // el job id: corre UNA vez por job, no pisa ediciones en curso. NO llama
  // onEditFieldChange (r.* ya viene correcto de initialFields) → solo display.
  useEffect(() => {
    if (!editMode || !editSeed) return;
    setBatchDefaults((prev) => ({
      ...prev,
      genre: editSeed.genre || "",
      concept: editSeed.concept || "",
      backgroundHint: editSeed.backgroundHint || "",
      bgVerbatim: editSeed.bgVerbatim != null ? !!editSeed.bgVerbatim : prev.bgVerbatim,
      // `wizardFields` ahora llega SIEMPRE (edición y variante). En variante es
      // obligatorio porque el submit manda el estado ABSOLUTO. En edición el
      // submit es un diff, así que la semilla no cambia qué viaja — pero SÍ
      // cambia lo que el operador ve, y ver el sticky de otro batch en vez del
      // valor de este video es exactamente lo que hacía que no clickeara y el
      // render saliera igual que antes.
      ...(editSeed.wizardFields || {}),
    }));
    setSceneMode(
      (editSeed.backgroundHint || "").trim()
        ? "prompt"
        : (editSeed.matchLyrics ? "lyrics" : "auto"),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editMode, editSeed?.jobId]);

  // ── Ancla "EN EL VIDEO" ──────────────────────────────────────────────────
  // El anillo violeta + check ya es una señal fortísima de "seleccionado" — y
  // es justamente por eso que el operador confió en él y no clickeó. Marcar
  // *diferencias* no lo salva: la señal que lo habría salvado es la AUSENCIA de
  // marca, y eso nadie lo parsea. Así que marcamos el presente en vez del
  // cambio: dos estados dentro de la misma mirada, "lo que elegí" (violeta) y
  // "lo que el video tiene" (ámbar). Cuando coinciden, una tarjeta lleva los
  // dos; cuando no, se marcan dos y el cambio queda dibujado.
  const ANCHOR_LABEL = t("upload.anchor_in_video") || "En el video";
  const isAnchor = (field, code) => {
    if (!editMode || !editBaseline) return false;
    return String(editBaseline[field] ?? "") === String(code ?? "");
  };
  const AnchorChip = () => (
    <span
      className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wide bg-amber-500/20 text-amber-300 ring-1 ring-amber-500/40 backdrop-blur-sm"
      title={t("upload.anchor_in_video_hint") || "Es lo que este video tiene ahora"}
    >
      {ANCHOR_LABEL}
    </span>
  );

  // Elegibilidad del add-on premium "Escenas" (multi-escena). Robusto: el
  // backend manda vía features.scenes (admin OR SCENES_ENABLED_TENANTS), pero
  // los admin siempre califican aunque la sesión cacheada no traiga el flag.
  const scenesEligible = user?.features?.scenes === true || user?.role === "admin";
  // Art Track gateado por tenant (default OFF salvo admin). Si no califica,
  // no mostramos el selector de tipo de video (queda solo lyric, como antes
  // de la feature) y reseteamos artTrack si vino prendido de un estado viejo.
  // Kill-switch de build: Art Track NO va a producción (2026-07-22). El build
  // de prod (genly.pro) no setea VITE_ART_TRACK_ENABLED → la feature queda
  // totalmente oculta (ni admins la ven). Se habilita por entorno para testeo
  // (staging: VITE_ART_TRACK_ENABLED=true); ahí sigue gateada por feature/admin.
  const ART_TRACK_ENABLED = import.meta.env.VITE_ART_TRACK_ENABLED === "true";
  const artTrackEligible =
    ART_TRACK_ENABLED && (user?.features?.art_track === true || user?.role === "admin");
  useEffect(() => {
    if (artTrack && !artTrackEligible) onArtTrackChange?.(false);
  }, [artTrack, artTrackEligible]);
  const [showScenesUpsell, setShowScenesUpsell] = useState(false);
  // Costo en créditos del add-on Escenas. El backend lo expone en
  // features.scenes_credit_cost; default 3 (valor de lanzamiento) si la sesión
  // cacheada no lo trae aún.
  const scenesCreditCost = user?.features?.scenes_credit_cost ?? 3;
  // Badge "Nuevo" + beacon hasta que el usuario interactúe con la card de
  // Escenas (descubrimiento en contexto). Se persiste para no repetir; si
  // localStorage falla, default a "ya visto" para no molestar.
  const [scenesSeen, setScenesSeen] = useState(() => {
    try { return localStorage.getItem("genly_scenes_seen") === "1"; } catch { return true; }
  });
  const markScenesSeen = () => {
    setScenesSeen(true);
    try { localStorage.setItem("genly_scenes_seen", "1"); } catch { /* storage bloqueado */ }
  };
  // Escape cierra el modal de upsell (a11y — audit NIT).
  useEffect(() => {
    if (!showScenesUpsell) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setShowScenesUpsell(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showScenesUpsell]);
  // Preview del fondo subido por el usuario (audit 2026-06-11): antes el
  // operador generaba a ciegas — el branch custom del preview era un
  // placeholder "Fondo subido". Object URL con revoke en cleanup; el guard
  // de .slice cubre los File stubs restaurados post-refresh.
  const [customPreviewUrl, setCustomPreviewUrl] = useState(null);
  useEffect(() => {
    if (!backgroundFile || typeof backgroundFile.slice !== "function") {
      setCustomPreviewUrl(null);
      return undefined;
    }
    const url = URL.createObjectURL(backgroundFile);
    setCustomPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [backgroundFile]);
  const selectSceneMode = (m) => {
    track("wizard.scene_mode", { mode: m });
    setSceneMode(m);
    // Keep the legacy `match_lyrics` payload deterministic. "Mi prompt"
    // must not inherit whichever card happened to be selected before it.
    // Public payload fields remain unchanged: Auto/Prompt=false, Lyrics=true.
    const _ml = inspiredByLyricsForSceneMode(m);
    onInspiredByLyricsChange && onInspiredByLyricsChange(_ml);
    // En edición, el modo de escena (Auto/Inspirado/Mi prompt) SÍ es editable:
    // propagamos match_lyrics a currentReview para que el diff lo mande al
    // /edit (computeFieldDiff → bucket background). onInspiredByLyricsChange
    // solo toca el estado top-level del flujo de creación, no currentReview.
    if (editMode) onEditFieldChange?.("matchLyrics", _ml);
    if (m === "auto") {
      if (_hint) updateBatchDefault("backgroundHint", "");   // stale prompt must not override
    } else if (m === "lyrics") {
      if (_hint) updateBatchDefault("backgroundHint", "");
    }
    // Nota: multi-escena (enableScenes) es ORTOGONAL — funciona con cualquiera
    // de los 3 modos; su toggle premium vive debajo de las cards.
  };
  // Sample lyric for the live preview: first file's title, else a placeholder.
  // Phase 3 (2026-05-25): si estamos en review (reviewSegments presente),
  // mostrar una línea real de la letra en el preview central — la primera
  // línea no-vacía. Da contexto visual de la canción específica que el
  // operador está editando. Sin review, fallback al título genérico.
  const _reviewFirstLine = (() => {
    if (!reviewSegments || !Array.isArray(reviewSegments)) return "";
    for (const s of reviewSegments) {
      const text = (s?.text || "").trim();
      if (text) return text;
    }
    return "";
  })();
  const _previewLyric = _reviewFirstLine || (files[0]?.songTitle || files[0]?.title || "").trim();

  // Start (seconds) of the first sung line, mirroring the backend's
  // `first_lyric_start = segments[0]["start"]` (ass_render.title_card_lines).
  // Lets TitleCardPreview resolve the "auto" title template the same way the
  // render does (long intro → centered hero, short intro → compact badge)
  // instead of always assuming the hero. null when transcription hasn't
  // produced segments yet; the preview falls back to the hero and self-corrects
  // once they arrive.
  const _firstLyricStart =
    Array.isArray(reviewSegments) && typeof reviewSegments[0]?.start === "number"
      ? reviewSegments[0].start
      : null;

  // ── Studio Console stepper ─────────────────────────────────────────────
  // 4 steps revealed one at a time (variant A): the left rail navigates,
  // the center stage holds the live preview, the right panel shows only the
  // active step's controls. Step 1 (Subí) gates advancing on the artist name.
  // WIZARD_STEPS — Phase 1+2 (2026-05-25). Paso 6 "Lyrics" está SIEMPRE
  // en el stepper pero su interactividad depende de hasReviewableContent
  // (la prop que App.jsx prende cuando empieza el transcribe o hay
  // currentReview con segments).
  // - hasReviewableContent=false → paso 6 con border dashed gris,
  //   cursor-not-allowed, tooltip "Disponible después de Revisar lyrics".
  // - hasReviewableContent=true  → paso 6 clickeable; auto-advance del
  //   wizard a step=6 vía el useEffect de abajo.
  const WIZARD_STEPS = [
    { id: 1, label: t("upload.step_upload") || "Subí" },
    { id: 2, label: t("upload.step_mode") || "Modo" },
    { id: 3, label: t("upload.step_motion") || "Movimiento" },
    { id: 4, label: t("upload.step_animation") || "Tipografía & Animación" },
    { id: 5, label: t("upload.step_deliver") || "Entregá" },
    { id: 6, label: t("upload.step_lyrics") || "Lyrics" },
  ];
  // En modo post-render edit (lockedSteps no vacío + content reviewable
  // pre-seeded) arrancamos directo en step 6 para que el operador no vea
  // un flash del paso 1 mientras el useEffect de auto-advance se acomoda.
  //
  // RACE GUARD 2026-05-27 (fix/edit-lyrics-bootstrap-race): en prod un bug
  // ponía esto en step 1 ("Crear videos") cuando el operador abría
  // /videos/X/edit-lyrics y currentReview se reseteaba a null por race.
  // Con lockedSteps.length>0 sabemos que el padre ya intentó montarnos en
  // modo post-render edit; el currentReview puede llegar después (Phase B
  // del bootstrap), pero el step 6 ya es el correcto desde el initial
  // mount. NO REQUIERE hasReviewableContent. Para wizard nuevo (no edit),
  // lockedSteps es [] así que sigue arrancando en step 1.
  const [wizardStep, setWizardStep] = useState(() => {
    if (Array.isArray(lockedSteps) && lockedSteps.length > 0) return 6;
    return 1;
  });
  // Step 6 es clickeable cuando hay contenido de review activo O cuando
  // estamos en modo post-render edit (lockedSteps set). Cuando no hay,
  // el cap es step 5 (los pasos 1-5 son siempre clickables).
  const _maxInteractiveStep =
    (hasReviewableContent || (Array.isArray(lockedSteps) && lockedSteps.length > 0))
      ? 6 : 5;
  // lockedSteps (post-render edit): IDs no navegables. goStep bail-outs y
  // los helpers prev/next saltean los locked para que la sticky bar muestre
  // el siguiente paso navegable real, no uno que el operador no puede usar.
  // Art track: el movimiento (paso 3), la tipografía (paso 4) y la letra
  // (paso 6) NO aplican — el estilo es fijo (cover blureada + cover con sombra
  // + waveform), sin estilos de movimiento ni efectos. Se bloquean para que la
  // navegación los saltee (mismo mecanismo que el edit mode). Quedan: 1 (Subí),
  // 2 (Modo/cover) y 5 (Entregá).
  const _lockedSet = new Set([...lockedSteps, ...(artTrack ? [3, 4, 6] : [])]);
  const _findPrevUnlocked = (n) => {
    for (let i = n - 1; i >= 1; i--) if (!_lockedSet.has(i)) return i;
    return null;
  };
  const _findNextUnlocked = (n) => {
    for (let i = n + 1; i <= _maxInteractiveStep; i++) if (!_lockedSet.has(i)) return i;
    return null;
  };
  const goStep = (n) => {
    const clamped = Math.max(1, Math.min(_maxInteractiveStep, n));
    if (_lockedSet.has(clamped)) return;
    if (clamped !== wizardStep) {
      track("wizard.step", { step_from: wizardStep, step_to: clamped, trigger: "nav" });
    }
    // Prefetch de transcripción al dejar el paso "Subí" hacia adelante: la
    // fuente de letra + la letra oficial ya están elegidas por canción, así
    // que el POST /transcribe-uploaded sale con el anchor_lyrics correcto
    // (fix bug staging e77f84aefe33). Solo en el wizard nuevo (sin
    // lockedSteps) y solo al AVANZAR desde el paso 1.
    if (wizardStep === 1 && clamped > 1 && _lockedSet.size === 0
        && typeof onUploadAdvance === "function") {
      onUploadAdvance();
    }
    setWizardStep(clamped);
  };
  // Auto-advance a step 6 cuando aparece contenido de review O cuando
  // lockedSteps indica modo post-render edit. Y bajar a step 5 si el
  // operador clickea "Volver" y desaparece TODO el contexto de review.
  //
  // RACE GUARD 2026-05-27: agregamos lockedSteps al check porque en el
  // bug reportado, currentReview tarda en llegar (Phase B del bootstrap
  // de EditLyricsRoute) pero lockedSteps ya viene set desde el primer
  // render. Sin esta defensa, hay una ventana donde el editor monta en
  // step 1 ("Crear videos") visible al operador.
  const _editMode = Array.isArray(lockedSteps) && lockedSteps.length > 0;
  useEffect(() => {
    if ((hasReviewableContent || _editMode) && wizardStep !== 6) {
      setWizardStep(6);
    } else if (!hasReviewableContent && !_editMode && wizardStep === 6) {
      setWizardStep(5);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasReviewableContent, _editMode]);

  // Hovering a movement option previews it in the big stage without committing.
  const [hoverMovement, setHoverMovement] = useState(null);
  const [hoverEffect, setHoverEffect] = useState(null);
  // Same for the lyrics-animation step: hover previews the template live.
  const [hoverAnimation, setHoverAnimation] = useState(null);
  // And for the line-transition picker (lives in the same Animación step).
  const [hoverTransition, setHoverTransition] = useState(null);
  // Abstract motion icons — communicate the camera MOVEMENT, not a fake scene.
  // The big live preview is what actually demonstrates the motion.
  const movIcon = (code) => {
    const p = { fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round", viewBox: "0 0 24 24", className: "w-5 h-5" };
    switch (code) {
      case "estatico": // locked frame
        return (<svg {...p}><rect x="5" y="7" width="14" height="10" rx="1.5" /><path d="M5 10V8.5M5 14v1.5M19 10V8.5M19 14v1.5M9 7H7.5M15 7h1.5M9 17H7.5M15 17h1.5" /></svg>);
      case "sutil": // gentle zoom — small inward arrows
        return (<svg {...p}><rect x="4.5" y="6.5" width="15" height="11" rx="1.5" /><path d="M10 10l-1.5-1.5M14 10l1.5-1.5M10 14l-1.5 1.5M14 14l1.5 1.5" /></svg>);
      case "estandar": // strong push-in — bold inward arrows from corners
        return (<svg {...p}><rect x="3.5" y="5.5" width="17" height="13" rx="1.5" /><path d="M8.5 9.5L6 7m0 0v2.2M6 7h2.2M15.5 9.5L18 7m0 0v2.2M18 7h-2.2M8.5 14.5L6 17m0 0v-2.2M6 17h2.2M15.5 14.5L18 17m0 0v-2.2M18 17h-2.2" /></svg>);
      case "foto-parallax": // foto fija — static photo icon (no camera motion)
        return (<svg {...p}><rect x="4" y="5" width="16" height="14" rx="1.5" /><circle cx="9" cy="10" r="1.6" /><path d="M4 16l4.5-4 3 2.5L15 11l5 5" /></svg>);
      case "animado": // stylised 2D shapes
        return (<svg {...p}><circle cx="8" cy="9" r="2.4" /><path d="M14.5 6.5l3.5 5h-7z" /><rect x="9" y="14" width="6" height="4" rx="1" /></svg>);
      default: // auto — sparkle
        return (<svg {...p}><path d="M12 4l1.4 4 4 1.4-4 1.4L12 15l-1.4-4-4-1.4 4-1.4z" /><path d="M18 14l.6 1.7 1.7.6-1.7.6L18 18.6l-.6-1.7-1.7-.6 1.7-.6z" /></svg>);
    }
  };

  // Looping mini-demo of a lyrics-animation template, shown inside each card.
  // The big WizardLivePreview (←) shows the chosen one full-size; these just
  // hint the motion. CSS keyframes are injected once in the Animación step.
  const animDemo = (code) => {
    const base = "font-extrabold tracking-tight text-white text-[15px] leading-none";
    if (code === "karaoke") {
      return (
        <span className={base}>
          {t("upload.sample_words").split(" ").map((w, i) => (
            <span key={i} style={{ animation: `acard-karaoke 2.4s ${i * 0.5}s infinite`, marginRight: i === 0 ? "0.28em" : 0, display: "inline-block" }}>{w}</span>
          ))}
        </span>
      );
    }
    if (code === "word_reveal") {
      return (
        <span className={base}>
          {t("upload.sample_words").split(" ").map((w, i) => (
            <span key={i} style={{ animation: `acard-word 2.6s ${i * 0.45}s infinite`, marginRight: i === 0 ? "0.28em" : 0, display: "inline-block" }}>{w}</span>
          ))}
        </span>
      );
    }
    const anim =
      code === "pop" ? "acard-pop 2.2s infinite" :
      code === "glow" ? "acard-glow 2.4s ease-in-out infinite" :
      "acard-word 2.8s infinite"; // none → simple fade loop
    return <span className={base} style={{ animation: anim, display: "inline-block" }}>{t("upload.preview_lyric")}</span>;
  };

  // Looping mini-demo of a line transition (movement), shown inside its card.
  const transDemo = (code) => {
    const base = "font-extrabold tracking-tight text-white text-[15px] leading-none";
    const anim =
      code === "slide_up" ? "tcard-slideup 2.4s infinite" :
      code === "slide_side" ? "tcard-slideside 2.4s infinite" :
      code === "wipe" ? "tcard-wipe 2.6s infinite" :
      code === "dissolve_blur" ? "tcard-blur 2.6s infinite" :
      "acard-word 2.8s infinite"; // none → simple fade
    return (
      <span className="overflow-hidden inline-block">
        <span className={base} style={{ animation: anim, display: "inline-block" }}>{t("upload.preview_lyric")}</span>
      </span>
    );
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

  // Versión B (letra anclada): selector prominente de dos opciones por
  // canción — "Transcripción con IA de Genly" (default) vs "Tengo la letra
  // oficial" (expande el textarea; viaja como `anchor_lyrics` en
  // /transcribe-uploaded y el backend la ancla al motor CTC). La elección
  // vive en entry.lyricsSource ("auto" | "official") — App.jsx solo manda
  // anchor_lyrics cuando la selección es "official", así volver a la IA
  // desactiva el anclado sin perder el texto pegado. Gate por
  // features.anchor_lyrics (ANCHOR_LYRICS_ENABLED en el server): sin flag
  // no se muestra, así no prometemos una sincronización que el backend va
  // a ignorar. Iteración #908→v2 (feedback dueño de producto 15/07): el
  // toggle colapsado era invisible y no comunicaba la alternativa.
  const anchorLyricsEligible = user?.features?.anchor_lyrics === true;

  useEffect(() => {
    if (!onDeliveryChange) return;
    onDeliveryChange({
      delivery_profile: deliveryProfile,
      umg_frame_size: umgFrameSize,
      umg_fps: umgFps,
      umg_prores_profile: umgProresProfile,
      label_line: labelLine,
      // Art track moving effect (batch-wide). The art-track submit path
      // builds its own FormData and reads it from here (the lyric path
      // sends per-song effect instead).
      effect: batchDefaults.effect || "",
    });
  }, [deliveryProfile, umgFrameSize, umgFps, umgProresProfile, labelLine, batchDefaults.effect, onDeliveryChange]);

  useEffect(() => {
    if (bgMode === "library" && !libraryLoaded) {
      fetch(`${API}/backgrounds`, { headers: authHeaders() })
        .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(data => {
          const list = Array.isArray(data) ? data : [];
          setLibraryBgs(list);
          setLibraryLoaded(true);
          // Un solo GET batch para el "ya usado" de toda la grilla.
          // Incidente 2026-06-11: el fan-out anterior (un GET por asset)
          // escaló a 80 requests simultáneos al crecer la biblioteca y
          // agotó el pool de Postgres (SSL drops en Sentry, thumbnails
          // sin cargar). Failures swallowed — un badge ausente es fine.
          fetch(`${API}/backgrounds/usage`, { headers: authHeaders() })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => { if (data?.usage) setUsageMap(data.usage); })
            .catch(() => {});
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

  // Idiomas soportados. Spanish primero porque ~95% del catálogo target
  // (Universal Music Argentina) es en español; cualquier auto-detect que
  // se confunda termina disparando coverage_warning y timing sintetizado.
  // El default del entry.language al cargar un archivo es "es" (ver más
  // abajo en parseFilename); "auto" sigue disponible como opt-out
  // explícito para canciones en otros idiomas.
  const LANGUAGES = [
    { code: "es", label: t("lang.es") },
    { code: "en", label: t("lang.en") },
    { code: "pt", label: t("lang.pt") },
    { code: "", label: t("lang.auto") },
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
  // 2026-05-25 (operador UMG): clarificadas las definiciones —
  // estatico/sutil/estandar/animado son ESCENAS REALES generadas por
  // Veo (video). Solo "foto-parallax" produce una foto estática IA con
  // pan lateral. Cada card lleva metadata `kind` (video|image|auto) +
  // emoji prefix (🎬 vs 🖼) en la descripción para que el operador vea
  // de un vistazo cuál es video real vs foto IA con paneo.
  // Los CÓDIGOS viven en lib/catalogCodes.js (contrato de paridad con
  // pipeline._MOVEMENT_STYLE_RULES, asertado por renderParity.test.js);
  // acá solo la metadata de UI por código.
  const MOVEMENT_META = {
    estatico:        { kind: "video", label: t("upload.movement_estatico") || "Estático (escena viva)",     sample: "/movement_samples/estatico.mp4",  desc: t("upload.movement_estatico_desc") || "🎬 Escena real con cámara FIJA. Lo que se mueve son los elementos de la escena (gente, olas, nubes, neblina, fuego)." },
    sutil:           { kind: "video", label: t("upload.movement_sutil") || "Sutil (cámara apenas drift)",   sample: "/movement_samples/sutil.mp4",     desc: t("upload.movement_sutil_desc") || "🎬 Escena real con drift sutil de cámara + motion in-scene. Calmo pero vivo." },
    estandar:        { kind: "video", label: t("upload.movement_estandar") || "Estándar (cinematográfico)", sample: "/movement_samples/estandar.mp4",  desc: t("upload.movement_estandar_desc") || "🎬 Escena real con movimiento cinematográfico de cámara (zoom/drift)." },
    "foto-parallax": { kind: "image", label: t("upload.movement_foto_parallax") || "Foto fija",             sample: "/movement_samples/foto-fija.jpg", desc: t("upload.movement_parallax_desc") || "Foto IA fija (sin movimiento de cámara). Sumale un efecto abajo —lluvia, nieve, luces— para darle vida." },
    animado:         { kind: "video", label: t("upload.movement_animado") || "Animado (ilustración)",       sample: "/movement_samples/animado.mp4",   desc: t("upload.movement_animado_desc") || "🎬 Ilustración 2D estilizada animada, no fotorrealista." },
  };
  const MOVEMENT_STYLES = [
    { code: "", kind: "auto", label: t("upload.movement_auto") || "Auto", sample: null, desc: t("upload.movement_auto_desc") || "La IA decide el movimiento según la canción." },
    ...MOVEMENT_CODES.map((code) => ({
      code,
      ...(MOVEMENT_META[code] || { kind: "video", label: code, sample: null, desc: "" }),
    })),
  ];

  // Effect overlay — animated particles composited OVER the background (the
  // proven UMG pattern: foto/loop calmo + nieve/lluvia/estrellas encima). It's
  // an ORTHOGONAL axis to "Movimiento" (which moves the camera): the effect
  // falls on top of anything, even a still photo or a Library/uploaded clip.
  // Backed by pre-rendered alpha-screen loops; preview clips live at
  // /fx_samples/<code>.mp4 (effect composited over a neutral gradient).
  const EFFECTS = [
    { code: "",       label: t("upload.effect_none") || "Ninguno",     sample: null,                     desc: t("upload.effect_none_desc") || "Fondo limpio, sin efecto." },
    { code: "snow",   label: t("upload.effect_snow") || "Nieve",       sample: "/fx_samples/snow.mp4",   desc: t("upload.effect_snow_desc") || "Copos cayendo. Calmo, invernal." },
    { code: "rain",   label: t("upload.effect_rain") || "Lluvia",      sample: "/fx_samples/rain.mp4",   desc: t("upload.effect_rain_desc") || "Gotas finas sobre la escena." },
    { code: "stars",  label: t("upload.effect_stars") || "Estrellas",  sample: "/fx_samples/stars.mp4",  desc: t("upload.effect_stars_desc") || "Partículas que titilan. Nocturno." },
    { code: "bokeh",  label: t("upload.effect_bokeh") || "Bokeh",      sample: "/fx_samples/bokeh.mp4",  desc: t("upload.effect_bokeh_desc") || "Luces desenfocadas flotando." },
    { code: "light",  label: t("upload.effect_light") || "Luz",        sample: "/fx_samples/light.mp4",  desc: t("upload.effect_light_desc") || "Destellos suaves. Atardecer, glow." },
    // 2026-06-04: "Aurora" removido del selector — su asset (assets/fx/aurora.mp4)
    // es una COPIA de light.mp4, así que renderizaba idéntico a "Luz". El backend
    // sigue soportando effect="aurora" (EFFECTS en fx_compositor.py) por compat,
    // pero no lo ofrecemos hasta tener un loop de aurora propio (cortinas
    // ondulantes verde/teal). Para re-activarlo: restaurar la entrada de abajo.
    // { code: "aurora", label: t("upload.effect_aurora") || "Aurora", sample: "/fx_samples/aurora.mp4", desc: t("upload.effect_aurora_desc") || "Líneas de luz ondulantes que cruzan el cielo." },
  ];

  // Lyrics-animation templates. These are rendered as libass override tags in
  // the same single ffmpeg pass as the static text → zero impact on render
  // speed/quality (no moviepy slow path). 🎤 = word-level: needs per-word
  // timing, which the backend SYNTHESIZES from the line window when real word
  // data is absent, so every template works on any song.
  const LYRICS_ANIMATIONS = [
    { code: "none",        emoji: "",   label: t("upload.anim_none") || "Ninguna",   desc: t("upload.anim_none_desc") || "Corte limpio entre líneas." },
    { code: "karaoke",     emoji: "🎤", label: t("upload.anim_karaoke") || "Karaoke", desc: t("upload.anim_karaoke_desc") || "Las palabras se colorean al ritmo que se cantan." },
    { code: "word_reveal", emoji: "🎤", label: t("upload.anim_reveal") || "Revelado", desc: t("upload.anim_reveal_desc") || "Cada palabra aparece justo cuando se canta." },
    { code: "pop",         emoji: "",   label: t("upload.anim_pop") || "Pop",        desc: t("upload.anim_pop_desc") || "La línea entra con un pequeño rebote." },
    { code: "glow",        emoji: "",   label: t("upload.anim_glow") || "Glow",      desc: t("upload.anim_glow_desc") || "Brillo suave que late. Atmosférico." },
  ];

  // Line-to-line MOTION transitions. Orthogonal to the animation (they use
  // position/clip/blur, not scale/colour) so any combination composes. Also
  // libass tags in the same single pass → no pipeline/speed impact.
  const LINE_TRANSITIONS = [
    { code: "none",          label: t("upload.trans_none") || "Corte",       desc: t("upload.trans_none_desc") || "Sin movimiento entre líneas." },
    { code: "slide_up",      label: t("upload.trans_slide_up") || "Slide ↑", desc: t("upload.trans_slide_up_desc") || "La línea entra desde abajo subiendo." },
    { code: "slide_side",    label: t("upload.trans_slide_side") || "Slide →", desc: t("upload.trans_slide_side_desc") || "La línea entra desde un costado." },
    { code: "wipe",          label: t("upload.trans_wipe") || "Wipe",        desc: t("upload.trans_wipe_desc") || "Se descubre de izquierda a derecha." },
    { code: "dissolve_blur", label: t("upload.trans_blur") || "Disolvencia", desc: t("upload.trans_blur_desc") || "Entra desenfocada y se enfoca." },
  ];

  // Visual concept for the AI background. Operator-controlled; when set
  // it hard-overrides the genre's scene vocabulary. Mirror of the backend
  // _CONCEPT_SCENE_GUIDE keys in pipeline.py — keep in sync. UMG asked
  // for this on top of genre because the genre alone wasn't tight enough
  // to control the visual register.
  // Los CÓDIGOS viven en lib/catalogCodes.js (contrato de paridad con
  // pipeline._CONCEPT_SCENE_GUIDE, asertado por renderParity.test.js).
  const CONCEPT_LABELS = {
    naturaleza:  t("upload.concept_naturaleza") || "Naturaleza",
    tropical:    t("upload.concept_tropical") || "Tropical",
    acuatico:    t("upload.concept_acuatico") || "Acuático",
    ciudad:      t("upload.concept_ciudad") || "Ciudad",
    urbano:      t("upload.concept_urbano") || "Urbano",
    industrial:  t("upload.concept_industrial") || "Industrial",
    abstracto:   t("upload.concept_abstracto") || "Abstracto",
    cosmico:     t("upload.concept_cosmico") || "Cósmico",
    atmosferico: t("upload.concept_atmosferico") || "Atmosférico",
    romantico:   t("upload.concept_romantico") || "Romántico",
    vintage:     t("upload.concept_vintage") || "Vintage",
    cinematic:   t("upload.concept_cinematic") || "Cinematic",
    club:        t("upload.concept_club") || "Club",
    lujo:        t("upload.concept_lujo") || "Lujo",
    minimalista: t("upload.concept_minimalista") || "Minimalista",
  };
  const CONCEPTS = [
    { code: "", label: t("upload.concept_auto") || "Auto" },
    ...CONCEPT_CODES.map((code) => ({ code, label: CONCEPT_LABELS[code] || code })),
  ];

  const FONTS = [
    { code: "",                label: t("upload.font_auto") || "Auto",     css: "" },
    { code: "fredoka",         label: "Fredoka (redondeada)",              css: "'Fredoka', sans-serif",    weight: 600 },
    { code: "quicksand",       label: "Quicksand (suave)",                 css: "'Quicksand', sans-serif",  weight: 700 },
    { code: "nunito",          label: "Nunito (amigable)",                 css: "'Nunito', sans-serif",     weight: 800 },
    { code: "jost-bold",       label: "Jost (estilo Futura)",              css: "'Jost', sans-serif",       weight: 700 },
    { code: "montserrat-bold", label: "Montserrat",                        css: "'Montserrat', sans-serif", weight: 700 },
    { code: "poppins-bold",    label: "Poppins",                           css: "'Poppins', sans-serif",    weight: 700 },
    { code: "outfit-bold",     label: "Outfit (estilo Gilroy)",            css: "'Outfit', sans-serif",     weight: 700 },
    { code: "roboto-bold",     label: "Roboto",                            css: "'Roboto', sans-serif",     weight: 700 },
    { code: "bebas-neue",      label: "Bebas Neue",                        css: "'Bebas Neue', sans-serif", weight: 400 },
    { code: "oswald-bold",     label: "Oswald",                            css: "'Oswald', sans-serif",     weight: 700 },
    { code: "anton",           label: "Anton",                             css: "'Anton', sans-serif",      weight: 400 },
  ];

  // Filename → artist/title heuristic. ONE convention now: the FIRST
  // half (left of the separator) is the ARTIST. Same rule for both ` - `
  // and `_` separators. Noise suffixes ("Official Video", "Live", etc.)
  // are stripped case-insensitively from the title, with or without
  // brackets/parens.
  //
  // History (incident 2026-05-24): the previous code inverted the roles
  // for the `_` separator ("Title_Artist" convention, justified as
  // "what Suno emits"). A user uploading `Viejas Locas_Legalícenla.mp3`
  // (the more common `Artist_Title` order — what every Mac/Windows
  // export does when Finder strips spaces) ended up with
  // artist="Legalícenla", title="Viejas Locas". lrclib lookup failed,
  // the pipeline fell to whisperX-only, and the editor shipped
  // "Le realicen la..." instead of the actual chorus "Legalícenla".
  // PR #280 added a backend swap-retry that auto-corrects the metadata,
  // but the frontend was still INDUCING the inversion silently. This
  // unifies the convention so the swap-retry is the second line of
  // defense, not the first.
  //
  // If the user really has a Suno-style "Title_Artist.mp3" file, they
  // can hand-edit the inputs — the backend will detect the swap and
  // auto-correct via PR #280. So the trade-off is asymmetric: this
  // fix is correct for the common case, harmless (auto-corrected) for
  // the rare Suno case.
  const _NOISE_PATTERNS = [
    /\s*[\(\[]\s*official\s+video\s*[\)\]]/gi,
    /\s*[\(\[]\s*official\s+audio\s*[\)\]]/gi,
    /\s*[\(\[]\s*official\s+music\s+video\s*[\)\]]/gi,
    /\s*[\(\[]\s*lyric\s+video\s*[\)\]]/gi,
    /\s*[\(\[]\s*audio\s*[\)\]]/gi,
    /\s*[\(\[]\s*video\s*[\)\]]/gi,
    /\s*[\(\[]\s*en\s+vivo\s*[\)\]]/gi,
    /\s*[\(\[]\s*live\s*[\)\]]/gi,
    /\s*[\(\[]\s*lyrics\s*[\)\]]/gi,
    /\s*[\(\[]\s*remaster(?:ed)?(?:\s+\d{4})?\s*[\)\]]/gi,
    /\s*-\s*official\s+video\s*$/gi,
    /\s*-\s*live\s*$/gi,
  ];
  const _stripNoise = (s) => {
    let out = s;
    for (const pat of _NOISE_PATTERNS) out = out.replace(pat, "");
    return out.trim();
  };
  const parseFilename = (filename) => {
    const name = filename.replace(/\.(mp3|wav|m4a|flac|aac|ogg)$/i, "");
    let artist = "";
    let song = name.trim();
    if (name.includes(" - ")) {
      const [head, ...rest] = name.split(" - ");
      artist = head.trim();
      song = rest.join(" - ").trim();
    } else if (name.includes("_")) {
      // YouTube / Suno export convention: "Title_Artist.ext" (head=title,
      // tail=artist). The backend implements this same convention in
      // main.py:1537-1539 with the docstring at main.py:1500-1508
      // documenting it as intentional.
      //
      // HOTFIX 2026-05-27: previously this branch used `head=artist`,
      // contradicting the backend. Every file with `_` separator created
      // 2 jobs with swapped metadata — one from /upload-url with the
      // frontend's (wrong) parse, one from /transcribe-uploaded with
      // the backend's (right) parse — and the dedupe didn't pesca them
      // because filenames were identical but artist/title differed.
      // Incident: agus.cafisi 16:42 UTC, file
      // `Un Pacto Live In Buenos Aires  2001_Bersuit Vergarabat.wav`.
      const [head, ...rest] = name.split("_");
      song = head.trim();
      artist = rest.join("_").trim();
    }
    song = _stripNoise(song);
    artist = _stripNoise(artist);
    return { artist, song };
  };

  const [batchTruncated, setBatchTruncated] = useState(0);
  const [oversize, setOversize] = useState([]);
  // Files whose extension isn't .mp3/.wav. Tracked so a wrong-type drop
  // shows a notice instead of silently doing nothing — drag&drop bypasses
  // the <input accept> filter, so this path is reachable in prod.
  const [rejectedType, setRejectedType] = useState([]);
  // 0-byte files (failed export, cloud placeholder not yet synced). They
  // "upload" fine and then die opaquely in transcription — reject at the
  // door with a reason instead.
  const [rejectedEmpty, setRejectedEmpty] = useState([]);
  // Duplicates skipped (same name+size already in the batch or repeated
  // within the drop). Amber notice: benign, but silent-skip would look
  // like "the drop did nothing" for the duplicated file.
  const [duplicates, setDuplicates] = useState([]);

  const addFiles = (fileList) => {
    // Reset EVERY notice up front. The early-returns below used to skip
    // the resets, so a second drop could show this drop's notice next to
    // a stale one from the previous drop (old filenames included).
    setRejectedType([]);
    setRejectedEmpty([]);
    setDuplicates([]);
    setOversize([]);
    setBatchTruncated(0);

    const all = Array.from(fileList);
    const mp3s = all.filter((f) => {
      const lower = f.name.toLowerCase();
      return ACCEPTED_EXTS.some((ext) => lower.endsWith(ext));
    });
    const wrongType = all.filter((f) => !mp3s.includes(f));
    if (wrongType.length) {
      setRejectedType(wrongType.map((f) => f.name));
      // warn-level so captureConsoleIntegration ships it to Sentry,
      // grouped under the [upload-reject] tag.
      console.warn(
        "[upload-reject] wrong type:",
        wrongType.map((f) => `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`).join(", "),
      );
    }
    if (!mp3s.length) return;

    const empty = mp3s.filter((f) => f.size === 0);
    const nonEmpty = mp3s.filter((f) => f.size > 0);
    if (empty.length) {
      setRejectedEmpty(empty.map((f) => f.name));
      console.warn(
        "[upload-reject] empty file:",
        empty.map((f) => f.name).join(", "),
      );
    }
    if (!nonEmpty.length) return;

    // Dedup: against the current batch (same name+size) and within the
    // drop itself. Duplicate rows shared the prefetch job downstream and
    // produced two /generate calls over the same job_id.
    const seen = new Set(
      (Array.isArray(files) ? files : []).map((e) => `${e.file.name}|${e.file.size}`),
    );
    const unique = [];
    const dupes = [];
    for (const f of nonEmpty) {
      const sig = `${f.name}|${f.size}`;
      if (seen.has(sig)) dupes.push(f);
      else { seen.add(sig); unique.push(f); }
    }
    if (dupes.length) {
      setDuplicates(dupes.map((f) => f.name));
      console.warn(
        "[upload-reject] duplicate skipped:",
        dupes.map((f) => f.name).join(", "),
      );
    }
    if (!unique.length) return;
    const okAll = unique;

    const max = MAX_FILE_MB * 1024 * 1024;
    const tooBig = okAll.filter((f) => f.size > max);
    const okSize = okAll.filter((f) => f.size <= max);
    if (tooBig.length) {
      setOversize(tooBig.map((f) => f.name));
      console.warn(
        "[upload-reject] oversize (cap " + MAX_FILE_MB + " MB):",
        tooBig.map((f) => `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`).join(", "),
      );
    }
    if (!okSize.length) return;

    // Audit 2026-05-26 (#388 wizard-duplicate-jobs): compute remaining,
    // accepted, and newEntries OUTSIDE the setState callback. Side
    // effects that fire inside a reducer (setBatchTruncated, y el
    // auto-transcribe al drop que hacía el código viejo) get
    // double-invoked under React StrictMode (dev) and CAN double-invoke
    // in production if React decides to abort+retry a render. El disparo
    // de transcripción se movió al avance de paso (onUploadAdvance), pero
    // mantener estos cálculos fuera del reducer sigue siendo correcto.
    //
    // We can do all the math here because the parent passes `files`
    // as a prop (line 117), so we know the current count without
    // needing to read prev from the reducer. The reducer below becomes
    // a pure `[...prev, ...newEntries]` — no side effects, safe to
    // double-invoke.
    const currentCount = Array.isArray(files) ? files.length : 0;
    const remaining = MAX_BATCH_SIZE - currentCount;
    if (remaining <= 0) {
      setBatchTruncated(okSize.length);
      return;
    }
    const accepted = okSize.slice(0, remaining);
    const dropped = okSize.length - accepted.length;
    setBatchTruncated(dropped > 0 ? dropped : 0);
    const newEntries = accepted.map((f) => {
      const { artist, song } = parseFilename(f.name);
      return {
        file: f,
        artist,
        songTitle: song,
        // Default 'es' instead of '' (auto-detect). Auto was producing
        // ~50% language-misdetection on Spanish catalogue (audited
        // 2026-05-15 across 4 sample tracks: 2 misdetected as javanese
        // and italian respectively, ending in the synthesizer path with
        // bad timestamps). Operator can still flip to 'auto' if they
        // upload a non-Spanish song.
        language: "es",
        ...batchDefaultsRef.current,
      };
    });
    if (!newEntries.length) return;

    // Pure reducer — safe under StrictMode double-invoke.
    onFiles((prev) => [...prev, ...newEntries]);
    // NOTA: la transcripción NO arranca acá. Antes (2026-05-23) el trigger
    // vivía en el drop, pero eso disparaba el prefetch cuando la fuente de
    // letra todavía era el default "IA" y la letra oficial no estaba pegada
    // → job sin anclar (bug staging e77f84aefe33). Ahora el prefetch se
    // dispara al avanzar del paso "Subí" (goStep → onUploadAdvance), con la
    // fuente de letra ya resuelta por canción.
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const updateField = (idx, field, value) => {
    // Edge case (a): si el operador cambia la fuente de letra o edita la
    // letra oficial de una canción que YA disparó su prefetch (volvió al
    // paso "Subí" tras avanzar), invalidamos ese prefetch para que la
    // transcripción se rehaga con el estado nuevo (el job cacheado salió
    // con el anchor anterior). Solo aplica a esos dos campos.
    if ((field === "lyricsSource" || field === "anchorLyrics")
        && typeof onInvalidatePrefetch === "function") {
      const entry = files[idx];
      if (entry?.file) onInvalidatePrefetch(entry.file);
    }
    onFiles((prev) =>
      prev.map((entry, i) => (i === idx ? { ...entry, [field]: value } : entry))
    );
  };

  // U11 UX (2026-05-25): el parser de filename a veces invierte artist↔title.
  // El backend U11 lo resuelve con DB lookup de known-artists, pero el primer
  // upload del tenant (cache vacío) cae a la heurística histórica. Botón
  // de "intercambiar" deja al operador corregir en 1 click sin volver a
  // tipear ambos campos.
  const swapArtistTitle = (idx) => {
    onFiles((prev) =>
      prev.map((entry, i) =>
        i === idx
          ? { ...entry, artist: entry.songTitle || "", songTitle: entry.artist || "" }
          : entry
      )
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
              <HelpTip articleId="which-format" />
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
              <HelpTip articleId="which-format" />
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
      {artTrack && (
        <div className="mt-3 space-y-1" onClick={(e) => e.stopPropagation()}>
          <label className="text-[10px] uppercase tracking-[0.18em] text-gray-500 block">
            {t("upload.label_line_label") || "Línea legal / sello (opcional)"}
          </label>
          <input
            type="text"
            maxLength={120}
            value={labelLine}
            onChange={(e) => setLabelLine(e.target.value)}
            placeholder={t("upload.label_line_placeholder") || "℗ 2026 Nombre del sello"}
            className="w-full sm:w-96 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand/60"
          />
          <p className="text-[11px] text-gray-500">
            {t("upload.label_line_hint") || "Se muestra chica abajo a la izquierda del video, en todos los formatos."}
          </p>
        </div>
      )}
    </div>
  );

  // Rejection notices (wrong type / oversize / batch full). Rendered in
  // BOTH dropzone branches so they show even when NOTHING was accepted —
  // i.e. the user's first/only file was too big or the wrong format.
  // Before 2026-07-02 these only rendered inside the files.length > 0
  // branch, so an all-rejected first drop gave zero feedback ("subo el
  // audio y no hace nada" — 107 MB WAV, Universal).
  const _notices = (
    <div onClick={(e) => e.stopPropagation()}>
      {rejectedType.length > 0 && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
          <p className="text-[11px] text-red-300">
            {t("upload.wrong_type", {
              count: rejectedType.length,
              names: rejectedType.slice(0, 3).join(", ") + (rejectedType.length > 3 ? "…" : ""),
            })}
          </p>
          <button
            onClick={(e) => { e.stopPropagation(); setRejectedType([]); }}
            className="mt-1 text-[11px] text-red-400/60 hover:text-red-300"
          >{t("common.dismiss") || "dismiss"}</button>
        </div>
      )}
      {rejectedEmpty.length > 0 && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
          <p className="text-[11px] text-red-300">
            {t("upload.empty_file", {
              count: rejectedEmpty.length,
              names: rejectedEmpty.slice(0, 3).join(", ") + (rejectedEmpty.length > 3 ? "…" : ""),
            })}
          </p>
          <button
            onClick={(e) => { e.stopPropagation(); setRejectedEmpty([]); }}
            className="mt-1 text-[11px] text-red-400/60 hover:text-red-300"
          >{t("common.dismiss") || "dismiss"}</button>
        </div>
      )}
      {duplicates.length > 0 && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20">
          <p className="text-[11px] text-amber-300">
            {t("upload.duplicate_skipped", {
              count: duplicates.length,
              names: duplicates.slice(0, 3).join(", ") + (duplicates.length > 3 ? "…" : ""),
            })}
          </p>
          <button
            onClick={(e) => { e.stopPropagation(); setDuplicates([]); }}
            className="mt-1 text-[11px] text-amber-400/60 hover:text-amber-300"
          >{t("common.dismiss") || "dismiss"}</button>
        </div>
      )}
      {oversize.length > 0 && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
          <p className="text-[11px] text-red-300">
            {t("upload.oversize", {
              dropped: oversize.length,
              max: MAX_FILE_MB,
              names: oversize.slice(0, 3).join(", ") + (oversize.length > 3 ? "…" : ""),
            })}
          </p>
          <button
            onClick={(e) => { e.stopPropagation(); setOversize([]); }}
            className="mt-1 text-[11px] text-red-400/60 hover:text-red-300"
          >{t("common.dismiss") || "dismiss"}</button>
        </div>
      )}
      {batchTruncated > 0 && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20">
          <p className="text-[11px] text-amber-300">
            {t("upload.batch_truncated", { dropped: batchTruncated, max: MAX_BATCH_SIZE })}
          </p>
          <button
            onClick={(e) => { e.stopPropagation(); setBatchTruncated(0); }}
            className="mt-1 text-[11px] text-amber-400/60 hover:text-amber-300"
          >{t("common.dismiss") || "dismiss"}</button>
        </div>
      )}
    </div>
  );

  // Video type selector (Lyric Video vs Art Track). Shown both in the
  // empty-state (pre-upload) and in step 1, so the operator picks the type
  // before or right after adding audio. Hidden entirely when Art Track isn't
  // enabled for this account (feature gate) — then there's only one type.
  const _videoTypeSelector = !artTrackEligible ? null : (
    <div className="mb-5">
      <div className="text-label text-gray-400 mb-2">
        {t("upload.video_type") || "Tipo de video"}
      </div>
      <div className="flex gap-1 p-1 glass rounded-xl w-fit">
        {[
          { id: false, label: t("upload.video_type_lyrics") || "Lyric Video" },
          { id: true, label: t("upload.video_type_art") || "Art Track" },
        ].map((m) => (
          <button
            key={String(m.id)}
            type="button"
            onClick={() => onArtTrackChange?.(m.id)}
            className={`px-4 py-1.5 rounded-lg text-label transition-all ${
              artTrack === m.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>
      {artTrack && (
        <p className="mt-2 text-sm text-gray-400 max-w-xl">
          {t("upload.video_type_art_hint") ||
            "Master audio + cover, sin letra. Subí la portada en el paso “Modo”; el video muestra el cover centrado sobre un fondo difuminado con movimiento sutil."}
        </p>
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
        className={`group relative rounded-3xl text-center cursor-pointer transition-all duration-300
          ${files.length > 0 ? "p-8" : "p-12 md:p-16"}
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
            {_notices}
          </div>
        ) : (
          <div className="py-6 md:py-8">
            <div className="w-20 h-20 md:w-24 md:h-24 mx-auto mb-6 rounded-3xl bg-brand/10 ring-1 ring-brand/20 flex items-center justify-center group-hover:bg-brand/20 group-hover:ring-brand/40 transition-all duration-300">
              <svg className="w-10 h-10 md:w-12 md:h-12 text-brand-light group-hover:text-white transition-colors duration-300" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            </div>
            <p className="text-xl md:text-2xl font-bold text-white mb-2 tracking-tight">{t("upload.drag")}</p>
            <p className="text-ink-secondary text-sm mb-3">{t("upload.drag_sub")}</p>
            <p className="text-gray-700 text-[11px]">
              {t("upload.size_hint")}
            </p>
            {_notices}
          </div>
        )}
      </div>
  );

  // QA fix 2026-05-28: en edit mode files=[] (no se sube nada nuevo, el
  // job ya tiene su audio), pero el operador SÍ necesita ver los
  // controls de movement/effect en step 3 para corregir esos campos.
  // El gate original `files.length > 0` ocultaba todo el panel en edit
  // mode → step 3 quedaba vacío. Ahora abrimos también para editMode.
  // Los sub-bloques internos siguen con sus propios checks
  // (`files.length > 1` para acciones de batch) — esos correctamente
  // se ocultan si no hay archivos.
  // Preset interno SOLO-ADMIN: la "receta UMG Argentina" derivada del análisis
  // de los videos aprobados de Universal (may–jul 2026). Un click rellena el
  // wizard con los settings de menor retrabajo (fuente grande, estático,
  // mayúsculas, sin escenas, ProRes/HD). El operador puede ajustar cualquiera
  // antes de generar. NO cambia defaults del servidor ni del tenant, y no se
  // expone en el portal de descargas de UMG — solo rellena el formulario, igual
  // que si se tipeara a mano. Gateado por role=admin en el JSX.
  const applyUmgArgentinaRecipe = () => {
    track("wizard.preset", { preset: "umg_argentina" });
    // Fondo: Auto (AI) + inspirado en la letra; sin multi-escena.
    onStyleChange?.("auto");
    onBgMode?.("auto");
    selectSceneMode("lyrics");        // match_lyrics=true, sin hint
    onEnableScenesChange?.(false);
    // Render (batchDefaults; el fan-out interno los aplica también por canción).
    updateBatchDefault("movementStyle", "estatico");
    updateBatchDefault("font", "poppins-bold");
    updateBatchDefault("fontScale", "1.3");
    updateBatchDefault("textCase", "upper");
    updateBatchDefault("lyricsAnimation", "none");
    updateBatchDefault("lineTransition", "none");
    updateBatchDefault("effect", "");
    updateBatchDefault("titleTemplate", "auto");
    updateBatchDefault("frameFormat", "full");
    // Entrega: master ProRes + YouTube, HD / 24fps / ProRes 422 HQ.
    setDeliveryProfile("both");
    setUmgFrameSize("HD");
    setUmgFps(24);
    setUmgProresProfile(3);
  };

  const _batchSettingsBlock = (files.length > 0 || editMode) ? (
    <div className="mt-3 glass rounded-card px-4 py-4">
      <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500 mb-3">
        {files.length > 1
          ? (t("upload.batch_settings_title") || "Configuración del lote")
          : (t("upload.single_settings_title") || "Ajustes del video")}
      </p>

      {user?.role === "admin" && !editMode && (
        <div className="mb-3 flex items-center justify-between gap-3 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-3 py-2.5">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="rounded-full bg-accent/[0.08] px-1.5 py-0.5 text-[8px] font-bold uppercase text-accent ring-1 ring-accent/20">{t("upload.recipe_umg_badge") || "Admin"}</span>
              <span className="text-[11px] font-medium text-gray-200">{t("upload.recipe_umg_title") || "Receta UMG Argentina"}</span>
            </div>
            <p className="text-[10px] text-gray-600 mt-0.5">
              {t("upload.recipe_umg_desc") || "Rellena los settings de menor retrabajo. Podés ajustar cualquiera antes de generar."}
            </p>
          </div>
          <button
            type="button"
            onClick={applyUmgArgentinaRecipe}
            className="shrink-0 rounded-full bg-brand text-white text-[11px] font-medium px-3 py-1.5 hover:opacity-90 transition-opacity"
          >
            {t("upload.recipe_umg_apply") || "Aplicar"}
          </button>
        </div>
      )}

      {bgMode !== "auto" && (
        <div className="mb-3 rounded-lg bg-surface-1/60 ring-1 ring-white/[0.05] px-3 py-2 text-[11px] text-gray-400">
          {bgMode === "library"
            ? (t("upload.settings_library_note") || "Usás un fondo de Biblioteca — el movimiento y la escena los define ese clip. El género solo ajusta detalles.")
            : (t("upload.settings_custom_note") || "Usás un fondo subido por vos — el movimiento y la escena los define tu archivo.")}
        </div>
      )}

      {/* Movement gallery — click a card to apply to all tracks */}
      <div className="mb-4">
        <div className="mb-2">
          <div className="flex items-baseline justify-between">
            <p className="text-[11px] text-gray-400 font-medium">
              {t("upload.movement_gallery_title") || "Movimiento del fondo"}
            </p>
            {files.length > 1 && (
              <p className="text-[10px] text-gray-600">
                {t("upload.movement_gallery_hint") || "Click para aplicar a todos · personalizable por canción"}
              </p>
            )}
          </div>
          <p className="text-[10px] text-gray-600 mt-0.5">
            {t("upload.movement_gallery_desc") || "Cómo se mueve la cámara del fondo. Pasá el mouse o elegí y miralo en el preview ←"}
          </p>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          {MOVEMENT_STYLES.map((m) => {
            const active = batchDefaults.movementStyle === m.code;
            const inVideo = isAnchor("movementStyle", m.code);
            return (
              <button
                key={m.code || "auto"}
                type="button"
                onClick={() => updateBatchDefault("movementStyle", m.code)}
                onMouseEnter={() => setHoverMovement(m.code)}
                onMouseLeave={() => setHoverMovement(null)}
                aria-pressed={active}
                data-movement={m.code || "auto"}
                data-in-video={inVideo ? "true" : undefined}
                aria-label={`${m.label}: ${m.desc}${inVideo ? ` — ${ANCHOR_LABEL}` : ""}`}
                title={m.desc}
                className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                  active
                    ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                    : "border-white/[0.06] hover:border-white/[0.20]"
                }`}
              >
                {/* Real Veo example clip per style (Auto has no clip → icon) */}
                <div className="aspect-video bg-black relative overflow-hidden">
                  {m.sample ? (
                    m.kind === "image" ? (
                      // Foto fija = STATIC photo → render an <img>, NOT a looping
                      // <video> (showing motion on the "fixed photo" card was
                      // self-contradictory; operator-reported).
                      <img src={m.sample} alt="" className="w-full h-full object-cover pointer-events-none" />
                    ) : (
                      <video src={m.sample} className="w-full h-full object-cover pointer-events-none" autoPlay loop muted playsInline />
                    )
                  ) : (
                    <div className="w-full h-full grid place-items-center text-gray-400" style={{ background: "radial-gradient(120% 100% at 50% 0,#2a1d52,#0b0820)" }}>
                      <span className="w-7 h-7">{movIcon(m.code)}</span>
                    </div>
                  )}
                  {active && (
                    <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-brand grid place-items-center shadow">
                      <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                    </div>
                  )}
                  {inVideo && <AnchorChip />}
                </div>
                <div className="px-2.5 py-2 bg-surface-1">
                  <p className={`text-[12px] font-medium leading-tight ${active ? "text-white" : "text-gray-200"}`}>
                    {m.label.replace(/\s*\(.*\)\s*/, "")}
                  </p>
                  <p className="text-[10px] text-gray-500 leading-snug mt-0.5">{m.desc}</p>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Effect gallery — particles composited OVER the background. Available
          for ANY source (IA / Biblioteca / Subido): it's an overlay, not a
          generation choice. Orthogonal to "Movimiento" (camera). */}
      <div className="mb-4 pt-3 border-t border-white/[0.05]">
        <div className="mb-2">
          <div className="flex items-baseline justify-between">
            <p className="text-[11px] text-gray-400 font-medium">
              {t("upload.effect_gallery_title") || "Efecto encima"}
            </p>
            {files.length > 1 && (
              <p className="text-[10px] text-gray-600">
                {t("upload.effect_gallery_hint") || "Click para aplicar a todos · personalizable por canción"}
              </p>
            )}
          </div>
          <p className="text-[10px] text-gray-600 mt-0.5">
            {t("upload.effect_gallery_desc") || "Partículas que caen encima del fondo (nieve, lluvia, estrellas…). Es el toque del formato de UMG. Se suma a cualquier fondo, incluso de Biblioteca."}
          </p>
        </div>
        <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
          {EFFECTS.map((e) => {
            const active = (batchDefaults.effect || "") === e.code;
            const inVideo = isAnchor("effect", e.code);
            return (
              <button
                key={e.code || "none"}
                type="button"
                onClick={() => updateBatchDefault("effect", e.code)}
                onMouseEnter={() => setHoverEffect(e.code)}
                onMouseLeave={() => setHoverEffect(null)}
                aria-pressed={active}
                data-effect={e.code || "none"}
                data-in-video={inVideo ? "true" : undefined}
                aria-label={`${e.label}: ${e.desc}${inVideo ? ` — ${ANCHOR_LABEL}` : ""}`}
                title={e.desc}
                className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                  active
                    ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                    : "border-white/[0.06] hover:border-white/[0.20]"
                }`}
              >
                <div className="aspect-video bg-black relative overflow-hidden">
                  {e.sample ? (
                    <video src={e.sample} className="w-full h-full object-cover pointer-events-none" autoPlay loop muted playsInline />
                  ) : (
                    <div className="w-full h-full grid place-items-center text-gray-500 text-[10px]" style={{ background: "radial-gradient(120% 100% at 50% 0,#241a40,#0b0820)" }}>
                      {t("upload.effect_none") || "Ninguno"}
                    </div>
                  )}
                  {active && (
                    <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-brand grid place-items-center shadow">
                      <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                    </div>
                  )}
                  {inVideo && <AnchorChip />}
                </div>
                <div className="px-2 py-1.5 bg-surface-1">
                  <p className={`text-label leading-tight ${active ? "text-white" : "text-gray-200"}`}>{e.label}</p>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Scene metadata (genre + concept) — only when generating with AI.
          Explained inline so it's clear what they DO and how they impact. */}
      {bgMode === "auto" && (
        <div className="mb-4 pt-3 border-t border-white/[0.05]">
          <p className="text-[11px] text-gray-400 font-medium">{t("upload.scene_meta_title") || "Escena"}</p>
          {/* Edición del fondo = el editor de siempre (Movimiento/Efecto/hint
              define el look Y el motor: "Foto fija"→Imagen, resto→Veo). Encima,
              un badge honesto del modelo real (cupo de 3 ediciones, no cobro por
              regen) + un botón para "otra tirada" sin cambiar nada (si no, el
              diff queda vacío y sale "No cambiaste nada"). Rediseño 2026-07-24:
              se sacó el selector "Motor del fondo" (redundante con Movimiento) y
              el toggle de validación se plegó en "Opciones avanzadas". */}
          {editMode && (
            <div className="mt-2 mb-3 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-3 py-2.5">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-[11px] font-medium text-gray-200">
                    {variantMode
                      ? (t("variant.cost_title") || "Cuesta 1 video de tu plan")
                      : (t("upload.regen_bg_title") || "Regeneración de fondo incluida")}
                  </p>
                  <p className="text-[10px] text-gray-600 mt-0.5">
                    {variantMode
                      ? (t("variant.cost_desc_wizard") || "La variante es un video nuevo: se cobra 1 video al plan y pasa por review como cualquier upload. A partir de la 4ª versión de la misma canción se factura un extra (te lo confirmamos antes de crearla).")
                      : (t("upload.regen_bg_desc") || "Editás el fondo como siempre (movimiento, efecto, look). Al aprobar se genera una versión nueva con IA y usa 1 de tus 3 ediciones. Cambiar a un fondo de Biblioteca es gratis.")}
                  </p>
                </div>
                {/* El toggle "Generar otra versión" existe porque una
                    edición puede NO tocar el fondo (y entonces el diff
                    queda vacío). Una variante siempre genera fondo nuevo
                    — ofrecer el toggle sería mentir sobre una elección
                    que no existe. */}
                {!variantMode && (
                <button
                  type="button"
                  onClick={toggleRegenRequested}
                  aria-pressed={regenRequested}
                  className={`shrink-0 rounded-full text-[11px] font-medium px-3 py-1.5 transition-colors ${
                    regenRequested
                      ? "bg-brand text-white"
                      : "bg-surface-3/60 text-gray-300 ring-1 ring-white/[0.06] hover:text-white"
                  }`}
                >
                  {regenRequested ? (t("upload.regen_bg_on") || "Se generará ✓") : (t("upload.regen_bg_cta") || "Generar otra versión")}
                </button>
                )}
              </div>
              {/* Fondo-libre (bypass del validador) — sólo cuentas no-UMG, y
                  plegado para no ensuciar el editor. UMG tiene política fija
                  (el backend la fuerza igual) → ni mostramos el disclosure. */}
              {!isUniversalAccount(user?.tenant_id, user?.billing_group) && (
                <details className="mt-2">
                  <summary className="text-[10px] text-gray-500 hover:text-gray-300 cursor-pointer select-none">
                    {t("upload.bg_advanced") || "Opciones avanzadas"}
                  </summary>
                  <div className="mt-2">
                    <ContentValidationToggle
                      value={regenValidation}
                      onChange={setRegenValidationChoice}
                      tenantId={user?.tenant_id}
                      billingGroup={user?.billing_group}
                    />
                  </div>
                </details>
              )}
            </div>
          )}
          <p className="text-[10px] text-gray-600 mt-0.5 mb-2">
            {sceneMode === "prompt"
              ? (t("upload.scene_meta_prompt_note") || "Tu prompt define la escena — género y concepto quedan como ayuda secundaria.")
              : (t("upload.scene_meta_desc") || "Género ajusta la paleta y la atmósfera · Concepto define el tipo de escena (ciudad, naturaleza, abstracto…).")}
          </p>
          {/* Género/Concepto: editables también en edición (cableados al /edit
              vía computeFieldDiff → bucket background). Cambiarlos regenera el
              fondo con la nueva vocabulario de escena; el pipeline los lee de
              render_params (persistidos por request_edit). */}
          <div className={`flex flex-wrap gap-3 ${sceneMode === "prompt" ? "opacity-50" : ""}`}>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-gray-600 shrink-0" title={t("upload.genre_help") || "El estilo musical. Ajusta paleta, iluminación y atmósfera del fondo."}>{t("upload.genre_label") || "Género:"}</span>
              <Listbox
                value={batchDefaults.genre}
                onChange={(v) => updateBatchDefault("genre", v)}
                options={GENRES}
                className="w-44"
                ariaLabel={t("upload.genre_label") || "Género"}
              />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-gray-600 shrink-0" title={t("upload.concept_help") || "El tipo de escena visual: ciudad, naturaleza, abstracto, cósmico, etc."}>{t("upload.concept_label") || "Concepto:"}</span>
              <Listbox
                value={batchDefaults.concept}
                onChange={(v) => updateBatchDefault("concept", v)}
                options={CONCEPTS}
                className="w-44"
                ariaLabel={t("upload.concept_label") || "Concepto"}
              />
            </div>
          </div>
        </div>
      )}

      {SHOW_UPLOAD_TYPOGRAPHY && (<>
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
      <div className="mb-3" data-tour="upload-text-case">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-gray-600 shrink-0">{t("upload.text_case_label") || "Texto:"}</span>
          <span className="rounded-full bg-accent/[0.08] px-1.5 py-0.5 text-[8px] font-bold uppercase text-accent ring-1 ring-accent/20">
            {t("whatsnew.new_badge") || "Nuevo"}
          </span>
          <HelpTip text={t("announce.typocase_tagline")} />
          <div className="flex gap-1">
            {TEXT_CASE_OPTS.map((opt) => (
              <button
                key={opt.code}
                type="button"
                title={opt.code === "sentence" ? `${opt.label} · ${t("announce.typocase_tagline")}` : opt.label}
                onClick={() => updateBatchDefault("textCase", opt.code)}
                onMouseEnter={() => setHoverCaseBatch(opt.code)}
                onMouseLeave={() => setHoverCaseBatch(null)}
                className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                  ${batchDefaults.textCase === opt.code
                    ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                    : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                  }`}
              >{opt.d}</button>
            ))}
          </div>
        </div>
        {hoverCaseBatch && (
          <div className="mt-1.5 px-3 py-1.5 rounded-md bg-black/40 ring-1 ring-white/[0.06] flex items-baseline gap-2 animate-fade-in">
            <span className="text-[11px] font-mono text-white/80 tracking-wide">
              {applyTextCase(t("upload.sample_lyric"), hoverCaseBatch)}
            </span>
            <span className="text-[10px] text-gray-600">← {t("upload.case_preview_help")}</span>
          </div>
        )}
      </div>

      {/* Frame format: Pantalla completa (16:9) / Cine (franjas 2.39:1).
          El letterbox se aplica determinísticamente en post — look de cine
          intencional, opuesto a las barras estocásticas de Veo. */}
      <div className="mb-3" data-tour="upload-frame-format">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-gray-600 shrink-0">{t("upload.frame_format_label") || "Formato:"}</span>
          <span className="rounded-full bg-accent/[0.08] px-1.5 py-0.5 text-[8px] font-bold uppercase text-accent ring-1 ring-accent/20">
            {t("whatsnew.new_badge") || "Nuevo"}
          </span>
          <HelpTip text={t("announce.cinema_tagline")} />
          <div className="flex gap-1">
            {FRAME_FORMAT_OPTS.map((opt) => (
              <button
                key={opt.code}
                type="button"
                title={opt.code === "cine" ? `${opt.label} · ${t("announce.cinema_tagline")}` : opt.label}
                onClick={() => updateBatchDefault("frameFormat", opt.code)}
                className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                  ${(batchDefaults.frameFormat || "full") === opt.code
                    ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                    : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                  }`}
              >{opt.d}</button>
            ))}
          </div>
        </div>
      </div>

      {/* Font scale — A's en tamaños crecientes.
          QA fix 2026-05-28 (UMG report): agus reportó que un operador
          UMG quiso letras más grandes pero el botón "1.3" era el
          máximo del UI. El backend acepta hasta 1.5 (clamp en
          ass_render.py:114 y pipeline.py:7513). Sumamos el 6to botón
          para destrabar ese 17% extra de headroom que el backend ya
          tenía pero el UI no exponía. */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_scale_label") || "Tamaño:"}</span>
        <div className="flex items-end gap-1">
          {[
            { code: "0.75", cls: "text-[9px]"  },
            { code: "0.9",  cls: "text-[11px]" },
            { code: "1.0",  cls: "text-[13px]" },
            { code: "1.15", cls: "text-[16px]" },
            { code: "1.3",  cls: "text-[19px]" },
            { code: "1.5",  cls: "text-[22px]" },
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

      {/* Lyric transition (Corte/Fade fade-time) + Text motion (Sutil drift):
          deprecados 2026-05-23. Los reemplazan lyrics_animation +
          line_transition (libass), elegidos en el editor post-upload. */}

      {/* Text contrast pills */}
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.contrast_label") || "Contraste:"}</span>
        <div className="flex gap-1">
          {[
            { code: "subtle", style: { WebkitTextStroke: "0px", textShadow: "none" },         label: t("upload.contrast_subtle") || "Suave" },
            { code: "medium", style: { WebkitTextStroke: "0.5px black", textShadow: "0 0 3px rgba(0,0,0,0.8)" }, label: t("upload.contrast_medium") || "Medio" },
            { code: "strong", style: { WebkitTextStroke: "1px black",   textShadow: "0 0 6px rgba(0,0,0,1), -1px -1px 0 #000, 1px 1px 0 #000" }, label: t("upload.contrast_strong") || "Fuerte" },
          ].map((opt) => (
            <button
              key={opt.code}
              type="button"
              title={opt.label}
              onClick={() => updateBatchDefault("textContrast", opt.code)}
              className={`px-2 py-1 rounded-md text-[13px] font-bold text-white transition-all
                ${batchDefaults.textContrast === opt.code
                  ? "bg-brand/20 ring-1 ring-brand/40"
                  : "bg-surface-3/40 hover:bg-surface-3/60"
                }`}
              style={{ ...opt.style, paintOrder: "stroke fill" }}
            >A</button>
          ))}
        </div>
      </div>
      </>)}
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
          (entry.effect       || "") !== (bd.effect       || "") ||
          (entry.font         || "") !== (bd.font         || "") ||
          (entry.textCase     || "upper") !== (bd.textCase     || "upper") ||
          (entry.fontScale    || "1.0")   !== (bd.fontScale    || "1.0")   ||
          (entry.lyricsAnimation || "none") !== (bd.lyricsAnimation || "none") ||
          (entry.lineTransition || "none") !== (bd.lineTransition || "none") ||
          (entry.textContrast || "medium") !== (bd.textContrast || "medium");

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
                {/* 2026-05-24: WAV warning ELIMINADO. Los músicos suben
                    WAV master por calidad — sugerirles "mejor MP3" es
                    contradictorio con el producto. El upload DEBE funcionar
                    hasta 500MB (default subido de 100 → 500 MB en main.py
                    vía env MAX_UPLOAD_MB). Si el archivo excede el límite,
                    el backend devuelve 413 con detalle accionable. */}
                {/* 2026-05-23: status badge de la transcripción en background.
                    Refleja el estado real del job en backend (queued ▷ transcribing
                    ▷ done | error). Si transcribeStatusByFile no tiene la key,
                    no muestra nada (path legacy o todavía no arrancó). */}
                {(() => {
                  const k = `${entry.file.name}__${entry.file.lastModified}__${entry.file.size}`;
                  const st = transcribeStatusByFile[k];
                  if (!st) return null;
                  const label = {
                    uploading:    { txt: `📤 ${t("status.awaiting_upload")}`, cls: "text-blue-400" },
                    queued:       { txt: `🕓 ${t("status.queued")}`, cls: "text-gray-400" },
                    transcribing: { txt: `🎙 ${t("status.transcribing")}`, cls: "text-brand-light" },
                    done:         { txt: `✓ ${t("upload.ready_to_review")}`, cls: "text-accent" },
                    error:        { txt: `✗ ${st.error || "Error"}`, cls: "text-red-400" },
                  }[st.status] || { txt: st.status, cls: "text-gray-500" };
                  return (
                    <p className={`text-[11px] mt-0.5 truncate ${label.cls}`}>
                      {label.txt}
                    </p>
                  );
                })()}
              </div>
              <button
                onClick={(e) => removeFile(i, e)}
                aria-label={t("upload.discard_audio")}
                title={t("upload.discard_audio")}
                className="shrink-0 w-7 h-7 rounded-lg hover:bg-red-500/10 flex items-center justify-center text-gray-300 hover:text-red-400 transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Core fields — U11 UX (2026-05-25):
                 - Labels SIEMPRE visibles arriba del input para que el
                   operador NO confunda artist vs título cuando el parser
                   auto-completa.
                 - Botón "↔ Intercambiar" cuando AMBOS campos están filleados
                   (la situación típica del autocomplete) para corregir
                   filenames invertidos ("Title - Artist") en 1 click. */}
            <div className="space-y-2.5">
              {/* Artist row */}
              <div>
                <label className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[11px] uppercase tracking-wide font-medium text-brand/80">
                    {t("upload.artist") || "Artista"}
                    <span className="text-amber-400 ml-0.5">*</span>
                  </span>
                  {entry.artist.trim() && (entry.songTitle || "").trim() && (
                    <button
                      type="button"
                      onClick={() => swapArtistTitle(i)}
                      title={t("upload.swap_hint") || "¿Quedó al revés? Intercambia artista y título."}
                      className="text-[10px] uppercase tracking-wide text-gray-400 hover:text-brand transition-colors flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-white/[0.04]"
                    >
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" />
                      </svg>
                      {t("upload.swap") || "Intercambiar"}
                    </button>
                  )}
                </label>
                <input
                  type="text"
                  value={entry.artist}
                  onChange={(e) => updateField(i, "artist", e.target.value)}
                  placeholder={t("upload.artist_placeholder") || "Ej: Viejas Locas"}
                  required
                  className={`w-full px-3 py-1.5 rounded-lg bg-surface-1 border
                    focus:outline-none text-sm text-white placeholder-gray-500 transition-all
                    ${entry.artist.trim() ? "border-white/[0.06] focus:border-brand/50" : "border-amber-500/40 focus:border-amber-400"}`}
                />
                {!entry.artist.trim() && (
                  <p className="text-[11px] text-amber-400/80 mt-1">
                    {t("upload.artist_required") || "Nombre del artista es requerido"}
                  </p>
                )}
              </div>
              {/* Title row */}
              <div>
                <label className="block text-[11px] uppercase tracking-wide font-medium text-gray-400 mb-1">
                  {t("upload.song_title") || "Título de la canción"}
                </label>
                <input
                  type="text"
                  value={entry.songTitle || ""}
                  onChange={(e) => updateField(i, "songTitle", e.target.value)}
                  placeholder={t("upload.song_title_placeholder") || "Ej: Legalícenla"}
                  className="w-full px-3 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06]
                    focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500 transition-all"
                />
                {!(entry.songTitle || "").trim() && (
                  <p className="text-[11px] text-gray-600 mt-1">
                    {t("upload.song_title_hint") || "Si lo dejás vacío, lo inferimos del nombre del archivo"}
                  </p>
                )}
              </div>
              {/* Todo lo relacionado a la LETRA (versión en vivo, fuente de
                  letra, idioma de transcripción) NO aplica a art tracks —
                  rinden sin letra y saltean Whisper. Solo se muestra en lyric
                  video. */}
              {!artTrack && (<>
              {/* Toggle "versión en vivo" (06/07): arma la auditoría
                  acústica del final aunque el título no diga "live" —
                  las letras publicadas suelen ser de la versión de
                  estudio y el final del vivo difiere. Si el título ya
                  tiene marcador live, el backend lo detecta solo. */}
              <label className={`live-version-toggle flex items-start gap-2.5 rounded-xl border p-2.5 cursor-pointer select-none ${entry.live ? "is-active" : ""}`}>
                <input
                  type="checkbox"
                  checked={!!entry.live}
                  onChange={(e) => updateField(i, "live", e.target.checked)}
                  className="sr-only"
                />
                <span className="live-version-toggle__check mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-md border" aria-hidden="true">
                  <svg className={`h-2.5 w-2.5 transition-opacity ${entry.live ? "opacity-100" : "opacity-0"}`} viewBox="0 0 12 12" fill="none">
                    <path d="m2.2 6.1 2.3 2.2L9.9 3" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </span>
                <span className="min-w-0">
                  <span className="block text-[12px] text-gray-300 font-medium">
                    {t("upload.live_version") || "Versión en vivo"}
                  </span>
                  <span className="block text-[11px] leading-relaxed text-gray-500">
                    {t("upload.live_version_hint") || "Marcalo si es un show en vivo: revisamos el final contra el audio."}
                  </span>
                </span>
              </label>
              {/* Source of lyrics: Genly AI is the safe default. The official
                  lyrics path is an intentional upgrade, and only becomes
                  "ready" once there is actual text to anchor. */}
              {anchorLyricsEligible && (() => {
                const isOfficial = entry.lyricsSource === "official";
                const lineCount = (entry.anchorLyrics || "")
                  .split("\n").filter((l) => l.trim()).length;
                const lineBadge = lineCount === 1
                  ? (t("upload.anchor_lyrics_line") || "1 línea")
                  : (t("upload.anchor_lyrics_lines", { n: lineCount }) || "{n} líneas").replace("{n}", lineCount);
                const isReady = isOfficial && lineCount > 0;
                const readyMessage = t("upload.anchor_lyrics_ready", { n: lineCount }) || `${lineBadge} listos para sincronizar`;
                return (
                  <section data-testid={`lyrics-source-${i}`} className="lyrics-source relative overflow-hidden rounded-2xl border">
                    <div className="lyrics-source__halo absolute -right-12 -top-14 h-36 w-36 rounded-full pointer-events-none" />
                    <div className="relative px-3.5 pt-3.5 pb-3">
                      <div className="flex items-start gap-2.5 mb-3">
                        <span className="lyrics-source__mark w-8 h-8 rounded-xl flex items-center justify-center shrink-0">
                          <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24" aria-hidden="true">
                            <path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Z" strokeLinecap="round" strokeLinejoin="round" />
                            <path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </span>
                        <span className="min-w-0">
                          <span className="block text-[10px] uppercase tracking-[0.16em] font-semibold text-brand-light/80">
                            {t("upload.lyrics_source_label") || "Letra"}
                          </span>
                          <span className="block text-[13px] leading-tight font-semibold text-white mt-0.5">
                            {t("upload.lyrics_source_heading") || "Elegí cómo trabajar la letra"}
                          </span>
                          <span className="block text-[11px] leading-snug text-gray-500 mt-1">
                            {t("upload.lyrics_source_heading_sub") || "Podés dejar que Genly la detecte o usar tu versión oficial."}
                          </span>
                        </span>
                      </div>
                      <div className="grid grid-cols-2 gap-2" role="radiogroup"
                        aria-label={t("upload.lyrics_source_label") || "Letra"}>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={!isOfficial}
                        data-testid={`lyrics-source-ai-${i}`}
                        onClick={() => updateField(i, "lyricsSource", "auto")}
                        className={`lyrics-source__choice group relative min-h-[108px] overflow-hidden rounded-xl border p-3 text-left transition-all duration-200 ${!isOfficial ? "is-active" : ""}
                          ${!isOfficial
                            ? "border-brand/70"
                            : "border-white/[0.07]"}`}
                      >
                        {!isOfficial && <span className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-violet-200/80 to-transparent" />}
                        <span className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                          !isOfficial ? "bg-white/15 text-white" : "bg-white/[0.06] text-gray-400 group-hover:text-gray-200"
                        }`} aria-hidden="true">
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                            <path d="m12 3 1.3 4.2L17.5 8.5l-4.2 1.3L12 14l-1.3-4.2-4.2-1.3 4.2-1.3L12 3Z" strokeLinecap="round" strokeLinejoin="round" />
                            <path d="M5 15v4M3 17h4M19 16v3M17.5 17.5h3" strokeLinecap="round" />
                          </svg>
                        </span>
                        <span className="block mt-2">
                          <span className={`block text-[12px] font-semibold ${!isOfficial ? "text-white" : "text-gray-200"}`}>
                            {t("upload.lyrics_source_ai") || "Transcripción con IA de Genly"}
                          </span>
                          <span className={`block mt-1 text-[11px] leading-snug ${!isOfficial ? "text-violet-100/65" : "text-gray-500"}`}>
                            {t("upload.lyrics_source_ai_sub") || "Detectamos y sincronizamos la letra automáticamente."}
                          </span>
                        </span>
                        <span className={`absolute right-2.5 top-2.5 flex h-4 w-4 items-center justify-center rounded-full border ${
                          !isOfficial ? "border-white/60 bg-white text-brand" : "border-white/[0.16]"
                        }`} aria-hidden="true">
                          {!isOfficial && <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg>}
                        </span>
                      </button>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={isOfficial}
                        data-testid={`lyrics-source-official-${i}`}
                        onClick={() => updateField(i, "lyricsSource", "official")}
                        className={`lyrics-source__choice group relative min-h-[108px] overflow-hidden rounded-xl border p-3 text-left transition-all duration-200 ${isOfficial ? "is-active" : ""}
                          ${isOfficial
                            ? "border-brand/70"
                            : "border-white/[0.07]"}`}
                      >
                        {isOfficial && <span className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-violet-200/80 to-transparent" />}
                        <span className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                          isOfficial ? "bg-white/15 text-white" : "bg-white/[0.06] text-gray-400 group-hover:text-gray-200"
                        }`} aria-hidden="true">
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                            <path d="M7 3.5h7L19 8v12.5H7z" strokeLinecap="round" strokeLinejoin="round" />
                            <path d="M14 3.5V8h5M10 12h6M10 15h6" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </span>
                        <span className="block mt-2">
                          <span className={`block text-[12px] font-semibold ${isOfficial ? "text-white" : "text-gray-200"}`}>
                            {t("upload.lyrics_source_official") || "Tengo la letra oficial"}
                          </span>
                          <span className={`block mt-1 text-[11px] leading-snug ${isOfficial ? "text-violet-100/65" : "text-gray-500"}`}>
                            {t("upload.lyrics_source_official_sub") || "Úsala cuando necesitás que el texto coincida exactamente."}
                          </span>
                        </span>
                        {lineCount > 0 && (
                          <span className="absolute bottom-2.5 left-3 text-[10px] font-semibold text-violet-200 tabular-nums">
                            {lineBadge}
                          </span>
                        )}
                        <span className={`absolute right-2.5 top-2.5 flex h-4 w-4 items-center justify-center rounded-full border ${
                          isOfficial ? "border-white/60 bg-white text-brand" : "border-white/[0.16]"
                        }`} aria-hidden="true">
                          {isOfficial && <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg>}
                        </span>
                      </button>
                      </div>
                    {isOfficial && (
                      <div className="mt-3 border-t border-white/[0.07] pt-3 animate-fade-in">
                        <div className="flex items-center justify-between gap-3 mb-1.5">
                          <label className="block text-[11px] font-medium text-gray-200">
                          {t("upload.anchor_lyrics_input_label") || "Pegá la letra oficial"}
                          </label>
                          <span className="text-[10px] text-gray-600">
                            {t("upload.anchor_lyrics_format_hint") || "TXT · sin tiempos"}
                          </span>
                        </div>
                        <textarea
                          value={entry.anchorLyrics || ""}
                          onChange={(e) => updateField(i, "anchorLyrics", e.target.value)}
                          onPaste={(e) => {
                            // Algunos orígenes (Word, PDF, ciertos sitios de letras)
                            // separan versos con CRLF, CR solo o los separadores
                            // Unicode U+2028/U+2029. El <textarea> NO dibuja esos como
                            // salto → la letra quedaba pegada en un bloque aunque el
                            // contador (split "\n") marcaba bien las líneas. Normalizamos
                            // a "\n" en el pegado. Si no hay separadores raros, dejamos
                            // el paste nativo (preserva cursor/undo).
                            const raw = e.clipboardData?.getData("text");
                            if (raw == null) return;
                            const normalized = raw.replace(/\r\n?/g, "\n").replace(/[\u2028\u2029]/g, "\n");
                            if (normalized === raw) return;
                            e.preventDefault();
                            const el = e.target;
                            const start = el.selectionStart ?? el.value.length;
                            const end = el.selectionEnd ?? el.value.length;
                            const next = (el.value.slice(0, start) + normalized + el.value.slice(end)).slice(0, 20000);
                            updateField(i, "anchorLyrics", next);
                          }}
                          placeholder={t("upload.anchor_lyrics_placeholder") || "Pegá la letra acá — una línea por verso, sin timestamps."}
                          rows={6}
                          maxLength={20000}
                          className="w-full px-3 py-2.5 rounded-xl bg-black/20 border border-white/[0.08]
                            focus:border-brand/70 focus:ring-2 focus:ring-brand/10 focus:outline-none text-sm text-white placeholder-gray-600
                            transition-all resize-y leading-relaxed"
                        />
                        <div className={`mt-2 flex items-center gap-1.5 text-[11px] ${isReady ? "text-emerald-300" : "text-amber-300/90"}`} aria-live="polite">
                          <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true">
                            {isReady
                              ? <path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" />
                              : <><path d="M12 9v4M12 17h.01" strokeLinecap="round" /><path d="M10.3 3.5 2.9 16.2A2 2 0 0 0 4.6 19h14.8a2 2 0 0 0 1.7-2.8L13.7 3.5a2 2 0 0 0-3.4 0Z" strokeLinejoin="round" /></>}
                          </svg>
                          <span>
                            {isReady
                              ? readyMessage
                              : (t("upload.anchor_lyrics_required") || "Pegá al menos una línea para activar la sincronización exacta.")}
                          </span>
                        </div>
                      </div>
                    )}
                    </div>
                  </section>
                );
              })()}
              {/* Language pills. Default 'es' is highlighted on file
                  load — operator can click another to override, or
                  click 'auto' to let Whisper detect (not recommended
                  for Spanish catalogue: ~50% misdetection rate). */}
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[11px] text-gray-600 mr-1">{t("upload.lang_label") || "Idioma:"}</span>
                {LANGUAGES.map((l) => (
                  <button
                    key={l.code || "auto"}
                    type="button"
                    onClick={() => updateField(i, "language", l.code)}
                    className={`text-[11px] font-bold px-2 py-1 rounded-md transition-all uppercase
                      ${entry.language === l.code
                        ? "bg-brand/20 text-brand"
                        : "text-gray-600 hover:text-gray-400 hover:bg-white/[0.03]"
                      }`}
                  >{l.code || (t("lang.auto") || "auto")}</button>
                ))}
              </div>
              </>)}

              {/* Personalizar toggle — only shown in batch mode (2+ songs) */}
              {files.length > 1 && (
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
              )}

              {/* Per-track override drawer — only in batch mode */}
              {files.length > 1 && isPersonalizing && (
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
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.effect_label") || "Efecto:"}</span>
                        <Listbox value={entry.effect || ""} onChange={(v) => updateField(i, "effect", v)} options={EFFECTS} className="flex-1" ariaLabel={t("upload.effect_label") || "Efecto"} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.animation_label") || "Animación:"}</span>
                        <Listbox value={entry.lyricsAnimation || "none"} onChange={(v) => updateField(i, "lyricsAnimation", v)} options={LYRICS_ANIMATIONS} className="flex-1" ariaLabel={t("upload.animation_label") || "Animación"} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[11px] text-gray-600 shrink-0">{t("upload.transition_label") || "Transición:"}</span>
                        <Listbox value={entry.lineTransition || "none"} onChange={(v) => updateField(i, "lineTransition", v)} options={LINE_TRANSITIONS} className="flex-1" ariaLabel={t("upload.transition_label") || "Transición"} />
                      </div>
                    </>
                  )}
                  {SHOW_UPLOAD_TYPOGRAPHY && (<>
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_label") || "Tipografía:"}</span>
                    <Listbox value={entry.font || ""} onChange={(v) => updateField(i, "font", v)} options={FONTS} className="flex-1" ariaLabel={t("upload.font_label")} />
                  </div>
                  {/* Text case pills */}
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-gray-600 shrink-0">{t("upload.text_case_label") || "Texto:"}</span>
                      <span className="rounded-full bg-accent/[0.08] px-1.5 py-0.5 text-[8px] font-bold uppercase text-accent ring-1 ring-accent/20">
                        {t("whatsnew.new_badge") || "Nuevo"}
                      </span>
                      <HelpTip text={t("announce.typocase_tagline")} />
                      <div className="flex gap-1">
                        {TEXT_CASE_OPTS.map((opt) => (
                          <button key={opt.code} type="button"
                            title={opt.code === "sentence" ? `${opt.label} · ${t("announce.typocase_tagline")}` : opt.label}
                            onClick={() => updateField(i, "textCase", opt.code)}
                            onMouseEnter={() => setHoverCaseRow({ idx: i, code: opt.code })}
                            onMouseLeave={() => setHoverCaseRow(null)}
                            className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                              ${(entry.textCase || "upper") === opt.code
                                ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                                : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                              }`}
                          >{opt.d}</button>
                        ))}
                      </div>
                    </div>
                    {hoverCaseRow?.idx === i && (
                      <div className="mt-1.5 px-3 py-1.5 rounded-md bg-black/40 ring-1 ring-white/[0.06] flex items-baseline gap-2 animate-fade-in">
                        <span className="text-[11px] font-mono text-white/80 tracking-wide">
                          {applyTextCase(t("upload.sample_lyric"), hoverCaseRow.code)}
                        </span>
                        <span className="text-[10px] text-gray-600">← {t("upload.case_preview_help")}</span>
                      </div>
                    )}
                  </div>
                  {/* Font scale */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.font_scale_label") || "Tamaño:"}</span>
                    <div className="flex items-end gap-1">
                      {[
                        { code: "0.75", cls: "text-[9px]"  }, { code: "0.9",  cls: "text-[11px]" },
                        { code: "1.0",  cls: "text-[13px]" }, { code: "1.15", cls: "text-[16px]" },
                        { code: "1.3",  cls: "text-[19px]" }, { code: "1.5",  cls: "text-[22px]" },
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
                  {/* Lyric transition + Text motion legacy: deprecados
                      2026-05-23. Los reemplazan lyrics_animation +
                      line_transition que se eligen en el editor. */}
                  {/* Text contrast */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0">{t("upload.contrast_label") || "Contraste:"}</span>
                    <div className="flex gap-1">
                      {[
                        { code: "subtle", style: { WebkitTextStroke: "0px", textShadow: "none" },         label: t("upload.contrast_subtle") || "Suave" },
                        { code: "medium", style: { WebkitTextStroke: "0.5px black", textShadow: "0 0 3px rgba(0,0,0,0.8)" }, label: t("upload.contrast_medium") || "Medio" },
                        { code: "strong", style: { WebkitTextStroke: "1px black",   textShadow: "0 0 6px rgba(0,0,0,1), -1px -1px 0 #000, 1px 1px 0 #000" }, label: t("upload.contrast_strong") || "Fuerte" },
                      ].map((opt) => (
                        <button key={opt.code} type="button" title={opt.label}
                          onClick={() => updateField(i, "textContrast", opt.code)}
                          className={`px-2 py-1 rounded-md text-[13px] font-bold text-white transition-all
                            ${(entry.textContrast || "medium") === opt.code
                              ? "bg-brand/20 ring-1 ring-brand/40"
                              : "bg-surface-3/40 hover:bg-surface-3/60"
                            }`}
                          style={{ ...opt.style, paintOrder: "stroke fill" }}
                        >A</button>
                      ))}
                    </div>
                  </div>
                  </>)}
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
              const f = e.target.files[0];
              e.target.value = "";
              if (!f) return;
              const isVideo = /\.(mp4|mov)$/i.test(f.name);
              const capMb = isVideo ? MAX_BG_VIDEO_MB : MAX_BG_IMAGE_MB;
              if (f.size > capMb * 1024 * 1024) {
                setBgOversize({ name: f.name, capMb, sizeMb: (f.size / 1048576).toFixed(0) });
                console.warn(
                  "[upload-reject] bg oversize (cap " + capMb + " MB):",
                  `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`,
                );
                return;
              }
              setBgOversize(null);
              onBackgroundFile?.(f);
              onBackgroundId?.(null);
            }}
          />

          <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
            {t("upload.video_background")}
            <HelpTip articleId="backgrounds" />
          </p>
          <p className="text-[11px] text-gray-600 mb-2 mt-0.5">
            {bgMode === "auto" ? t("upload.bg_auto_summary")
              : bgMode === "library" ? t("upload.bg_library_summary")
              : t("upload.bg_custom_summary")}
          </p>

          {/* Mode selector — oculto en art track (siempre cover custom). */}
          {!artTrack && (
          <div className="flex gap-1 p-1 glass rounded-xl w-fit mb-3" data-tour="upload-bg-tabs">
            {[
              { id: "auto", label: t("upload.bg_auto") || "Generar con IA" },
              { id: "library", label: t("upload.bg_library") || "Library" },
              // "Subir el mío" (portada custom) NO se ofrece en edición: el
              // backend /edit no tiene edit_type "custom" (valid_edit_types sin
              // custom), así que elegirlo daba un no-op silencioso — el operador
              // "cambiaba el fondo" y el botón de aprobar decía "No cambiaste
              // nada". Fuera del wizard de edición sigue disponible al crear.
              ...(editMode ? [] : [{ id: "custom", label: t("upload.bg_custom_tab") || "Upload" }]),
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
                className={`px-4 py-1.5 rounded-lg text-label transition-all ${
                  bgMode === m.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>
          )}

          {/* Auto mode */}
          {bgMode === "auto" && (
            <div className="glass rounded-card px-4 py-3">
              <p className="text-xs text-gray-400">
                <svg className="inline-block w-3.5 h-3.5 mr-1.5 -mt-0.5 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
                {t("upload.bg_auto_desc") || "AI will generate a unique background based on the song's mood and lyrics."}
              </p>
              {/* Copy honesto de no-convergencia (incidente Gaby 2026-07-08:
                  3 regens "a ver si sale", cada una una escena distinta).
                  Nombrar el comportamiento acá evita descubrirlo a $0.90
                  el intento: la IA NO refina, reinterpreta. */}
              <p className="text-[11px] text-amber-300/80 mt-2">
                {t("upload.bg_ai_nonconverge_hint") ||
                  "La IA reinterpreta la canción en cada generación: sin indicaciones, cada intento puede dar una escena totalmente distinta. Para un resultado exacto usá la Biblioteca o escribí tu propio prompt."}
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
                          onClick={() => { track("wizard.library_filter", { filter: f.id }); setLibraryFilter(f.id); }}
                          className={`flex items-center gap-2 h-8 px-3 rounded-full text-label transition-all ${
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
                            onClick={() => {
                              track("wizard.library_select", {
                                asset_id: bg.id,
                                file_type: bg.file_type,
                                had_used_badge: Boolean(usageMap[bg.id]?.used),
                              });
                              onBackgroundId?.(bg.id); onBackgroundFile?.(null);
                            }}
                            className={`rounded-card overflow-hidden text-left group bg-surface-2/40 transition-all ${
                              selected
                                ? "ring-2 ring-brand shadow-glow"
                                : "ring-1 ring-white/[0.04] hover:ring-white/[0.10] hover:bg-surface-2/70"
                            }`}
                          >
                            <div className="aspect-video bg-black/30 relative">
                              {bg.file_type === "mp4" ? (
                                <video
                                  src={backgroundPreviewUrl(API, bg.id, libraryPreviewTokens[String(bg.id)]) || undefined}
                                  className="w-full h-full object-cover"
                                  preload="metadata"
                                  muted loop playsInline
                                  onMouseEnter={(e) => { e.currentTarget.play().catch(() => {}); }}
                                  onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
                                />
                              ) : (
                                <img
                                  src={backgroundPreviewUrl(API, bg.id, libraryPreviewTokens[String(bg.id)]) || undefined}
                                  className="w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-500"
                                  alt={bg.name}
                                  /* 80 assets full-res: sin lazy, el grid
                                     dispara TODO junto y la biblioteca
                                     "no carga" (incidente 2026-06-11). */
                                  loading="lazy"
                                  decoding="async"
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
                              <p className="text-caption text-white truncate">{bg.name}</p>
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
                              onClick={() => { track("wizard.library_mode", { asset_id: backgroundId, mode: m.id }); onBackgroundMode?.(m.id); }}
                              className={`h-8 px-3 rounded-full text-label transition-all ${
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
                              "Se usará un frame de este video como referencia visual y se generará un clip nuevo derivado del original."}
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
              {bgOversize && (
                <div className="mb-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20">
                  <p className="text-[11px] text-red-300">
                    {t("upload.bg_oversize", {
                      name: bgOversize.name,
                      size: bgOversize.sizeMb,
                      max: bgOversize.capMb,
                    })}
                  </p>
                  <button
                    onClick={(e) => { e.stopPropagation(); setBgOversize(null); }}
                    className="mt-1 text-[11px] text-red-400/60 hover:text-red-300"
                  >{t("common.dismiss") || "dismiss"}</button>
                </div>
              )}
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
                          {t("upload.animate_image_hint") || "El video cinemático anima tu imagen en lugar de usar zoom/pan"}
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
    summaryParts.push(t("upload.bg_auto") || "Generar con IA");
  }
  const summary = summaryParts.join(" · ");

  return (
    // QA fix 2026-05-28 (UX, scroll architecture): en lg+ el wrapper
    // ahora ocupa exactamente el alto que le da el padre (lg:flex-1 +
    // lg:min-h-0 + lg:overflow-hidden viniendo de App.jsx::newBatchScreen).
    // Adentro, el grid es flex-col (flex-1) para que la grid de 3
    // columnas tome todo el alto disponible y la columna RIGHT pueda
    // hacer su propio overflow-y-auto sin tener que pelearse con el
    // page-scroll. pb-28 (clear del CTA flotante "Aprobar y generar") se
    // mantiene solo en mobile — en desktop el CTA es fixed bottom-0 con
    // su propio espacio. Mobile (<lg) no se toca: pb-28 + page-scroll.
    <div className="wizard-workspace w-full px-2 md:px-6 pb-28 lg:pb-0 lg:h-full lg:overflow-hidden lg:flex lg:flex-col">
      <UploadTour user={user} />
      {/* Pre-upload short-circuit: drop zone-only layout aplica solo cuando
          NO hay contenido reviewable Y NO estamos en edit-mode. Sin estas
          dos condiciones extra, /edit-lyrics (lockedSteps no vacío) y
          /review post-transcribe (donde `files` legítimamente puede estar
          vacío) caían acá y se veía "Crear videos" en vez del editor.
          Bug 2026-05-27, fix UploadZone-shortcircuit. */}
      {files.length === 0 && !hasReviewableContent && !_editMode ? (
        /* UX 2026-05-29: empty state centered vertically + bigger max-width
           so the dropzone doesn't feel like a 250px island floating in the
           viewport. Matches the visual weight of the Dashboard hero. */
        <div className="max-w-3xl mx-auto py-8 md:py-12 flex flex-col items-center justify-center min-h-[55vh]">
          <div className="w-full">{_videoTypeSelector}</div>
          <div className="w-full">{_dropZone}</div>
          <p className="text-[11px] text-ink-secondary/60 mt-6 text-center max-w-md">
            {t("upload.empty_hint") || "Tip: para mejor calidad, usá audio sin clipping y con voz al frente de la mezcla."}
          </p>
        </div>
      ) : (
      /* UI F1+F2+F3 (2026-05-26): el grid se reconfigura cuando el
         wizard llega al paso 6. En pasos 1-5 el operador está
         configurando opciones — preview central grande tiene sentido.
         En paso 6 está corrigiendo 60+ líneas de lyric, una actividad
         que necesita ancho horizontal en el panel derecho. Reparto:

           Pasos 1-5 (configurar):  190px sidebar + 1fr preview + 460px max derecha
           Paso 6 (trabajar):       56px sidebar (icon-only) + 320px preview thumbnail + 1fr derecha

         A 1500 px viewport en paso 6 el panel de trabajo gana +640 px
         vs el layout original. El sidebar se reduce a iconos numerados
         con tooltip; el preview pasa a thumbnail con compact=true
         (oculta caption de movement/effect que ya no se está editando). */
      (() => {
        const isStep6 = wizardStep === 6 && hasReviewableContent;
        // QA fix 2026-05-28 (UX, polish, 2nd pass): el operador reportó
        // que con 168 px el label "Tipografía & Animación" se clippeaba
        // contra el borde del column (overflow-hidden del scroll
        // context). Subimos a 200 px — cubre cómodamente todos los
        // labels en es/en/pt incluso con el icono numérico + padding.
        // El preview baja a 360-500 px (≈40 px menos) lo cual sigue
        // bien usable para el operador.
        const gridCols = isStep6
          ? "lg:grid-cols-[200px_minmax(360px,500px)_minmax(0,1fr)]"
          : "lg:grid-cols-[190px_minmax(0,1fr)_minmax(400px,460px)]";
        // Studio focus: when LyricsEditor emits `editor-focus-mode`, collapse
        // the three-column wizard into a two-column editing workspace. The
        // step rail disappears, but the live preview stays docked at a compact
        // 320–400 px so the operator can keep watching the video while the
        // timeline uses all remaining width. Returning to the regular mode
        // restores the original three-column wizard automatically.
        return (
        // QA fix 2026-05-28: grid pasa a llenar el alto del padre y a ser
        // su propio scroll context en lg+. items-start mantiene la
        // alineación al top de las columnas (necesario para que el
        // sticky-top-4 del stepper y preview siga funcionando). lg:flex-1
        // lg:min-h-0 deja la grid ocupar el espacio que el flex-col
        // exterior le da. lg:overflow-hidden previene que la grid haga
        // overflow al body — el scroll vive en la columna RIGHT.
        <div className={`wizard-workspace-grid flex flex-col lg:grid ${gridCols} [.editor-focus-mode_&]:lg:grid-cols-[clamp(320px,24vw,400px)_minmax(0,1fr)] gap-6 items-start lg:items-stretch lg:h-full lg:min-h-0 lg:overflow-hidden lg:flex-1`}>

        {/* LEFT — step rail (vertical on desktop, horizontal pills on mobile).
            Paso 6 "Lyrics" se ve siempre; está deshabilitado hasta que
            haya contenido reviewable (hasReviewableContent prop). Cuando
            se activa, el useEffect de arriba auto-avanza el wizard a
            step 6 y permite navegar libremente entre 4↔6 (operador
            cambia font/animation en paso 4, vuelve a paso 6 a aprobar).

            QA fix 2026-05-28 (UX): la versión anterior ocultaba labels en
            step 6 para ganar espacio. Operador reportó que no se entendía
            qué era cada paso. Volvemos a mostrar labels SIEMPRE — el
            grid arriba se ajustó a 168 px de sidebar para acomodarlas. */}
        <nav className="wizard-step-rail flex lg:flex-col gap-1.5 lg:gap-1 overflow-x-auto lg:overflow-visible lg:sticky lg:top-4 w-full lg:w-auto order-first [.editor-focus-mode_&]:hidden">
          {WIZARD_STEPS.map((s) => {
            const isLyrics = s.id === 6;
            const lyricsDisabled = isLyrics && !hasReviewableContent;
            const locked = _lockedSet.has(s.id);
            const disabled = lyricsDisabled || locked;
            const active = !disabled && wizardStep === s.id;
            const done = !disabled && wizardStep > s.id;
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => { if (!disabled) goStep(s.id); }}
                disabled={disabled}
                aria-disabled={disabled}
                title={locked
                  ? (t("upload.step_locked_hint") || "No editable en este modo — usá \"Regenerar fondo\" desde el video.")
                  : lyricsDisabled
                    ? (t("upload.step_lyrics_hint") || "Disponible después de \"Revisar lyrics\"")
                    : (isStep6 ? s.label : undefined)}
                className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-[12.5px] font-medium whitespace-nowrap transition-all text-left shrink-0 ${
                  disabled
                    ? "text-gray-600 cursor-not-allowed opacity-50"
                    : active ? "bg-brand/[0.12] text-white ring-1 ring-brand/35"
                             : "text-gray-400 hover:text-white hover:bg-white/[0.04]"
                }`}
              >
                <span className={`w-6 h-6 rounded-full grid place-items-center text-[11px] font-bold shrink-0 ${
                  disabled ? "bg-surface-3/50 text-gray-600 border border-dashed border-gray-600/40"
                           : active ? "bg-brand text-white"
                                    : done ? "bg-accent/20 text-accent"
                                           : "bg-surface-3 text-gray-400"
                }`}>{done ? "✓" : s.id}</span>
                <span className="ml-0">{s.label}</span>
              </button>
            );
          })}
        </nav>

        {/* CENTER — stage: live preview of the result.

            UI F3 (2026-05-26): en paso 6 el preview se monta con
            compact=true → el componente oculta el caption inferior
            de movement/effect (controles que ya no se están
            editando), y el max-width del contenedor se ajusta al
            grid column (260-320 px). Mantiene sticky para acompañar
            el scroll del timeline en el panel derecho. */}
        <div className="wizard-preview-stage lg:sticky lg:top-4 space-y-2 min-w-0 w-full">
          {/* UI v1.1 (2026-05-30): toggle pill — "Letra / Portada". The
              central preview shows whichever face is selected; the bottom
              fan-out of states (Lyrics editor, batch progress, ready, etc.)
              only renders when previewFace === "lyrics" so toggling to
              "title" doesn't accidentally hide the karaoke editor below.
              Auto-flips when the operator touches a Portada control (see
              updateBatchDefault below); explicit click here overrides. */}
          <div className="flex items-center gap-1 p-0.5 rounded-lg bg-surface-2/40 ring-1 ring-white/[0.04] w-fit">
            {[
              { code: "lyrics", label: t("preview.face_lyrics") || "Letra" },
              { code: "title",  label: t("preview.face_title")  || "Portada" },
            ].map((opt) => {
              const active = previewFace === opt.code;
              return (
                <button
                  key={opt.code}
                  type="button"
                  onClick={() => setPreviewFace(opt.code)}
                  className={`px-3 py-1 rounded-md text-[11px] font-medium transition-all
                    ${active
                      ? "bg-brand/20 text-brand ring-1 ring-brand/30"
                      : "text-gray-500 hover:text-gray-200"
                    }`}
                  aria-pressed={active}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>

          {/* Title-card face: rendered in the same slot as the lyric preview
              so the operator's eye never has to travel. Same proportions
              (aspect-video) as WizardLivePreview, so the layout doesn't
              jump when toggling. */}
          {previewFace === "title" ? (
            <TitleCardPreview
              artist={titlePreviewArtist}
              song={titlePreviewSong}
              font={batchDefaults.font || ""}
              textCase={batchDefaults.textCase || "upper"}
              frameFormat={batchDefaults.frameFormat || "full"}
              template={batchDefaults.titleTemplate || "auto"}
              titleSize={parseFloat(batchDefaults.titleSize) || 1.0}
              artistFont={batchDefaults.titleArtistFont || ""}
              songFont={batchDefaults.titleSongFont || ""}
              songLines={
                (batchDefaults.titleSongBreak || "").includes("\n")
                  ? batchDefaults.titleSongBreak.split("\n")
                  : null
              }
              firstLyricStart={_firstLyricStart}
            />
          ) : artTrack ? (
            /* Art track: preview del layout real (cover blureada con scrim +
               cover con sombra a la derecha + barras EQ latiendo + título en
               zona segura), aproximación visual del render. */
            <div className="relative w-full aspect-video rounded-xl overflow-hidden bg-black">
              {customPreviewUrl ? (
                <>
                  <style>{`@keyframes atwave { 0%, 100% { transform: scaleY(0.45); } 50% { transform: scaleY(1); } }`}</style>
                  <img src={customPreviewUrl} alt="" className="absolute inset-0 w-full h-full object-cover scale-110 blur-2xl brightness-50 saturate-[.75]" />
                  {/* Moving effect (screen-blended, matching the render). */}
                  {(() => {
                    const fx = EFFECTS.find((e) => e.code === (batchDefaults.effect || "") && e.sample);
                    return fx ? (
                      <video key={fx.code} src={fx.sample} className="absolute inset-0 w-full h-full object-cover pointer-events-none mix-blend-screen" autoPlay loop muted playsInline />
                    ) : null;
                  })()}
                  <div className="absolute inset-0 bg-gradient-to-r from-black/60 via-black/25 to-transparent" />
                  <div className="absolute inset-0" style={{ boxShadow: "inset 0 0 120px 40px rgba(0,0,0,0.45)" }} />
                  <img src={customPreviewUrl} alt="" className="absolute top-1/2 right-[8%] -translate-y-1/2 h-[62%] aspect-square object-cover rounded shadow-2xl shadow-black/70 ring-1 ring-white/10" />
                  <div className="absolute left-[9%] top-[38%] text-[8px] md:text-[10px] uppercase tracking-[0.35em] text-white/60 drop-shadow">{t("arttrack.official_audio") || "Official Audio"}</div>
                  <div className="absolute left-[9%] top-[44%] flex items-center gap-[4px] h-[16%]">
                    {Array.from({ length: 24 }).map((_, i) => (
                      <span
                        key={i}
                        className="w-[7px] bg-white/90 rounded-full"
                        style={{
                          height: `${30 + Math.abs(Math.sin(i * 1.7)) * 65}%`,
                          animation: "atwave 1.1s ease-in-out infinite",
                          animationDelay: `${(i % 7) * 90}ms`,
                        }}
                      />
                    ))}
                  </div>
                  <div className="absolute left-[9%] top-[66%] max-w-[44%] text-white">
                    <div className="font-bold text-lg md:text-2xl leading-tight drop-shadow line-clamp-2">{titlePreviewSong || t("upload.video_type_art") || "Art Track"}</div>
                    <div className="text-sm md:text-base text-white/85 drop-shadow">{titlePreviewArtist}</div>
                  </div>
                  {(labelLine || "").trim() ? (
                    <div className="absolute left-[9%] bottom-[6%] text-[9px] md:text-[11px] text-white/55 drop-shadow">{labelLine}</div>
                  ) : null}
                </>
              ) : (
                <div className="absolute inset-0 flex items-center justify-center text-center text-gray-400 text-sm px-6">
                  {t("arttrack.cover_missing_desc") || "Subí el cover en el paso “Modo” para ver la vista previa."}
                </div>
              )}
              <span className="absolute top-2 left-2 text-[10px] uppercase tracking-[0.18em] text-white/70">{t("upload.video_type_art") || "Art Track"}</span>
            </div>
          ) : (bgMode === "auto" || bgMode === "library" || (bgMode === "custom" && customPreviewUrl)) ? (
            <WizardLivePreview
              style={style}
              customColors={customColors}
              movementStyle={hoverMovement ?? batchDefaults.movementStyle}
              effect={hoverEffect ?? batchDefaults.effect}
              lyricsAnimation={hoverAnimation ?? batchDefaults.lyricsAnimation}
              lineTransition={hoverTransition ?? batchDefaults.lineTransition}
              lyricColor={batchDefaults.lyricColor || "#FFFFFF"}
              lyricSungColor={batchDefaults.lyricSungColor || "#FFFFFF"}
              /* QA fix 2026-05-28: cuando el operador selecciona un fondo
                 de biblioteca en step 2, la BASE del preview es ese
                 asset (en vez del clip del movement). Para assets .mp4
                 (file_type "mp4") va como <video>; para imágenes
                 (jpg/png) se renderiza como <img> via la prop nueva
                 clipIsVideo. Hasta el follow-up, el operador veía negro
                 cuando elegía un asset image porque el <video> element
                 fallaba a cargar la imagen. */
              clipSrc={(() => {
                if (bgMode === "custom" && customPreviewUrl) {
                  return customPreviewUrl;
                }
                if (bgMode === "library" && backgroundId) {
                  return backgroundPreviewUrl(API, backgroundId, libraryPreviewTokens[String(backgroundId)]);
                }
                return (MOVEMENT_STYLES.find((m) => m.code === (hoverMovement ?? batchDefaults.movementStyle))?.sample) || "/movement_samples/estandar.mp4";
              })()}
              clipIsVideo={(() => {
                if (bgMode === "custom" && customPreviewUrl) {
                  return /\.(mp4|mov)$/i.test(backgroundFile?.name || "");
                }
                if (bgMode === "library" && backgroundId) {
                  const sel = libraryBgs.find((b) => b.id === backgroundId);
                  return sel?.file_type === "mp4";
                }
                // Movement samples are MP4 EXCEPT foto-fija (static .jpg);
                // returning true for the .jpg would render it in a <video>
                // element → black preview (the exact bug above).
                const mv = MOVEMENT_STYLES.find((mm) => mm.code === (hoverMovement ?? batchDefaults.movementStyle));
                return mv?.kind !== "image";
              })()}
              /* Typography 2026-05-26: cerrar el gap entre los controles del
                 paso 4 (font/case/size/contrast) y el preview central. Antes
                 el comentario al lado del bloque mentía — "el preview ya
                 escucha estos cambios live" — pero el componente no recibía
                 los props, así que el operador veía siempre Montserrat
                 extrabold en lowercase con contraste medium hardcoded
                 mientras configuraba. Defaults en el componente
                 ("/upper/1.0/medium") cubren el resto de pasos cuando
                 batchDefaults aún no fue tocado. */
              font={batchDefaults.font || ""}
              textCase={batchDefaults.textCase || "upper"}
              frameFormat={batchDefaults.frameFormat || "full"}
              fontScale={batchDefaults.fontScale || "1.0"}
              textContrast={batchDefaults.textContrast || "medium"}
              mode={sceneMode}
              lyric={_previewLyric}
              /* Phase C 2026-05-25: el ref de playback tick permite al
                 preview leer la línea activa + currentTime para hacer
                 word-jump real cuando el operador está reproduciendo el
                 audio en la review (step 6). Sin el ref, el preview cae
                 al modo legacy (lyric loop con `_previewLyric`). */
              playbackTickRef={playbackTickRef}
              /* Post-render edit: MP4 ya renderizado del job. Cuando viene,
                 el preview muta a "Resultado actual" y todos los overlays
                 (palette/grade/karaoke sim) se cortocircuitan.

                 QA fix 2026-05-28 (UX, second pass): operador reportó que
                 en step 6 quedaban DOS reproductores (el MP4 con sus
                 controles + el audio bar del LyricsEditor) sin sincronizar
                 — confuso. Y la live preview con karaoke reflejaba mejor
                 el flow del editor (la línea activa se ilumina al pasar
                 el audio via playbackTickRef). Ahora droppeamos el MP4 en
                 TODO el edit-wizard: la live preview corre en todos los
                 pasos (incluido step 6), reflejando movement/fondo/
                 tipografía/color + drag-resize, y su karaoke sigue el
                 audio del LyricsEditor. Si el operador quiere ver el
                 video resultante actual, lo ve en JobDetail. */
              renderedVideoUrl={null}
              /* UI F3 + F5 (2026-05-26): compact en paso 6; placeholderBg
                 mientras el pre-gen del fondo no terminó. */
              compact={isStep6}
              placeholderBg={isStep6 && bgStatus !== "done"}
            />
          ) : (
            // Custom SIN archivo todavía (o stub post-refresh): no hay
            // nada que mostrar — con archivo real, el branch de arriba lo
            // usa como base del live preview (fix audit 2026-06-11).
            <div className="aspect-video rounded-2xl ring-1 ring-white/[0.08] bg-surface-2/50 grid place-items-center text-gray-500 text-[13px]">
              {t("upload.bg_custom_tab") || "Fondo subido"}
            </div>
          )}
          <p className="text-[10px] text-gray-600 px-1">
            {_previewLyric
              ? `${t("upload.preview_editing") || "Línea actual"}: ${_previewLyric}${files.length > 1 ? ` · +${files.length - 1}` : ""}`
              : (t("upload.preview_disclaimer") || "Aproximación del mood y el movimiento. El fondo final lo genera la IA.")}
          </p>
          {/* 2026-07-16: slot para el player bar de LyricsEditor (paso 6).
              Lo portalea acá, bajo el video, para que la columna derecha
              quede full con la letra. Fuera del paso 6 no se monta. */}
          {isStep6 && onPlayerSlotRef && (
            <div ref={onPlayerSlotRef} className="mt-1" data-testid="wizard-player-slot" />
          )}
        </div>

        {/* RIGHT — active step controls only (revealed one step at a time).
            QA fix 2026-05-28: en lg+ este es el único scroll context del
            wizard. h-full toma el alto que el grid le asigna; overflow-y-
            auto deja que el operador scrollee la lista de lyrics (o
            cualquier control del paso activo) sin mover el resto del
            layout (banners + stepper + preview quedan fijos arriba).
            min-h-0 es necesario en flex column children para que el
            overflow funcione (sin esto el child colapsaría a su content
            height antes de aplicar el overflow). */}
        {/* px-1.5 + py-0.5: `overflow-y-auto` fuerza overflow-x a clip, que
            cortaba el ring/glow redondeado de las cards seleccionadas (Inspirado,
            Multi-escena, Auto de colores) contra los bordes. El padding les da
            aire para que el borde no quede recortado. */}
        {/* lg:pb-24: el footer del wizard es `fixed bottom-0` (~68px de alto).
            Sin padding inferior, el último contenido del paso (ej. "Mi prompt" +
            Multi-escena, el modo más alto) quedaba TAPADO detrás del footer y no
            se podía scrollear por encima ("trabado", QA 2026-07-01). El padding
            reserva el espacio del CTA fijo. En mobile el outer ya tiene pb-28. */}
        <div className="wizard-controls-panel space-y-4 min-w-0 w-full px-1.5 py-0.5 lg:h-full lg:min-h-0 lg:overflow-y-auto lg:pb-24">
          {files.length > 1 && (
            <div className="flex items-center gap-1.5 px-1">
              <span className="inline-flex items-center gap-1.5 text-[10px] text-gray-500 uppercase tracking-[0.16em]">
                <svg className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                </svg>
                {t("upload.applies_to_tracks", { count: files.length })}
              </span>
            </div>
          )}

          {/* STEP 1 — Subí: manage files + per-track metadata */}
          {wizardStep === 1 && (
            <>
              {_videoTypeSelector}
              {_dropZone}
              {_filesBlock}
            </>
          )}

          {/* STEP 2 — Modo: source + the 3 modes + contextual mood/prompt */}
          {wizardStep === 2 && (
            <>
              {_bgBlock}
              {/* Art track: efecto de movimiento opcional (partículas/luz que
                  se mueven sobre la portada). Reusa los mismos loops que los
                  lyric videos. NO mostramos los estilos de movimiento de
                  cámara — no aplican al formato art track. */}
              {artTrack && (
                <div className="mt-4 pt-3 border-t border-white/[0.05]">
                  <p className="text-[11px] text-gray-400 font-medium">
                    {t("upload.arttrack_effect_title") || "Efecto en movimiento (opcional)"}
                  </p>
                  <p className="text-[10px] text-gray-600 mt-0.5 mb-2">
                    {t("upload.arttrack_effect_desc") || "Partículas/luz que se mueven sobre la portada, como el video de referencia. Se aplica a master y short."}
                  </p>
                  <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
                    {EFFECTS.map((e) => {
                      const active = (batchDefaults.effect || "") === e.code;
                      return (
                        <button
                          key={e.code || "none"}
                          type="button"
                          onClick={() => updateBatchDefault("effect", e.code)}
                          aria-label={`${e.label}: ${e.desc}`}
                          title={e.desc}
                          className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                            active
                              ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                              : "border-white/[0.06] hover:border-white/[0.20]"
                          }`}
                        >
                          <div className="aspect-video bg-black relative overflow-hidden">
                            {e.sample ? (
                              <video src={e.sample} className="w-full h-full object-cover pointer-events-none" autoPlay loop muted playsInline />
                            ) : (
                              <div className="w-full h-full grid place-items-center text-gray-500 text-[10px]" style={{ background: "radial-gradient(120% 100% at 50% 0,#241a40,#0b0820)" }}>
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
                            <p className={`text-label leading-tight ${active ? "text-white" : "text-gray-200"}`}>{e.label}</p>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              {bgMode === "auto" && (
                <>
                  <div className="grid grid-cols-1 gap-2">
                    {[
                      { code: "auto",   icon: "✨", label: t("upload.mode_auto") || "Auto",                                 desc: t("upload.mode_auto_desc") || "La IA elige la escena por el género y el mood." },
                      { code: "lyrics", icon: "🎤", label: t("upload.inspired_by_lyrics_label") || "Inspirado en la letra", desc: t("upload.mode_lyrics_desc") || "El fondo nace de lo que dice la canción.", badge: t("upload.mode_edge") || "ÚNICO" },
                      { code: "prompt", icon: "✍️", label: t("upload.bg_prompt_label_short") || "Mi prompt",                desc: t("upload.mode_prompt_desc") || "Vos describís el fondo, con opción usar tal cual." },
                    ].map((m) => {
                      const sel = sceneMode === m.code;
                      return (
                        <button
                          key={m.code}
                          type="button"
                          onClick={() => selectSceneMode(m.code)}
                          className={`text-left rounded-card px-4 py-3 flex items-start gap-3 border transition-all duration-200 ${
                            sel ? "border-transparent ring-1 ring-brand/50 bg-brand/[0.08] shadow-glow"
                                : "border-white/[0.06] bg-surface-2/40 hover:border-white/[0.18]"
                          }`}
                        >
                          <span className={`w-9 h-9 rounded-xl grid place-items-center text-[17px] shrink-0 ${sel ? "bg-brand" : "bg-surface-3"}`}>{m.icon}</span>
                          <span className="min-w-0">
                            <span className="flex items-center gap-2">
                              <span className={`text-[13px] font-semibold ${sel ? "text-white" : "text-gray-200"}`}>{m.label}</span>
                              {m.badge && <span className="text-[8px] font-bold tracking-[0.04em] px-1.5 py-0.5 rounded bg-accent/15 text-accent">{m.badge}</span>}
                            </span>
                            <span className="block text-[11px] text-gray-500 mt-0.5 leading-snug">{m.desc}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>

                  {/* Multi-escena: capa PREMIUM ortogonal a los 3 modos de arriba.
                      Funciona con cualquiera (Auto / letra / Mi prompt). Siempre
                      visible (upsell); desbloqueada si scenesEligible.
                      Oculto en edición: activar/desactivar Escenas es un cambio
                      estructural que el /edit no soporta (el backend lo rechaza
                      en jobs multi-escena), así que ofrecerlo sería engañoso. */}
                  {!editMode && (() => {
                    const on = !!enableScenes;
                    const locked = !scenesEligible;
                    return (
                      <button
                        type="button"
                        aria-pressed={!locked && on}
                        aria-label={locked
                          ? (t("upload.scenes_locked_aria") || "Multi-escena — disponible en el plan superior")
                          : (t("upload.scenes_toggle") || "Multi-escena")}
                        onClick={() => {
                          markScenesSeen();
                          if (locked) { track("wizard.scenes_upsell"); setShowScenesUpsell(true); return; }
                          track("wizard.scenes", { enabled: !on });
                          onEnableScenesChange && onEnableScenesChange(!on);
                        }}
                        className={`w-full text-left rounded-card px-4 py-3 flex items-center gap-3 border transition-all duration-200 outline-none focus-visible:ring-2 focus-visible:ring-brand/60 ${
                          on ? "border-transparent ring-1 ring-brand/50 bg-brand/[0.08] shadow-glow"
                             : "border-brand/25 bg-gradient-to-r from-brand/[0.07] to-transparent hover:border-brand/45"
                        }`}
                      >
                        <span className="relative w-9 h-9 rounded-xl grid place-items-center text-[17px] shrink-0 bg-gradient-to-br from-brand to-accent">
                          🎬
                          {!scenesSeen && (
                            <span className="absolute -top-1 -right-1 flex h-3 w-3" aria-hidden="true">
                              <span className="absolute inline-flex h-full w-full rounded-full bg-accent opacity-75 animate-ping" />
                              <span className="relative inline-flex h-3 w-3 rounded-full bg-accent ring-2 ring-surface-2" />
                            </span>
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-2">
                            <span className={`text-[13px] font-semibold ${on ? "text-white" : "text-gray-200"}`}>{t("upload.scenes_toggle") || "Multi-escena"}</span>
                            {!scenesSeen && (
                              <span className="text-[9px] font-bold tracking-[0.04em] px-1.5 py-0.5 rounded bg-accent text-white">{t("upload.scenes_new_badge") || "NUEVO"}</span>
                            )}
                            <span className="text-[9px] font-bold tracking-[0.04em] px-1.5 py-0.5 rounded bg-gradient-to-r from-brand to-accent text-white">{t("upload.premium_badge") || "PREMIUM"}</span>
                          </span>
                          <span className="block text-[11px] text-ink-secondary mt-0.5 leading-snug">
                            {t("upload.scenes_toggle_desc") || "Varias escenas con arco narrativo en vez de un fondo único."}
                            <span className="text-brand-light font-medium"> · {(t("upload.scenes_cost_hint") || "{n} créditos (un video normal usa 1)").replace("{n}", scenesCreditCost)}</span>
                          </span>
                        </span>
                        {locked
                          ? <span className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-brand/15 text-brand-light text-[10px] font-semibold">
                              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V7a4 4 0 1 1 8 0v4" /></svg>
                              {t("upload.scenes_locked") || "Plan superior"}
                            </span>
                          : <span className={`shrink-0 w-9 h-5 rounded-full transition-colors relative ${on ? "bg-brand" : "bg-white/10"}`}>
                              <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
                            </span>
                        }
                      </button>
                    );
                  })()}

                  {/* Upsell de Escenas: aparece al tocar la card sin acceso. */}
                  {showScenesUpsell && (
                    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={() => setShowScenesUpsell(false)}>
                      <div role="dialog" aria-modal="true" aria-label={t("upload.mode_scenes") || "Escenas"} className="w-full max-w-sm rounded-card bg-surface-2 ring-1 ring-brand/25 p-5 text-center" onClick={(e) => e.stopPropagation()}>
                        <div className="text-[28px] mb-1">🎬</div>
                        <h3 className="text-[15px] font-bold text-white flex items-center justify-center gap-2">
                          {t("upload.mode_scenes") || "Escenas"}
                          <span className="text-[9px] font-bold tracking-[0.04em] px-1.5 py-0.5 rounded bg-gradient-to-r from-brand to-accent text-white">{t("upload.premium_badge") || "PREMIUM"}</span>
                        </h3>
                        <p className="text-[12px] text-ink-secondary mt-2">
                          {t("upload.scenes_upsell_body") || "Convertí tus videos en un conjunto de escenas con arco narrativo, en vez de un fondo único. Disponible en el plan superior."}
                        </p>
                        <div className="mt-4 flex gap-2 justify-center">
                          <button onClick={() => setShowScenesUpsell(false)} className="text-[12px] font-medium px-3 py-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-gray-300">
                            {t("common.cancel") || "Cancelar"}
                          </button>
                          <button onClick={() => { track("wizard.scenes_upgrade_click"); setShowScenesUpsell(false); window.location.assign("/account?tab=facturacion"); }} className="text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-brand hover:bg-brand-light text-white">
                            {t("upload.scenes_upsell_cta") || "Quiero mejorar mi plan"}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Explainer de Escenas (sólo cuando multi-escena está ON).
                      Refuerza el valor: arco narrativo, cortes musicales, rima. */}
                  {enableScenes && scenesEligible && (
                    <div className="rounded-card bg-gradient-to-br from-brand/[0.08] to-transparent ring-1 ring-brand/20 px-4 py-3">
                      <p className="text-[12px] font-semibold text-accent-light mb-1.5 flex items-center gap-1.5">
                        🎬 {t("upload.scenes_on_title") || "Tu video tendrá varias escenas"}
                      </p>
                      <ul className="text-[11px] text-gray-400 space-y-1">
                        <li>• {t("upload.scenes_b1") || "Una biblia visual compartida → todas parecen el mismo film."}</li>
                        <li>• {t("upload.scenes_b2") || "Los cortes caen en los cambios de la canción (entra el coro, el puente)."}</li>
                        <li>• {t("upload.scenes_b3") || "El coro vuelve siempre a la misma escena."}</li>
                      </ul>
                    </div>
                  )}

                  {sceneMode === "prompt" ? (
                    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
                      <p className="text-[11px] text-gray-600 mb-2">
                        {t("upload.bg_prompt_hint") || "Describí el fondo que querés. Manda sobre el género/concepto y la letra."}
                      </p>
                      <textarea
                        value={batchDefaults.backgroundHint || ""}
                        onChange={(e) => updateBatchDefault("backgroundHint", e.target.value.slice(0, 2000))}
                        rows={3}
                        maxLength={2000}
                        placeholder={t("upload.bg_prompt_placeholder") || "Ej: mansión surreal de noche, pileta vacía, cámara fija, sólo se mueve el reflejo del agua…"}
                        className="w-full text-caption rounded-lg bg-surface-1 border border-white/[0.08] focus:border-brand/50 px-3 py-2 text-gray-200 placeholder:text-gray-600 resize-y outline-none"
                      />
                      <label className="mt-2 flex items-center gap-2.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={!batchDefaults.bgVerbatim}
                          onChange={(e) => updateBatchDefault("bgVerbatim", !e.target.checked)}
                          className="peer sr-only"
                        />
                        <div className="relative w-9 h-5 rounded-full bg-surface-3 peer-checked:bg-brand transition-colors duration-200 shrink-0">
                          <div className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 peer-checked:translate-x-4" />
                        </div>
                        <span className="text-[11px] text-gray-400">
                          {t("upload.bg_enhance_label") || "✨ Mejorar mi prompt con IA"}
                          <span className="block text-[10px] text-gray-600">{t("upload.bg_enhance_hint") || "Por defecto usamos tu texto tal cual."}</span>
                        </span>
                      </label>
                    </div>
                  ) : onStyleChange && (
                    // La paleta se bloquea SOLO en edición: /edit no tiene
                    // edit_type=palette (recolorear pisaría el fondo IA
                    // cacheado). En VARIANTE el fondo se genera de cero y
                    // /variant sí acepta `style` → control desbloqueado.
                    <div className={`rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3 ${
                      editMode && !variantMode ? "relative opacity-60 pointer-events-none select-none" : ""
                    }`}>
                      {editMode && !variantMode && (
                        // QA fix 2026-05-27: step 2 ahora navegable en edit
                        // mode (antes estaba locked entero). Pero la paleta
                        // (style) es structural — cambiarla recolorea el
                        // fondo IA cacheado y el backend no soporta
                        // edit_type=palette. La cerramos a nivel control con
                        // un overlay visible para que el operador entienda
                        // que el dato existe pero no es editable acá.
                        <div className="absolute top-2 right-2 flex items-center gap-1 px-2 py-0.5 rounded-md bg-surface-1 ring-1 ring-white/[0.08] text-[10px] text-gray-500 pointer-events-auto select-text">
                          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                            <path d="M12 11v4M8 11V7a4 4 0 118 0v4M5 11h14v9a1 1 0 01-1 1H6a1 1 0 01-1-1v-9z" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                          <span title={t("editor.locked_structural") || "No editable post-render — generá un video nuevo para cambiar paleta."}>
                            {t("editor.locked_short") || "No editable"}
                          </span>
                        </div>
                      )}
                      <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("upload.style_label")}</p>
                      <p className="text-[11px] text-gray-600 mb-2 mt-0.5">
                        {t("upload.style_desc") || "Cómo se colorea el fondo IA"}
                      </p>

                      {/* Auto — default, the AI picks colors from the song */}
                      <button
                        type="button"
                        onClick={() => { track("wizard.style", { style: "auto" }); onStyleChange("auto"); }}
                        className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border text-left transition-all duration-200 mb-2 ${
                          style === "auto" ? "border-transparent ring-1 ring-brand/50 bg-brand/[0.08]" : "border-white/[0.06] hover:border-white/[0.16]"
                        }`}
                      >
                        <span className="w-8 h-8 rounded-lg shrink-0" style={{ background: "conic-gradient(from 180deg,#6D4AFF,#14C8A8,#b45a14,#6D4AFF)" }} />
                        <span>
                          <span className={`block text-[12px] font-semibold ${style === "auto" ? "text-white" : "text-gray-200"}`}>{t("upload.style_auto") || "Auto"}</span>
                          <span className="block text-[10px] text-gray-500">{t("upload.style_auto_desc") || "La IA elige los colores según la canción"}</span>
                        </span>
                      </button>

                      {/* 4 presets */}
                      <div className="grid grid-cols-2 gap-2">
                        {STYLES.map((s) => (
                          <button
                            key={s.code}
                            type="button"
                            onClick={() => { track("wizard.style", { style: s.code }); onStyleChange(s.code); }}
                            className={`flex flex-col items-center gap-2 px-2 py-2.5 rounded-xl border text-label transition-all duration-200
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

                      {/* Personalizado — pick your own colors */}
                      <button
                        type="button"
                        onClick={() => { track("wizard.style", { style: "custom" }); onStyleChange("custom"); }}
                        className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border text-left transition-all duration-200 mt-2 ${
                          style === "custom" ? "border-transparent ring-1 ring-brand/50 bg-brand/[0.08]" : "border-white/[0.06] hover:border-white/[0.16]"
                        }`}
                      >
                        <span className="w-8 h-8 rounded-lg shrink-0 grid place-items-center text-[15px] bg-surface-3">🎨</span>
                        <span>
                          <span className={`block text-[12px] font-semibold ${style === "custom" ? "text-white" : "text-gray-200"}`}>{t("upload.style_custom") || "Personalizado"}</span>
                          <span className="block text-[10px] text-gray-500">{t("upload.style_custom_desc") || "Elegí tus colores (marca, artista…)"}</span>
                        </span>
                      </button>
                      {style === "custom" && onCustomColorsChange && (() => {
                        const parts = (customColors || "").split(",").map((x) => x.trim()).filter(Boolean);
                        const c1 = parts[0] && /^#[0-9a-fA-F]{6}$/.test(parts[0]) ? parts[0] : "#6D4AFF";
                        const c2 = parts[1] && /^#[0-9a-fA-F]{6}$/.test(parts[1]) ? parts[1] : "#14C8A8";
                        return (
                          <div className="mt-2 flex items-center gap-3 px-1">
                            <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer">
                              <input type="color" value={c1} onChange={(e) => onCustomColorsChange(`${e.target.value}, ${c2}`)} className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0" />
                            </label>
                            <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer">
                              <input type="color" value={c2} onChange={(e) => onCustomColorsChange(`${c1}, ${e.target.value}`)} className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0" />
                            </label>
                            <span className="text-[10px] text-gray-600">{t("upload.style_custom_hint") || "2 colores dominantes para el fondo"}</span>
                          </div>
                        );
                      })()}
                    </div>
                  )}
                </>
              )}
            </>
          )}

          {/* STEP 3 — Movimiento + metadata */}
          {wizardStep === 3 && _batchSettingsBlock}

          {/* STEP 4 — Tipografía & Animación: font/case/size/contrast + lyrics animation template (libass) */}
          {wizardStep === 4 && (
            <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-4">
              <style>{`
                @keyframes acard-pop { 0%{transform:scale(1.18);opacity:0} 55%{transform:scale(.95)} 80%{transform:scale(1.03)} 100%{transform:scale(1);opacity:1} }
                @keyframes acard-glow { 0%,100%{text-shadow:0 1px 6px rgba(0,0,0,.6)} 50%{text-shadow:0 0 13px rgba(20,200,168,.95),0 1px 6px rgba(0,0,0,.6)} }
                @keyframes acard-word { 0%,12%{opacity:0;transform:translateY(5px)} 34%,100%{opacity:1;transform:translateY(0)} }
                @keyframes acard-karaoke { 0%,8%{color:#7c7c8a} 42%,100%{color:#fff} }
                @keyframes tcard-slideup { 0%{transform:translateY(120%);opacity:0} 22%,88%{transform:translateY(0);opacity:1} 100%{transform:translateY(-120%);opacity:0} }
                @keyframes tcard-slideside { 0%{transform:translateX(-130%);opacity:0} 22%,88%{transform:translateX(0);opacity:1} 100%{transform:translateX(130%);opacity:0} }
                @keyframes tcard-wipe { 0%{clip-path:inset(0 100% 0 0)} 35%,100%{clip-path:inset(0 0 0 0)} }
                @keyframes tcard-blur { 0%{filter:blur(6px);opacity:0} 30%,80%{filter:blur(0);opacity:1} 100%{filter:blur(6px);opacity:0} }
              `}</style>

              {/* Tipografía — UI gap fix 2026-05-26. El refactor del paso 6
                  (commit 6c2e8a8) ocultó estos controles en LyricsEditor con
                  hideTypographyControls=true asumiendo que vivían en el paso
                  4, pero el bloque nunca llegó a migrarse. Sin esto el
                  operador no tiene forma de elegir font/case/size/contrast
                  desde el wizard. Los handlers (updateBatchDefault) y el
                  WizardLivePreview central ya escuchan estos cambios live —
                  solo faltaba el JSX. */}
              <div className="mb-4 pb-3 border-b border-white/[0.05]">
                <p className="text-[11px] text-gray-300 font-medium">{t("upload.typography_section") || "Tipografía"}</p>
                <p className="text-[10px] text-gray-600 mt-0.5 mb-3">
                  {t("upload.typography_desc") || "Estilo del texto sobre el video. El preview ← se actualiza al instante."}
                </p>
                <div className="space-y-3">
                  {/* Font */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0 w-20">{t("upload.font_label") || "Tipografía:"}</span>
                    <Listbox
                      value={batchDefaults.font}
                      onChange={(v) => updateBatchDefault("font", v)}
                      options={FONTS}
                      className="flex-1"
                      ariaLabel={t("upload.font_label") || "Tipografía"}
                    />
                  </div>

                  {/* Text case pill buttons: MAY / Aa / min / ori */}
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-gray-600 shrink-0 w-20">{t("upload.text_case_label") || "Texto:"}</span>
                      <span className="rounded-full bg-accent/[0.08] px-1.5 py-0.5 text-[8px] font-bold uppercase text-accent ring-1 ring-accent/20">
                        {t("whatsnew.new_badge") || "Nuevo"}
                      </span>
                      <HelpTip text={t("announce.typocase_tagline")} />
                      <div className="flex gap-1">
                        {TEXT_CASE_OPTS.map((opt) => (
                          <button
                            key={opt.code}
                            type="button"
                            title={opt.code === "sentence" ? `${opt.label} · ${t("announce.typocase_tagline")}` : opt.label}
                            onClick={() => updateBatchDefault("textCase", opt.code)}
                            onMouseEnter={() => setHoverCaseBatch(opt.code)}
                            onMouseLeave={() => setHoverCaseBatch(null)}
                            className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold transition-all
                              ${batchDefaults.textCase === opt.code
                                ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                                : "bg-surface-3/40 text-gray-500 hover:text-gray-300"
                              }`}
                          >{opt.d}</button>
                        ))}
                      </div>
                    </div>
                    {hoverCaseBatch && (
                      <div className="mt-1.5 ml-[5.5rem] px-3 py-1.5 rounded-md bg-black/40 ring-1 ring-white/[0.06] flex items-baseline gap-2 animate-fade-in">
                        <span className="text-[11px] font-mono text-white/80 tracking-wide">
                          {applyTextCase(t("upload.sample_lyric"), hoverCaseBatch)}
                        </span>
                        <span className="text-[10px] text-gray-600">← {t("upload.case_preview_help")}</span>
                      </div>
                    )}
                  </div>

                  {/* Font scale — A's en tamaños crecientes (ver fix
                      arriba en línea ~1264: 1.5 sumado para destrabar
                      backend max). */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0 w-20">{t("upload.font_scale_label") || "Tamaño:"}</span>
                    <div className="flex items-end gap-1">
                      {[
                        { code: "0.75", cls: "text-[9px]"  },
                        { code: "0.9",  cls: "text-[11px]" },
                        { code: "1.0",  cls: "text-[13px]" },
                        { code: "1.15", cls: "text-[16px]" },
                        { code: "1.3",  cls: "text-[19px]" },
                        { code: "1.5",  cls: "text-[22px]" },
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

                  {/* Text contrast pills */}
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-600 shrink-0 w-20">{t("upload.contrast_label") || "Contraste:"}</span>
                    <div className="flex gap-1">
                      {[
                        { code: "subtle", style: { WebkitTextStroke: "0px", textShadow: "none" },         label: t("upload.contrast_subtle") || "Suave" },
                        { code: "medium", style: { WebkitTextStroke: "0.5px black", textShadow: "0 0 3px rgba(0,0,0,0.8)" }, label: t("upload.contrast_medium") || "Medio" },
                        { code: "strong", style: { WebkitTextStroke: "1px black",   textShadow: "0 0 6px rgba(0,0,0,1), -1px -1px 0 #000, 1px 1px 0 #000" }, label: t("upload.contrast_strong") || "Fuerte" },
                      ].map((opt) => (
                        <button
                          key={opt.code}
                          type="button"
                          title={opt.label}
                          onClick={() => updateBatchDefault("textContrast", opt.code)}
                          className={`px-2 py-1 rounded-md text-[13px] font-bold text-white transition-all
                            ${batchDefaults.textContrast === opt.code
                              ? "bg-brand/20 ring-1 ring-brand/40"
                              : "bg-surface-3/40 hover:bg-surface-3/60"
                            }`}
                          style={{ ...opt.style, paintOrder: "stroke fill" }}
                        >A</button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              <p className="text-[11px] text-gray-300 font-medium">{t("upload.animation_section_full")}</p>
              <p className="text-[10px] text-gray-600 mt-0.5 mb-3">
                {t("upload.anim_gallery_desc") || "Cómo aparecen las palabras sobre el video. Pasá el mouse o elegí y miralo en el preview ←"}
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {LYRICS_ANIMATIONS.map((a) => {
                  const active = batchDefaults.lyricsAnimation === a.code;
                  return (
                    <button
                      key={a.code}
                      type="button"
                      onClick={() => updateBatchDefault("lyricsAnimation", a.code)}
                      onMouseEnter={() => setHoverAnimation(a.code)}
                      onMouseLeave={() => setHoverAnimation(null)}
                      aria-label={`${a.label}: ${a.desc}`}
                      title={a.desc}
                      className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                        active
                          ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                          : "border-white/[0.06] hover:border-white/[0.20]"
                      }`}
                    >
                      <div className="aspect-video relative overflow-hidden grid place-items-center" style={{ background: "radial-gradient(120% 100% at 50% 0,#1a1430,#0b0820)" }}>
                        {animDemo(a.code)}
                        {active && (
                          <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-brand grid place-items-center shadow">
                            <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                          </div>
                        )}
                      </div>
                      <div className="px-2.5 py-2 bg-surface-1">
                        <p className={`text-[12px] font-medium leading-tight ${active ? "text-white" : "text-gray-200"}`}>
                          {a.emoji ? `${a.emoji} ` : ""}{a.label}
                        </p>
                        <p className="text-[10px] text-gray-500 leading-snug mt-0.5">{a.desc}</p>
                      </div>
                    </button>
                  );
                })}
              </div>
              <p className="text-[10px] text-gray-600 mt-2">
                🎤 {t("upload.anim_word_note") || "Funcionan en toda canción — el tiempo por palabra se calcula automáticamente."}
              </p>

              {/* Lyric text color — color picker(s). El segundo solo aplica a
                  karaoke (color de la palabra cantada). Para none/pop/glow/
                  word_reveal alcanza con un solo color para todo el texto.

                  OCULTO en edición y variante (2026-07-25): `lyric_color` /
                  `lyric_sung_color` NO existen en computeFieldDiff, ni en
                  EditJobRequest, ni en _VARIANT_OVERRIDABLE_FIELDS — el valor
                  no sale del browser. Era un control editable-e-ignorado, y el
                  principio del #977 prohíbe exactamente eso. Además pintaba el
                  sticky de localStorage, así que mentía dos veces. Sigue
                  disponible en la CREACIÓN, donde /generate sí lo acepta.
                  Para cablearlo hacen falta 3 cambios (diff + EditJobRequest +
                  _VARIANT_OVERRIDABLE_FIELDS); /retry ya lo hereda. */}
              {!editMode && (
              <div className="mt-4 pt-3 border-t border-white/[0.05]">
                <p className="text-[11px] text-gray-300 font-medium">{t("upload.lyric_color_title") || "Color del texto"}</p>
                <p className="text-[10px] text-gray-600 mt-0.5 mb-2">
                  {t("upload.lyric_color_desc") || "Color de las letras sobre el video. Por defecto blanco."}
                </p>
                <div className="flex items-center gap-3 flex-wrap">
                  <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer">
                    <input
                      type="color"
                      value={batchDefaults.lyricColor || "#FFFFFF"}
                      onChange={(e) => updateBatchDefault("lyricColor", e.target.value)}
                      className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0"
                      aria-label={t("upload.lyric_color_label") || "Color del texto"}
                    />
                    <span>{batchDefaults.lyricsAnimation === "karaoke" ? (t("upload.lyric_color_unsung") || "No cantada") : (t("upload.lyric_color_label") || "Texto")}</span>
                  </label>
                  {batchDefaults.lyricsAnimation === "karaoke" && (
                    <label className="flex items-center gap-1.5 text-[11px] text-gray-400 cursor-pointer">
                      <input
                        type="color"
                        value={batchDefaults.lyricSungColor || "#FFFFFF"}
                        onChange={(e) => updateBatchDefault("lyricSungColor", e.target.value)}
                        className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0"
                        aria-label={t("upload.lyric_sung_color_label") || "Color palabra cantada"}
                      />
                      <span>{t("upload.lyric_color_sung") || "Cantada"}</span>
                    </label>
                  )}
                  {(batchDefaults.lyricColor !== "#FFFFFF" || batchDefaults.lyricSungColor !== "#FFFFFF") && (
                    <button
                      type="button"
                      onClick={() => { updateBatchDefault("lyricColor", "#FFFFFF"); updateBatchDefault("lyricSungColor", "#FFFFFF"); }}
                      className="text-[10px] text-gray-500 hover:text-white underline-offset-2 hover:underline transition-colors"
                    >
                      {t("upload.lyric_color_reset") || "Restablecer"}
                    </button>
                  )}
                </div>
              </div>
              )}

              {/* Transición entre líneas — eje aparte, compone con la animación */}
              <div className="mt-4 pt-3 border-t border-white/[0.05]">
                <p className="text-[11px] text-gray-300 font-medium">{t("upload.transition_title") || "Transición entre líneas"}</p>
                <p className="text-[10px] text-gray-600 mt-0.5 mb-3">
                  {t("upload.transition_desc") || "Cómo entra y sale cada línea. Se combina con la animación elegida."}
                </p>
                <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
                  {LINE_TRANSITIONS.map((tr) => {
                    const active = (batchDefaults.lineTransition || "none") === tr.code;
                    return (
                      <button
                        key={tr.code}
                        type="button"
                        onClick={() => updateBatchDefault("lineTransition", tr.code)}
                        onMouseEnter={() => setHoverTransition(tr.code)}
                        onMouseLeave={() => setHoverTransition(null)}
                        aria-label={`${tr.label}: ${tr.desc}`}
                        title={tr.desc}
                        className={`text-left rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer ${
                          active
                            ? "border-transparent ring-1 ring-brand/50 shadow-glow"
                            : "border-white/[0.06] hover:border-white/[0.20]"
                        }`}
                      >
                        <div className="aspect-video relative overflow-hidden grid place-items-center" style={{ background: "radial-gradient(120% 100% at 50% 0,#1a1430,#0b0820)" }}>
                          {transDemo(tr.code)}
                          {active && (
                            <div className="absolute top-1.5 right-1.5 w-4 h-4 rounded-full bg-brand grid place-items-center shadow">
                              <svg className="w-2.5 h-2.5 text-white" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                            </div>
                          )}
                        </div>
                        <div className="px-2 py-1.5 bg-surface-1">
                          <p className={`text-label leading-tight ${active ? "text-white" : "text-gray-200"}`}>{tr.label}</p>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Portada (intro title card) — Full Rotor v1.1: visual
                  template gallery + size with live percentage + per-element
                  fonts in a 2-column grid + manual song break.
                  Refactored 2026-05-30 to address the operator feedback
                  "los controles están confusos / pegados al footer". */}
              <div
                ref={portadaControlsRef}
                className={`mt-5 pt-4 border-t border-white/[0.06] rounded-lg transition-all duration-500 ${
                  portadaControlsPulse
                    ? "ring-2 ring-brand/60 bg-brand/[0.05] -mx-2 px-2 shadow-[0_0_0_4px_rgba(124,77,255,0.10)]"
                    : "ring-2 ring-transparent"
                }`}
              >
                <div className="flex items-center justify-between mb-3">
                  <p className="text-caption text-gray-200 font-medium">
                    {t("upload.titlecard_section") || "Portada del intro"}
                  </p>
                  <span className="text-[10px] text-gray-600">
                    {t("upload.titlecard_desc_short") || "Primeros 3-8 segundos del video"}
                  </span>
                </div>

                <div className="space-y-4">
                  {/* Layout template — visual gallery. Each preset shows a
                      mini 16:9 thumbnail with a stylised representation of
                      where the artist + title sit so the operator picks by
                      EYE, not by reading the label. */}
                  <div>
                    <p className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
                      {t("upload.titlecard_template_label") || "Disposición"}
                    </p>
                    <div className="grid grid-cols-4 gap-2">
                      {[
                        { code: "auto",        label: t("upload.titlecard_auto") || "Auto",
                          // Auto picks centered for long intros, badge for short — the
                          // thumbnail mirrors the "centered" default since that's the
                          // most common Genly look.
                          thumb: <div className="absolute inset-0 flex flex-col items-center justify-center gap-0.5">
                            <div className="w-[80%] h-[18%] rounded-sm bg-white/40" />
                            <div className="w-[55%] h-[14%] rounded-sm bg-white/25" />
                          </div>
                        },
                        { code: "centered",    label: t("upload.titlecard_centered") || "Centro",
                          thumb: <div className="absolute inset-0 flex flex-col items-center justify-center gap-0.5">
                            <div className="w-[80%] h-[20%] rounded-sm bg-white/60" />
                            <div className="w-[55%] h-[14%] rounded-sm bg-white/40" />
                          </div>
                        },
                        { code: "lower_third", label: t("upload.titlecard_lower_third") || "Tercio inf.",
                          thumb: <div className="absolute inset-0 flex flex-col items-center justify-end pb-[18%] gap-0.5">
                            <div className="w-[70%] h-[16%] rounded-sm bg-white/60" />
                            <div className="w-[48%] h-[12%] rounded-sm bg-white/40" />
                          </div>
                        },
                        { code: "badge",       label: t("upload.titlecard_badge") || "Badge",
                          thumb: <div className="absolute inset-0 flex flex-col items-start justify-end pl-[10%] pb-[14%] gap-0.5">
                            <div className="w-[42%] h-[14%] rounded-sm bg-white/60" />
                            <div className="w-[32%] h-[10%] rounded-sm bg-white/40" />
                          </div>
                        },
                      ].map((opt) => {
                        const active = (batchDefaults.titleTemplate || "auto") === opt.code;
                        return (
                          <button
                            key={opt.code}
                            type="button"
                            onClick={() => updateBatchDefault("titleTemplate", opt.code)}
                            className={`group flex flex-col items-center gap-1 transition-all`}
                            title={opt.label}
                          >
                            <div className={`relative w-full aspect-video rounded-md overflow-hidden ring-1 transition-all
                              ${active
                                ? "ring-brand bg-brand/[0.08] shadow-[0_0_0_3px_rgba(124,77,255,0.12)]"
                                : "ring-white/[0.08] bg-surface-1 hover:ring-white/[0.18] hover:bg-surface-2"
                              }`}
                            >
                              {/* Subtle gradient background to read like a real video frame */}
                              <div className="absolute inset-0 bg-gradient-to-br from-purple-900/30 via-purple-950/40 to-black/60" />
                              {opt.thumb}
                            </div>
                            <span className={`text-[10px] truncate w-full text-center transition-colors
                              ${active ? "text-brand" : "text-gray-500 group-hover:text-gray-300"}`}>
                              {opt.label}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                    {/* Nudge de descubribilidad (incidente Clari 19-jul, "no
                        encuentro dónde modificarlo"): cuando "Auto" va a
                        resolver al badge chico porque el tema canta enseguida,
                        el operador NO entiende por qué su título salió chico.
                        Le nombramos el motivo y el camino al título grande —
                        el override "Centro" YA existe y llega al render. */}
                    {(batchDefaults.titleTemplate || "auto") === "auto" &&
                      typeof _firstLyricStart === "number" &&
                      _firstLyricStart <= AUTO_INTRO_THRESHOLD_S && (
                      <div className="mt-2 flex items-start gap-2 rounded-lg bg-amber-500/[0.07] ring-1 ring-amber-500/25 px-3 py-2">
                        <svg className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                          <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" strokeLinecap="round" />
                        </svg>
                        <p className="text-[11px] text-amber-200/90 leading-snug flex-1">
                          {t("upload.titlecard_auto_badge_hint") ||
                            "Este tema canta enseguida, así que “Auto” usa el título compacto para no pisar la letra."}{" "}
                          <button
                            type="button"
                            onClick={() => updateBatchDefault("titleTemplate", "centered")}
                            className="text-amber-100 underline decoration-amber-400/40 hover:decoration-amber-200 font-medium"
                          >
                            {t("upload.titlecard_use_centered") || "Usar el título grande (Centro)"}
                          </button>
                        </p>
                      </div>
                    )}
                  </div>

                  {/* Size — quick presets show their relative scale via the
                      letter size, plus a slider for fine control. The
                      percentage on the right gives an explicit numeric
                      anchor (1.00× = default, 1.25× = +25 %, etc.). */}
                  <div>
                    <p className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
                      {t("upload.titlecard_size_label_short") || "Tamaño del texto"}
                    </p>
                    <div className="flex items-center gap-3">
                      <div className="flex items-end gap-1 shrink-0">
                        {[
                          { code: "0.75", cls: "text-[10px]" },
                          { code: "1.0",  cls: "text-[13px]" },
                          { code: "1.25", cls: "text-[16px]" },
                          { code: "1.5",  cls: "text-[19px]" },
                        ].map((opt) => {
                          const active = (batchDefaults.titleSize || "1.0") === opt.code;
                          return (
                            <button
                              key={opt.code}
                              type="button"
                              onClick={() => updateBatchDefault("titleSize", opt.code)}
                              className={`w-8 h-8 flex items-center justify-center rounded-md font-bold transition-all ${opt.cls}
                                ${active
                                  ? "bg-brand/20 text-brand ring-1 ring-brand/40"
                                  : "bg-surface-2/60 text-gray-500 hover:text-gray-300 hover:bg-surface-2"
                                }`}
                              aria-label={`${opt.code}×`}
                            >A</button>
                          );
                        })}
                      </div>
                      <input
                        type="range" min="0.5" max="2" step="0.05"
                        value={parseFloat(batchDefaults.titleSize) || 1}
                        onChange={(e) => updateBatchDefault("titleSize", e.target.value)}
                        className="flex-1 accent-brand"
                        aria-label={t("upload.titlecard_size_label") || "Tamaño del título"}
                      />
                      <span className="text-[11px] text-gray-400 w-12 text-right tabular-nums font-medium">
                        {(parseFloat(batchDefaults.titleSize) || 1).toFixed(2)}×
                      </span>
                    </div>
                  </div>

                  {/* Per-element fonts: artist + song side by side, 2-col
                      grid. The Listbox below opens upward when it sits
                      against the sticky footer (UI v1.1 flip-up). */}
                  <div>
                    <p className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
                      {t("upload.titlecard_fonts_label") || "Tipografías"}
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                      <div className="flex flex-col gap-1">
                        <span className="text-[10px] text-gray-500">
                          {t("upload.titlecard_artist_font_short") || "Artista"}
                        </span>
                        <Listbox
                          value={batchDefaults.titleArtistFont}
                          onChange={(v) => updateBatchDefault("titleArtistFont", v)}
                          options={FONTS}
                          ariaLabel={t("upload.titlecard_artist_font") || "Font artista"}
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <span className="text-[10px] text-gray-500">
                          {t("upload.titlecard_song_font_short") || "Canción"}
                        </span>
                        <Listbox
                          value={batchDefaults.titleSongFont}
                          onChange={(v) => updateBatchDefault("titleSongFont", v)}
                          options={FONTS}
                          ariaLabel={t("upload.titlecard_song_font") || "Font canción"}
                        />
                      </div>
                    </div>
                  </div>

                  {/* UI v1.1 (2026-05-30): manual song-title line break.
                      Off (default) => backend auto-shrink-then-wraps the song
                      title (historical behaviour, no change). On => the
                      operator types each line separately and the render
                      respects that exact break. Persists via render_params.
                      title_song_break = "line1\nline2". */}
                  <div className="pt-2 border-t border-white/[0.04]">
                    {(() => {
                      // Parse the current value. Default "" means "auto",
                      // shown as toggle OFF.
                      const raw = batchDefaults.titleSongBreak || "";
                      const enabled = raw.includes("\n");
                      const parts = raw.split("\n");
                      const line1 = enabled ? (parts[0] || "") : "";
                      const line2 = enabled ? (parts[1] || "") : "";
                      const toggleOn = () => {
                        // When the operator turns it on, seed both lines
                        // with empty strings — they can type or paste in.
                        // We keep "\n" present so .includes("\n") stays
                        // true even when both are empty.
                        updateBatchDefault("titleSongBreak", "\n");
                      };
                      const toggleOff = () => {
                        updateBatchDefault("titleSongBreak", "");
                      };
                      const setLine = (idx, val) => {
                        const next = [line1, line2];
                        next[idx] = val.replace(/\n/g, " ");  // never two breaks
                        updateBatchDefault(
                          "titleSongBreak",
                          `${next[0]}\n${next[1]}`,
                        );
                      };
                      return (
                        <>
                          <label className="flex items-center gap-2.5 cursor-pointer select-none">
                            {/* Custom switch — matches the design system used
                                elsewhere in the wizard (bg_enhance, etc.) so
                                the native checkbox doesn't clash with the
                                dark surface. */}
                            <input
                              type="checkbox"
                              checked={enabled}
                              onChange={(e) => (e.target.checked ? toggleOn() : toggleOff())}
                              className="peer sr-only"
                            />
                            <div className="relative w-9 h-5 rounded-full bg-surface-3 peer-checked:bg-brand transition-colors duration-200 shrink-0">
                              <div className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 peer-checked:translate-x-4" />
                            </div>
                            <span className="text-[11px] text-gray-300 font-medium flex-1">
                              {t("upload.titlecard_break_label") || "Partir título en 2 líneas"}
                            </span>
                            <HelpTip
                              text={
                                t("upload.titlecard_break_help") ||
                                "Si tu canción tiene un título largo y querés decidir vos dónde se parte (ej. 'Donde Estan' / 'Corazón'), activá esto y escribí cada línea. Si lo dejás apagado, el sistema decide automáticamente cuando no entra."
                              }
                            />
                          </label>
                          {enabled && (
                            <div className="mt-2.5 space-y-2 pl-[44px]">
                              {/* The pl-[44px] aligns the input column with
                                  the text label of the switch (switch w-9
                                  + gap 2.5 = ~44 px). Keeps the eyes on a
                                  single vertical axis. */}
                              <div className="flex items-center gap-2">
                                <span className="text-[10px] text-gray-500 shrink-0 w-12 text-right">
                                  {t("upload.titlecard_break_line1") || "Línea 1"}
                                </span>
                                <input
                                  type="text"
                                  value={line1}
                                  onChange={(e) => setLine(0, e.target.value)}
                                  className="flex-1 px-2.5 py-1.5 rounded-md bg-surface-1 border border-white/[0.08] text-[12px] text-white placeholder:text-gray-600 focus:border-brand/50 focus:outline-none transition-colors"
                                  placeholder={t("upload.titlecard_break_line1_ph") || "Primera línea"}
                                  maxLength={140}
                                />
                              </div>
                              <div className="flex items-center gap-2">
                                <span className="text-[10px] text-gray-500 shrink-0 w-12 text-right">
                                  {t("upload.titlecard_break_line2") || "Línea 2"}
                                </span>
                                <input
                                  type="text"
                                  value={line2}
                                  onChange={(e) => setLine(1, e.target.value)}
                                  className="flex-1 px-2.5 py-1.5 rounded-md bg-surface-1 border border-white/[0.08] text-[12px] text-white placeholder:text-gray-600 focus:border-brand/50 focus:outline-none transition-colors"
                                  placeholder={t("upload.titlecard_break_line2_ph") || "Segunda línea"}
                                  maxLength={140}
                                />
                              </div>
                            </div>
                          )}
                        </>
                      );
                    })()}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* STEP 5 — Entregá: delivery + final recap */}
          {wizardStep === 5 && (
            <>
              {user?.features?.prores_export && _deliveryBlock}
              <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
                <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500 mb-1">{t("upload.step_deliver") || "Entregá"}</p>
                <p className="text-[13px] text-gray-200">{summary}</p>
                <p className="text-[11px] text-gray-600 mt-1">{t("upload.deliver_hint") || "Revisá los lyrics para ajustar el tiempo, o generá directo."}</p>
              </div>
            </>
          )}

          {/* STEP 6 — Lyrics (Phase 2 2026-05-25): render prop de App.jsx
              con el contenido completo de review (transcribing / LyricsEditor /
              readyToGenerate / empty / error). El stepper + WizardLivePreview
              centrales persisten — el operador sigue viendo el preview con
              los settings del paso 4 mientras edita las lyrics. */}
          {wizardStep === 6 && (
            <div className="min-w-0 w-full">
              {renderStep6 ? renderStep6() : (
                <div className="text-center py-12 text-sm text-gray-500">
                  {t("upload.step_lyrics_hint") || "Disponible después de \"Revisar lyrics\""}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
        );
      })()
      )}

      {/* Sticky bottom CTA bar. Phase 2: oculta en paso 6 porque el contenido
          de review (LyricsEditor / transcribing / readyToGenerate) trae sus
          propios CTAs (Aprobar / Volver / Crear N videos).
          QA fix 2026-05-28: el gate `files.length > 0` ocultaba la barra en
          TODOS los pasos del edit-wizard (files=[] en edit mode), dejando
          al operador sin navegación entre pasos. Ahora también la mostramos
          en edit mode — los botones Atrás/Continuar usan el `_findPrev/
          NextUnlocked` que ya respeta lockedSteps, así que automáticamente
          saltea step 1 (file, locked) y step 5 (delivery, locked). El
          summary line muestra "MP4 1080p · Generar con IA" o similar (sin
          file count) lo cual es informativo aunque mínimo en edit mode. */}
      {(files.length > 0 || editMode) && wizardStep !== 6 && (
        <div
          className={`wizard-command-bar fixed bottom-0 left-0 right-0 z-30 px-4 md:px-8 py-4 transition-all duration-300 ${sidebarOpen ? "md:left-60" : "md:left-[72px]"}`}
          data-tour="upload-cta-bar"
        >
          <div className="w-full flex flex-wrap items-center gap-3">
            <div className="flex-1 min-w-0">
              <p className="text-[10px] uppercase tracking-[0.18em] text-gray-500">
                {t("upload.step")} {wizardStep}/{WIZARD_STEPS.length} · {WIZARD_STEPS[wizardStep - 1]?.label}
              </p>
              <p className="text-sm text-white truncate mt-0.5">{summary}</p>
              {!allHaveArtist && (
                <p className="text-[11px] text-amber-400/80 mt-0.5">
                  {t("upload.complete_artist") || "Completá el nombre del artista en cada archivo"}
                </p>
              )}
            </div>

            {(() => {
              // Locked-aware "Atrás": en modo post-render edit (lockedSteps
              // no vacío) los pasos anteriores pueden estar todos lockeados
              // → escondé el botón en vez de mandar al operador a un paso
              // que no puede usar.
              const prev = _findPrevUnlocked(wizardStep);
              if (prev == null) return null;
              return (
                <button
                  onClick={() => goStep(prev)}
                  className="btn-secondary text-xs h-11 px-4"
                >
                  {t("upload.back") || "Atrás"}
                </button>
              );
            })()}

            {/* Phase 2 (2026-05-25): cuando el operador ya está en review
                (hasReviewableContent=true) y vuelve a un paso anterior para
                ajustar font/animation/movement, el CTA principal cambia a
                "Volver a lyrics" para que pueda regresar al editor con 1
                click, sin re-disparar onStartReview. */}
            {hasReviewableContent && wizardStep < 6 ? (
              <button
                onClick={() => goStep(6)}
                className="btn-primary h-11 px-6"
              >
                {t("upload.back_to_lyrics") || "Volver a lyrics"}
                <svg className="inline-block ml-1.5 w-4 h-4 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M5 12h14M12 5l7 7-7 7" />
                </svg>
              </button>
            ) : wizardStep < 5 ? (
              (() => {
                // Locked-aware "Continuar". Si el próximo paso está
                // lockeado, saltea al siguiente navegable. Si no hay más
                // pasos navegables, no mostramos botón.
                const next = _findNextUnlocked(wizardStep);
                if (next == null || next > 5) return null;
                return (
                  <button
                    onClick={() => goStep(next)}
                    disabled={wizardStep === 1 && !allHaveArtist}
                    className="btn-primary h-11 px-6 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {t("upload.continue") || "Continuar"}
                    <svg className="inline-block ml-1.5 w-4 h-4 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M5 12h14M12 5l7 7-7 7" />
                    </svg>
                  </button>
                );
              })()
            ) : wizardStep === 5 ? (
              artTrack ? (
                // Art track: un solo CTA, genera directo (sin editor de letra).
                <button
                  onClick={() => { track("wizard.generate", { mode: "art_track", batch_size: (files || []).length }); onGenerateArtTrack?.(); }}
                  disabled={!allHaveArtist || !backgroundFile}
                  className="btn-primary h-11 px-6 disabled:opacity-40 disabled:cursor-not-allowed"
                  title={!backgroundFile ? (t("arttrack.cover_missing_desc") || "Subí el cover en el paso “Modo”") : undefined}
                >
                  {t("upload.generate_art_track") || "Generar Art Track"}
                  <svg className="inline-block ml-1.5 w-4 h-4 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
              ) : (
              <>
                {onGenerateDirect && (
                  <button
                    onClick={() => { track("wizard.generate", { mode: "direct", batch_size: (files || []).length }); onGenerateDirect(); }}
                    disabled={!allHaveArtist}
                    className="btn-secondary text-xs h-11 px-4 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {t("upload.generate_direct") || "Generar directo"}
                  </button>
                )}
                {onStartReview && (
                  <button
                    onClick={() => { track("wizard.start_review", {}); onStartReview(); }}
                    disabled={!allHaveArtist}
                    className="btn-primary h-11 px-6 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {t("upload.review_lyrics") || "Revisar lyrics"}
                    <svg className="inline-block ml-1.5 w-4 h-4 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M5 12h14M12 5l7 7-7 7" />
                    </svg>
                  </button>
                )}
              </>
              )
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
