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
export const LYRIC_LOOK_CODES = [
  "", "cosmico", "cine", "pincel", "pop70", "pelicula",
  "cinetico", "neon", "chat", "cuaderno", "bloque", "arco", "perspectiva",
  "duotono", "romantico", "degrade", "y2k",
];

/** Campos del wizard que un look define (y que el render ignora con look). */
export const LOOK_LOCKED_FIELDS = ["font", "lyricsAnimation", "lineTransition"];

// Paleta de tarjetas de Pop 70s: el fondo se REEMPLAZA por placas planas que
// cortan en cada línea; la palabra clave cambia de color para contrastar con
// la placa (backend Look.flat_colors / flat_accents).
const POP70_CARDS = ["#EE5FA0", "#6E8EDB", "#8FCB6B", "#F4ECDD"];
const POP70_CARD_ACCENTS = ["#FFD23F", "#FFD23F", "#EE5FA0", "#6E8EDB"];

// Cinético: tarjetas planas navy / celeste, cada una con su paleta
// (tinta, alternativa, acento) — backend Look.card_palettes.
const CINETICO_CARDS = ["#1F2244", "#86C8EE"];
const CINETICO_PALETTES = [["#FFFFFF", "#6EC6FF", "#FF7A45"], ["#1F2244", "#FFFFFF", "#E63946"]];
// Colores de tubo de Neón y de tinta de Duotono, uno por línea.
const NEON_TUBES = ["#FF3EA5", "#2EE6FF", "#B07BFF", "#FFB13B"];
const DUOTONO_INKS = ["#FFF7E6", "#FFD23F", "#FF8FB1", "#9EF0C8"];
const DEGRADE_SKY = ["#9D4EDD", "#C850C0", "#FF4F8B"];

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
    // Pinta todo el cuadro: en un job nuevo el backend no genera fondo IA.
    ownsBackground: true,
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
  // ── Looks 2 (backend lyric_looks.py, mismo orden) ──────────────────────
  {
    code: "cinetico",
    // Pinta todo el cuadro: en un job nuevo el backend no genera fondo IA.
    ownsBackground: true,
    font: { css: "'Big Shoulders Display', sans-serif", weight: 900 },
    preview: {
      layout: "kinetic",
      motion: "word_pop",
      exit: "smear",
      color: "#FFFFFF",
      fontScale: 1.0,
      // Palabras función (stopwords / ≤2 letras) en manuscrita, chicas.
      scriptFont: { css: "'Caveat', cursive", weight: 700 },
      textShadow: "0 0.2cqw 0 rgba(11,13,34,.25)",
      stroke: "0px",
      grain: true,
      cards: CINETICO_CARDS,
      cardAccents: CINETICO_PALETTES.map((p) => p[2]),
      cardPalettes: CINETICO_PALETTES,
      thumbBg: CINETICO_CARDS[0],
    },
  },
  {
    code: "neon",
    font: { css: "'Tilt Neon', sans-serif", weight: 400 },
    preview: {
      layout: "neon",
      motion: "neon",
      color: "#FFF4FA",
      fontScale: 1.9,
      // Líneas impares: tubo en manuscrita.
      scriptFont: { css: "'Neonderthaw', cursive", weight: 400 },
      lineColors: NEON_TUBES,
      textShadow: "none",
      stroke: "0px",
      vignette: true,
      grade: "brightness(.55) saturate(.7) contrast(1.08)",
      thumbBg: "radial-gradient(120% 100% at 50% 40%,#2a1830 0%,#120a18 60%,#050307 100%)",
    },
  },
  {
    code: "chat",
    font: { css: "'Roboto', sans-serif", weight: 700 },
    preview: {
      layout: "chat",
      motion: "fade",
      forceCase: "original",
      color: "#111111",
      accent: "#FFFFFF",
      fontScale: 0.78,
      bubbles: ["#E9E9EB", "#1F8BFF"],
      textShadow: "none",
      stroke: "0px",
      grade: "blur(6px) brightness(.84) saturate(.85)",
      stageScale: 1.08,
      thumbBg: "linear-gradient(160deg,#6f7c8f 0%,#3d4656 60%,#262c36 100%)",
    },
  },
  {
    code: "cuaderno",
    font: { css: "'Caveat', cursive", weight: 700 },
    preview: {
      layout: "build",
      motion: "word_pop",
      wordMotion: "write",
      forceCase: "original",
      color: "#FFFFFF",
      accent: "#FFFFFF",
      fontScale: 1.75,
      keyScale: 1.35,
      rowWidth: 0.62,
      glyphWidth: 0.42,
      wordTilt: 3,
      circleKey: "#E8322E",
      doodles: { heart: "#FF5DA2", star: "#FFD23F" },
      textShadow: "0 0.16cqw 0.2cqw rgba(0,0,0,.45)",
      stroke: "0.08cqw rgba(0,0,0,.55)",
      grade: "contrast(1.04) saturate(.9)",
      thumbBg: "radial-gradient(120% 100% at 50% 30%,#8a6a52 0%,#4b3528 60%,#1e140e 100%)",
    },
  },
  {
    code: "bloque",
    font: { css: "'Big Shoulders Display', sans-serif", weight: 900 },
    preview: {
      layout: "block",
      motion: "word_pop",
      color: "#FFFFFF",
      accent: "#FF6B5B",
      fontScale: 1.0,
      rowWidth: 0.46,
      lightWeight: 300,
      textShadow: "0 0.2cqw 0.2cqw rgba(0,0,0,.45)",
      stroke: "0px",
      grade: "brightness(.92) contrast(1.06) saturate(.9)",
      thumbBg: "radial-gradient(120% 100% at 50% 30%,#3b4a5a 0%,#1d2631 60%,#0b0f14 100%)",
    },
  },
  {
    code: "arco",
    font: { css: "'Montserrat', sans-serif", weight: 800 },
    preview: {
      layout: "arc",
      motion: "fade",
      color: "#FFFFFF",
      accent: "#FFD23F",
      fontScale: 0.85,
      keyScale: 2.1,
      glyphWidth: 0.72,
      textShadow: "0 0.16cqw 0.2cqw rgba(0,0,0,.45)",
      stroke: "0.1cqw rgba(0,0,0,.7)",
      vignette: true,
      grade: "contrast(1.08) saturate(.85)",
      thumbBg: "radial-gradient(120% 100% at 50% 50%,#2c3a52 0%,#141c2b 60%,#06080d 100%)",
    },
  },
  {
    code: "perspectiva",
    font: { css: "'Montserrat', sans-serif", weight: 800 },
    preview: {
      layout: "floor",
      motion: "fade",
      color: "#FFFFFF",
      accent: "#7CF5D6",
      fontScale: 1.5,
      textShadow: "none",
      stroke: "0.42cqw #0B4F47",
      grade: "contrast(1.06) saturate(.9)",
      thumbBg: "linear-gradient(180deg,#9fc4d6 0%,#5f8fa6 48%,#2c4a3f 52%,#16261f 100%)",
    },
  },
  {
    code: "duotono",
    font: { css: "'Permanent Marker', cursive", weight: 400 },
    preview: {
      layout: "line",
      motion: "boil",
      exit: "smear",
      color: "#FFF7E6",
      lineColors: DUOTONO_INKS,
      fontScale: 1.8,
      textShadow: "0.26cqw 0.26cqw 0 #1B2C7A",
      stroke: "0.42cqw #1B2C7A",
      // Póster a dos tintas: azul en las sombras, crema en las luces.
      duotone: ["#1B2C7A", "#F4ECDD"],
      grade: "grayscale(1) contrast(1.35) brightness(1.03)",
      thumbBg: "linear-gradient(135deg,#F4ECDD 0%,#8f97b8 45%,#1B2C7A 100%)",
    },
  },
  {
    code: "romantico",
    font: { css: "'Sacramento', cursive", weight: 400 },
    preview: {
      layout: "line",
      motion: "write_on",
      forceCase: "original",
      color: "#FFFFFF",
      fontScale: 2.4,
      textShadow: "0 0 0.4cqw #FFD9C2, 0 0 1.1cqw rgba(255,217,194,.75)",
      stroke: "0.08cqw #FFD9C2",
      letterbox: true,
      vignette: true,
      grade: "contrast(.94) saturate(.85) brightness(1.04)",
      tint: "#ffb38a",
      thumbBg: "radial-gradient(120% 100% at 50% 40%,#c98a6a 0%,#7a4a3c 55%,#2a1612 100%)",
    },
  },
  {
    code: "degrade",
    // Pinta todo el cuadro: en un job nuevo el backend no genera fondo IA.
    ownsBackground: true,
    font: { css: "'Oswald', sans-serif", weight: 700 },
    preview: {
      layout: "line",
      motion: "fade",
      color: "#FFFFFF",
      fontScale: 1.35,
      textShadow: "0 0.1cqw 0.25cqw rgba(0,0,0,.45)",
      stroke: "0px",
      // Fondo propio: degradé de atardecer + dos cordones de montañas.
      gradient: DEGRADE_SKY,
      mountains: ["#5B2A7A", "#21123A"],
      thumbBg: `linear-gradient(180deg,${DEGRADE_SKY.join(",")})`,
    },
  },
  {
    code: "y2k",
    font: { css: "'Michroma', sans-serif", weight: 400 },
    preview: {
      layout: "build",
      motion: "word_pop",
      wordMotion: "echo",
      color: "#FFFFFF",
      accent: "#BFF6FF",
      fontScale: 1.05,
      keyScale: 1.3,
      stagger: 0.08,
      rowWidth: 0.66,
      glyphWidth: 0.95,
      textShadow: "0 0 0.4cqw #5ED8FF, 0.16cqw 0.16cqw 0 rgba(30,107,255,.55)",
      stroke: "0.2cqw #5ED8FF",
      grade: "hue-rotate(-12deg) saturate(.75) brightness(1.08) contrast(.95)",
      tint: "#5ed8ff",
      lightLeak: true,
      thumbBg: "radial-gradient(120% 100% at 30% 20%,#e9fbff 0%,#8fd3f0 45%,#2a6fa8 100%)",
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

/**
 * ¿El look pinta todo el cuadro (tarjetas / degradé)? En un job NUEVO el
 * backend no genera fondo IA para estos looks (ahorra el costo de Veo).
 */
export function lookOwnsBackground(value) {
  const look = getLyricLook(value);
  return !!(look && look.ownsBackground);
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
  cinetico: t("upload.look_cinetico") || "Cinético",
  neon: t("upload.look_neon") || "Neón",
  chat: t("upload.look_chat") || "Chat",
  cuaderno: t("upload.look_cuaderno") || "Cuaderno",
  bloque: t("upload.look_bloque") || "Bloque",
  arco: t("upload.look_arco") || "Arco",
  perspectiva: t("upload.look_perspectiva") || "Perspectiva 3D",
  duotono: t("upload.look_duotono") || "Duotono",
  romantico: t("upload.look_romantico") || "Romántico",
  degrade: t("upload.look_degrade") || "Degradé",
  y2k: t("upload.look_y2k") || "Y2K",
});

const LYRIC_LOOK_DESCS = (t) => ({
  "": t("upload.look_none_desc") || "Elegís tipografía, animación y transición a mano.",
  cosmico: t("upload.look_cosmico_desc") || "La letra viaja hacia la cámara y pasa de largo.",
  cine: t("upload.look_cine_desc") || "Serif elegante, palabra clave grande, franjas de cine.",
  pincel: t("upload.look_pincel_desc") || "Trazo de pincel, palabra por palabra, clave en rojo.",
  pop70: t("upload.look_pop70_desc") || "No usa el fondo: tarjetas de color que cambian en cada línea.",
  pelicula: t("upload.look_pelicula_desc") || "Película gastada con tinte verdoso y letra amarilla chica.",
  cinetico: t("upload.look_cinetico_desc") || "Las palabras llegan de a una desde todos lados y arman la frase. No usa el fondo.",
  neon: t("upload.look_neon_desc") || "Cada línea es un cartel de neón que titila y se prende, sobre el fondo oscurecido.",
  chat: t("upload.look_chat_desc") || "La letra llega como una conversación de mensajes. Ideal para shorts.",
  cuaderno: t("upload.look_cuaderno_desc") || "Letra escrita a mano palabra por palabra, la clave marcada en rojo y dibujitos.",
  bloque: t("upload.look_bloque_desc") || "Filas estiradas al mismo ancho, gruesa y fina alternadas, la clave en coral.",
  arco: t("upload.look_arco_desc") || "La frase gira alrededor de un anillo y la clave queda grande en el medio.",
  perspectiva: t("upload.look_perspectiva_desc") || "La letra va acostada en el piso y viene hacia la cámara.",
  duotono: t("upload.look_duotono_desc") || "El fondo se vuelve un afiche a dos tintas y la letra de marcador tiembla.",
  romantico: t("upload.look_romantico_desc") || "Cursiva que se escribe sola, luz cálida y franjas de cine.",
  degrade: t("upload.look_degrade_desc") || "No usa el fondo: atardecer violeta y rosa con montañas, letra limpia.",
  y2k: t("upload.look_y2k_desc") || "Letra techno ancha con ecos que se juntan, sobre un tono celeste helado.",
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

/** Palabra "función" (stopword o ≤2 letras): Cinético la escribe chica. */
export function isFunctionWord(tok) {
  const n = normToken(tok);
  return STOPWORDS.has(n) || n.length <= 2;
}

/**
 * Filas de la composición cinética (espejo de backend _kinetic_events):
 * corridas de palabras gruesas de hasta 12 caracteres, cada palabra función
 * en su fila (las consecutivas se juntan en una fila manuscrita) y la clave
 * siempre sola. Devuelve { rows: [{ idx: [i...], kind }], keyIndex } con
 * kind = "script" | "heavy" | "key".
 */
export function kineticRows(tokens) {
  const k = pickKeyword(tokens);
  const small = (i) => i !== k && isFunctionWord(tokens[i]);
  const rows = [];
  let cur = [];
  tokens.forEach((_tok, i) => {
    if (i === k || small(i)) {
      if (cur.length) rows.push(cur);
      cur = [];
      rows.push([i]);
      return;
    }
    if (cur.length && [...cur, i].map((j) => tokens[j]).join(" ").length > 12) {
      rows.push(cur);
      cur = [];
    }
    cur.push(i);
  });
  if (cur.length) rows.push(cur);
  const merged = [];
  rows.forEach((r) => {
    const prev = merged[merged.length - 1];
    if (prev && r.length === 1 && small(r[0]) && prev.every(small)) prev.push(r[0]);
    else merged.push([...r]);
  });
  return {
    keyIndex: k,
    rows: merged.map((idx) => ({
      idx,
      kind: idx.every(small) ? "script" : (idx.length === 1 && idx[0] === k ? "key" : "heavy"),
    })),
  };
}

/**
 * Filas del look Bloque (espejo de backend _block_events): 1-3 palabras de
 * hasta 11 caracteres, la clave sola en su fila. Devuelve
 * { rows: [{ idx, key, heavy }], keyIndex }: las filas alternan fina/gruesa
 * (la clave y una línea de una sola fila van gruesas).
 */
export function blockRows(tokens) {
  const k = pickKeyword(tokens);
  const rows = [];
  let cur = [];
  tokens.forEach((_tok, i) => {
    if (i === k && tokens.length > 1) {
      if (cur.length) rows.push(cur);
      cur = [];
      rows.push([i]);
      return;
    }
    if (cur.length && [...cur, i].map((j) => tokens[j]).join(" ").length > 11) {
      rows.push(cur);
      cur = [i];
    } else {
      cur.push(i);
    }
  });
  if (cur.length) rows.push(cur);
  return {
    keyIndex: k,
    rows: rows.map((idx, r) => {
      const key = idx.length === 1 && idx[0] === k && tokens.length > 1;
      return { idx, key, heavy: key || r % 2 === 1 || rows.length === 1 };
    }),
  };
}

/**
 * Inclinación "puesta a mano" de cada palabra (Cuaderno), determinística
 * por línea/palabra como el backend: amp × ((i·37 + línea·11) mod 7 − 3) / 3.
 */
export function handWordTilt(i, lineIdx, amp) {
  return amp * ((((i * 37 + lineIdx * 11) % 7) - 3) / 3);
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
