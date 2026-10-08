// "Looks" de letra: tratamientos tipográficos completos en un clic (fuente +
// colores + composición + movimiento + fondo + grade). Los arma el backend
// con libass (backend/lyric_looks.py `LOOKS`); este módulo es el ÚNICO espejo
// del catálogo en el frontend, así el wizard, el editor, el preview, la ficha
// del video y los payloads no pueden nombrar ni validar distinto.
//
// Contrato con el backend:
//   - campo `lyric_look`: "" (sin look, default) | uno de LYRIC_LOOK_CODES.
//     Un código desconocido el backend lo baja a "".
//   - Con un look activo el render IGNORA font, lyrics_animation y
//     line_transition (los define el look) y SIGUE respetando font_scale,
//     text_case, lyric_color (pisa el color del look), effect (si no está
//     vacío reemplaza el overlay del look) y la portada.
//
// Los números de `preview` espejan los del backend (font_scale, key_scale,
// row_width, stagger, tilt, colores) para que el preview CSS se parezca al
// render. No es pixel-perfect: alcanza con que comunique el look.

/** Códigos válidos, en el orden en que se muestran. "" = sin look. */
export const LYRIC_LOOK_CODES = ["", "cosmico", "cine", "pincel", "pop70", "pelicula"];

/** Campos del wizard que un look define (y que el render ignora con look). */
export const LOOK_LOCKED_FIELDS = ["font", "lyricsAnimation", "lineTransition"];

// Paleta de tarjetas de Pop 70s: el fondo se REEMPLAZA por placas planas que
// cortan en cada línea; la palabra clave cambia de color para contrastar con
// la placa (backend Look.flat_colors / flat_accents).
const POP70_CARDS = ["#EE5FA0", "#6E8EDB", "#8FCB6B", "#F4ECDD"];
const POP70_CARD_ACCENTS = ["#FFD23F", "#FFD23F", "#EE5FA0", "#6E8EDB"];

const LOOKS = [
  {
    code: "",
    font: null,
    preview: null,
  },
  {
    code: "cosmico",
    font: { css: "'Audiowide', sans-serif", weight: 400 },
    preview: {
      layout: "line",
      motion: "zoom_through",
      color: "#FFFFFF",
      fontScale: 1.25,
      textShadow: "0 0.16cqw 0.3cqw rgba(0,0,0,.55)",
      stroke: "0.08cqw rgba(0,0,0,.6)",
      grade: "contrast(1.08) saturate(1.1)",
      thumbBg: "radial-gradient(120% 100% at 50% 40%,#1b1446 0%,#090622 55%,#020108 100%)",
    },
  },
  {
    code: "cine",
    font: { css: "'Marcellus', serif", weight: 400 },
    preview: {
      layout: "keyword",
      motion: "cine",
      color: "#F6EEE2",
      fontScale: 1.0,
      keyScale: 1.55,
      leadScale: 0.52,
      textShadow: "0 0 0.5cqw rgba(0,0,0,.7), 0 0 1.3cqw rgba(0,0,0,.45)",
      stroke: "0px",
      letterbox: true,
      vignette: true,
      grade: "contrast(1.06) saturate(.82) sepia(.14)",
      thumbBg: "radial-gradient(120% 100% at 50% 30%,#4a3624 0%,#1d140c 60%,#070504 100%)",
    },
  },
  {
    code: "pincel",
    font: { css: "'Knewave', cursive", weight: 400 },
    preview: {
      layout: "build",
      motion: "word_pop",
      color: "#F4F0E8",
      accent: "#E3262F",
      fontScale: 1.2,
      keyScale: 1.45,
      rowWidth: 0.62,
      tilt: 5,
      // Ancho medio de glifo / tamaño de fuente: sólo para partir las filas.
      glyphWidth: 0.62,
      textShadow: "0.2cqw 0.22cqw 0 rgba(0,0,0,.6)",
      stroke: "0.12cqw #000",
      grain: true,
      vignette: true,
      grade: "contrast(1.16) saturate(.35) brightness(1.06)",
      thumbBg: "radial-gradient(120% 100% at 50% 30%,#4b4844 0%,#22201d 60%,#0b0a09 100%)",
    },
  },
  {
    code: "pop70",
    font: { css: "'Shrikhand', cursive", weight: 400 },
    preview: {
      layout: "build",
      motion: "word_pop",
      color: "#FFF3DC",
      accent: "#FFD23F",
      fontScale: 1.5,
      keyScale: 1.5,
      rowWidth: 0.55,
      stagger: 0.07,
      glyphWidth: 0.7,
      textShadow: "0.34cqw 0.34cqw 0 #3A1D2E",
      stroke: "0.24cqw #3A1D2E",
      cards: POP70_CARDS,
      cardAccents: POP70_CARD_ACCENTS,
      thumbBg: POP70_CARDS[0],
    },
  },
  {
    code: "pelicula",
    font: { css: "'Oswald', sans-serif", weight: 700 },
    preview: {
      layout: "line",
      motion: "fade",
      color: "#F2C230",
      fontScale: 0.85,
      textShadow: "0 0.1cqw 0.25cqw rgba(0,0,0,.45)",
      stroke: "0px",
      grain: true,
      vignette: true,
      // Película gastada con tinte teal: negros levantados + poca saturación.
      grade: "saturate(.72) contrast(.9) brightness(1.04)",
      tint: "#1d6a73",
      thumbBg: "radial-gradient(120% 100% at 50% 30%,#3d6d6c 0%,#1d3a3e 60%,#0c1a1c 100%)",
    },
  },
];

const BY_CODE = Object.fromEntries(LOOKS.map((l) => [l.code, l]));

/** Normaliza cualquier valor a un código válido ("" si no se reconoce). */
export function normalizeLyricLook(value) {
  const code = String(value == null ? "" : value).trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(BY_CODE, code) ? code : "";
}

/** Datos del look (sin labels), o null para "sin look"/desconocido. */
export function getLyricLook(value) {
  const code = normalizeLyricLook(value);
  return code ? BY_CODE[code] : null;
}

/** ¿El look define (y por lo tanto bloquea) este campo del wizard? */
export function isLockedByLook(lyricLook, field) {
  return !!getLyricLook(lyricLook) && LOOK_LOCKED_FIELDS.includes(field);
}

/** code → nombre visible. Keys estáticas: el check de i18n las verifica. */
export const LYRIC_LOOK_LABELS = (t) => ({
  "": t("upload.look_none") || "Sin look",
  cosmico: t("upload.look_cosmico") || "Cósmico",
  cine: t("upload.look_cine") || "Cine",
  pincel: t("upload.look_pincel") || "Pincel",
  pop70: t("upload.look_pop70") || "Pop 70s",
  pelicula: t("upload.look_pelicula") || "Película",
});

const LYRIC_LOOK_DESCS = (t) => ({
  "": t("upload.look_none_desc") || "Elegís tipografía, animación y transición a mano.",
  cosmico: t("upload.look_cosmico_desc") || "La letra viaja hacia la cámara y pasa de largo.",
  cine: t("upload.look_cine_desc") || "Serif elegante, palabra clave grande, franjas de cine.",
  pincel: t("upload.look_pincel_desc") || "Trazo de pincel, palabra por palabra, clave en rojo.",
  pop70: t("upload.look_pop70_desc") || "No usa el fondo: tarjetas de color que cambian en cada línea.",
  pelicula: t("upload.look_pelicula_desc") || "Película gastada con tinte verdoso y letra amarilla chica.",
});

/** Opciones listas para pickers: [{ code, label, desc, font, preview }]. */
export function lyricLookOptions(t) {
  const labels = LYRIC_LOOK_LABELS(t);
  const descs = LYRIC_LOOK_DESCS(t);
  return LOOKS.map((l) => ({
    ...l,
    label: labels[l.code],
    desc: descs[l.code],
    // Forma que espera <Listbox>: muestra cada opción en su tipografía.
    css: l.font ? l.font.css : undefined,
    weight: l.font ? l.font.weight : undefined,
  }));
}

export function lyricLookLabel(t, value) {
  return LYRIC_LOOK_LABELS(t)[normalizeLyricLook(value)];
}

/**
 * Nota de "esto lo define el look". Interpola {look} a mano (no vía
 * t(key, vars)) para funcionar igual con el patrón `t(key) || fallback`.
 */
export function lookLockedNote(t, value) {
  const template = t("upload.look_locked_note")
    || "Definido por el look «{look}». Elegí «Sin look» para personalizarlo.";
  const label = lyricLookLabel(t, value) || "";
  return String(template).replace(/\{look\}/g, () => label);
}

// ── Palabra clave ──────────────────────────────────────────────────────────
// Espejo de backend lyric_looks.pick_keyword: la ÚLTIMA palabra "de
// contenido" (≥4 caracteres alfanuméricos y no stopword). Las líneas sin
// palabra de contenido no tienen énfasis.
const STOPWORDS = new Set(`
a al algo ante como con contra cual cuando de del desde donde e el ella ellas
ellos en entre era es esa ese eso esta este esto fue ha hay la las le les lo
los me mi mis muy nada ni no nos o os para pero por porque que qué se ser si
sin sobre son su sus tan te ti tu tú tus un una uno unos y ya yo él más mas
the and or but a an of to in on at for with from by is are was were be been
i you he she it we they me my your our their his her its this that these
those so as if not no do does did just like oh ooh yeah hey la na da
`.split(/\s+/).filter(Boolean));

function normToken(tok) {
  return String(tok || "").normalize("NFC").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
}

export function pickKeyword(tokens) {
  for (let i = tokens.length - 1; i >= 0; i -= 1) {
    const n = normToken(tokens[i]);
    if (n.length >= 4 && !STOPWORDS.has(n)) return i;
  }
  return null;
}

/**
 * Composición "palabra clave" (Cine): línea chica arriba + palabra grande.
 * Devuelve { lead, key } (lead puede ser "") o null si la línea va en una
 * sola fila a tamaño base (no hay palabra clave AL FINAL). Las líneas de ≤2
 * palabras van enteras en grande.
 */
export function keywordSplit(tokens) {
  if (!tokens.length) return null;
  if (tokens.length <= 2) return { lead: "", key: tokens.join(" ") };
  const k = pickKeyword(tokens);
  if (k == null || k !== tokens.length - 1) return null;
  return { lead: tokens.slice(0, k).join(" "), key: tokens[k] };
}

/**
 * Filas de la composición "build" (Pincel / Pop 70s), aproximadas sin medir
 * la fuente: ancho de palabra ≈ caracteres × tamaño × glyphWidth. Igual que
 * el backend, la palabra clave va en su propia fila si la línea es larga.
 * Devuelve [[índices de token], ...].
 */
export function buildRows(tokens, { baseFontPx, keyScale = 1, rowWidth = 0.6, glyphWidth = 0.62, frameWidth = 1920 } = {}) {
  const k = pickKeyword(tokens);
  const size = (i) => baseFontPx * (i === k ? keyScale : 1);
  const width = (i) => tokens[i].length * size(i) * glyphWidth;
  const space = baseFontPx * 0.35;
  const maxRow = frameWidth * rowWidth;
  const rows = [];
  let cur = [];
  let curW = 0;
  tokens.forEach((_tok, i) => {
    const w = width(i);
    const solo = i === k && tokens.length > 2;
    const afterKey = k != null && cur.length && cur[cur.length - 1] === k && tokens.length > 2;
    if (cur.length && (solo || afterKey || curW + space + w > maxRow)) {
      rows.push(cur);
      cur = [];
      curW = 0;
    }
    cur.push(i);
    curW += (cur.length > 1 ? space : 0) + w;
  });
  if (cur.length) rows.push(cur);
  return { rows, keyIndex: k };
}

/**
 * Color de letra a mandar en /generate. Con un look activo, el blanco por
 * defecto (#FFFFFF) significa "no elegí color" → se manda "" para que el
 * backend use el color del look; cualquier otro color lo pisa a propósito.
 * Sin look se conserva el contrato histórico (#FFFFFF por defecto).
 */
export function lyricColorForSubmit(lyricLook, lyricColor) {
  const color = lyricColor || "#FFFFFF";
  if (getLyricLook(lyricLook) && color.toUpperCase() === "#FFFFFF") return "";
  return color;
}

export default LOOKS;
