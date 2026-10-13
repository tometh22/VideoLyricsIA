// Variantes de looks por género + overrides del operador (contrato:
// .context/variants_contract.md). Un look (lib/lyricLooks) fija el
// MOVIMIENTO y la composición; la variante elige fuentes, paleta y
// tratamiento según el género de la canción. El operador puede corregir el
// género, pedir "otra variante" y pisar fuentes / colores.
//
// Este módulo es la parte pura (sin React): catálogo estático de respaldo,
// cliente de los 3 endpoints y los helpers de payload. El hook que los usa
// en vivo está en hooks/useLookVariant.js; el panel en
// components/LookVariantPanel.jsx.
//
// Campos (render_params / Form de /generate y /upload / body de edit y
// variant / campañas). Todos opcionales; "" = automático:
//   lyric_genre         "" | metal | rock | punk | pop | latin | reggaeton |
//                       hiphop | electronic | ballad | folk | indie
//   lyric_variant       "" | int ≥ 0
//   lyric_font_main     "" | id de fuente del catálogo
//   lyric_font_key      "" | id de fuente del catálogo
//   lyric_alt_color     "" | #RRGGBB
//   lyric_accent_color  "" | #RRGGBB
//   (lyric_color ya existía: con look, "#FFFFFF"/"" = el de la variante)

import { getLyricLook, normalizeLyricLook } from "./lyricLooks.js";

/** camelCase (wizard / review) ↔ snake_case (backend). */
export const LOOK_VARIANT_FIELDS = [
  ["lyricGenre", "lyric_genre"],
  ["lyricVariant", "lyric_variant"],
  ["lyricFontMain", "lyric_font_main"],
  ["lyricFontKey", "lyric_font_key"],
  ["lyricAltColor", "lyric_alt_color"],
  ["lyricAccentColor", "lyric_accent_color"],
];

/** Campos que "Restaurar" vuelve a la variante automática. */
export const LOOK_OVERRIDE_RESET = {
  lyricVariant: "",
  lyricFontMain: "",
  lyricFontKey: "",
  lyricColor: "#FFFFFF",
  lyricAltColor: "",
  lyricAccentColor: "",
};

export const LYRIC_GENRE_CODES = [
  "metal", "rock", "punk", "pop", "latin", "reggaeton",
  "hiphop", "electronic", "ballad", "folk", "indie",
];

// ── Catálogo de respaldo ────────────────────────────────────────────────
// Si GET /lyric-looks/catalog falla la UI sigue funcionando con esto. Los
// ids de fuente espejan backend/fonts (mismos archivos que lyric_recipes
// FONT_PAIRS + las fuentes propias de cada look).
const font = (id, label, weight, families = []) => ({ id, label, css_family: label, weight, families });

export const STATIC_LOOK_CATALOG = {
  families: [
    { id: "heavy", label: "Rock / metal" },
    { id: "pop", label: "Pop / latino" },
    { id: "urban", label: "Urbano / electrónica" },
    { id: "soft", label: "Balada / folk / indie" },
  ],
  genres: [
    { id: "metal", family: "heavy", label: "Metal" },
    { id: "rock", family: "heavy", label: "Rock" },
    { id: "punk", family: "heavy", label: "Punk" },
    { id: "pop", family: "pop", label: "Pop" },
    { id: "latin", family: "pop", label: "Latino" },
    { id: "reggaeton", family: "pop", label: "Reggaetón" },
    { id: "hiphop", family: "urban", label: "Hip-hop / trap" },
    { id: "electronic", family: "urban", label: "Electrónica" },
    { id: "ballad", family: "soft", label: "Balada" },
    { id: "folk", family: "soft", label: "Folk" },
    { id: "indie", family: "soft", label: "Indie" },
  ],
  fonts: [
    font("anton", "Anton", 400, ["heavy", "urban"]),
    font("audiowide", "Audiowide", 400, ["urban", "pop"]),
    font("bebas-neue", "Bebas Neue", 400, ["heavy", "urban"]),
    { ...font("big-shoulders", "Big Shoulders Display", 900, ["heavy", "urban", "pop"]), label: "Big Shoulders" },
    { ...font("big-shoulders-light", "Big Shoulders Display", 300, ["urban", "heavy"]), label: "Big Shoulders Light" },
    font("black-ops-one", "Black Ops One", 400, ["heavy", "urban"]),
    font("bungee", "Bungee", 400, ["heavy", "pop", "urban"]),
    font("caveat", "Caveat", 700, ["pop", "soft"]),
    font("creepster", "Creepster", 400, ["heavy"]),
    font("fredoka", "Fredoka", 600, ["pop"]),
    font("jost", "Jost", 700, ["soft", "urban"]),
    font("knewave", "Knewave", 400, ["heavy", "pop"]),
    font("marcellus", "Marcellus", 400, ["soft"]),
    font("metal-mania", "Metal Mania", 400, ["heavy"]),
    font("michroma", "Michroma", 400, ["urban", "pop"]),
    font("montserrat", "Montserrat", 800, ["pop", "urban", "soft"]),
    font("neonderthaw", "Neonderthaw", 400, ["urban", "soft"]),
    font("new-rocker", "New Rocker", 400, ["heavy"]),
    font("nunito", "Nunito", 800, ["pop", "soft"]),
    font("oswald", "Oswald", 700, ["heavy", "urban", "soft"]),
    font("outfit", "Outfit", 700, ["pop", "soft"]),
    font("permanent-marker", "Permanent Marker", 400, ["pop", "urban"]),
    font("pirata-one", "Pirata One", 400, ["heavy"]),
    font("poppins", "Poppins", 700, ["pop"]),
    font("quicksand", "Quicksand", 700, ["soft"]),
    font("roboto", "Roboto", 700, ["urban", "pop"]),
    font("rubik-dirt", "Rubik Dirt", 400, ["heavy"]),
    font("rubik-glitch", "Rubik Glitch", 400, ["heavy", "urban"]),
    font("rubik-wet-paint", "Rubik Wet Paint", 400, ["heavy"]),
    font("sacramento", "Sacramento", 400, ["soft"]),
    font("shrikhand", "Shrikhand", 400, ["pop"]),
    font("tilt-neon", "Tilt Neon", 400, ["urban", "pop"]),
    font("unifraktur-cook", "UnifrakturCook", 700, ["heavy"]),
  ],
  recommended: {
    heavy: ["cinetico", "pincel", "bloque", "neon", "cine", "duotono", "perspectiva", "cosmico"],
    pop: ["cinetico", "pop70", "chat", "cuaderno", "y2k", "degrade", "duotono", "neon"],
    urban: ["bloque", "neon", "cinetico", "y2k", "perspectiva", "cosmico", "arco", "duotono"],
    soft: ["cine", "romantico", "pelicula", "cuaderno", "arco", "degrade", "cosmico", "chat"],
  },
};

/** Normaliza la respuesta del catálogo: cualquier hueco cae al respaldo. */
export function normalizeCatalog(data) {
  const d = data && typeof data === "object" ? data : {};
  const arr = (v, fb) => (Array.isArray(v) && v.length ? v : fb);
  return {
    families: arr(d.families, STATIC_LOOK_CATALOG.families),
    genres: arr(d.genres, STATIC_LOOK_CATALOG.genres),
    fonts: arr(d.fonts, STATIC_LOOK_CATALOG.fonts).filter((f) => f && f.id),
    recommended: d.recommended && typeof d.recommended === "object"
      ? d.recommended
      : STATIC_LOOK_CATALOG.recommended,
  };
}

export function catalogGenre(catalog, genreId) {
  const id = String(genreId || "").trim().toLowerCase();
  if (!id) return null;
  return ((catalog || STATIC_LOOK_CATALOG).genres || []).find((g) => g.id === id) || null;
}

export function catalogFamily(catalog, familyId) {
  if (!familyId) return null;
  return ((catalog || STATIC_LOOK_CATALOG).families || []).find((f) => f.id === familyId) || null;
}

export function catalogFont(catalog, fontId) {
  if (!fontId) return null;
  return ((catalog || STATIC_LOOK_CATALOG).fonts || []).find((f) => f.id === fontId)
    || STATIC_LOOK_CATALOG.fonts.find((f) => f.id === fontId)
    || null;
}

/** Familia visual de un género ("" si no se conoce). */
export function genreFamily(catalog, genreId) {
  return catalogGenre(catalog, genreId)?.family || "";
}

/** `font-family` CSS para un css_family del catálogo. */
export function fontCss(cssFamily) {
  const name = String(cssFamily || "").replace(/['"\\;{}]/g, "").trim();
  return name ? `'${name}', sans-serif` : undefined;
}

/**
 * Fuentes del catálogo ordenadas para un picker: primero las de la familia
 * del género, después el resto (orden del catálogo dentro de cada grupo).
 */
export function orderedFonts(catalog, family) {
  const fonts = (catalog || STATIC_LOOK_CATALOG).fonts || [];
  if (!family) return fonts;
  const inFam = (f) => Array.isArray(f.families) && f.families.includes(family);
  return [...fonts.filter(inFam), ...fonts.filter((f) => !inFam(f))];
}

/**
 * Looks de la galería con los recomendados para la familia primero. "Sin
 * look" ("") queda siempre primero. Devuelve [{ ...look, recommended }].
 */
export function orderLooksForFamily(looks, catalog, family) {
  const rec = (family && (catalog || STATIC_LOOK_CATALOG).recommended?.[family]) || [];
  if (!rec.length) return looks.map((l) => ({ ...l, recommended: false }));
  const rank = new Map(rec.map((code, i) => [code, i]));
  const none = looks.filter((l) => !l.code);
  const recommended = looks
    .filter((l) => l.code && rank.has(l.code))
    .sort((a, b) => rank.get(a.code) - rank.get(b.code))
    .map((l) => ({ ...l, recommended: true }));
  const rest = looks.filter((l) => l.code && !rank.has(l.code)).map((l) => ({ ...l, recommended: false }));
  return [...none.map((l) => ({ ...l, recommended: false })), ...recommended, ...rest];
}

// ── Valores ─────────────────────────────────────────────────────────────

/** "" | entero ≥ 0. */
export function normalizeLyricVariant(value) {
  if (value == null || value === "") return "";
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 ? n : "";
}

export function normalizeLyricGenre(value) {
  const g = String(value == null ? "" : value).trim().toLowerCase();
  return LYRIC_GENRE_CODES.includes(g) ? g : "";
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/;
export function normalizeHexOrEmpty(value) {
  const v = String(value == null ? "" : value).trim();
  return HEX_RE.test(v) ? v.toUpperCase() : "";
}

function normalizeFontId(value) {
  return String(value == null ? "" : value).trim().slice(0, 64);
}

/** Los 6 campos nuevos (camelCase) desde un render_params (snake_case). */
export function lookVariantFieldsFromParams(params) {
  const p = params || {};
  return {
    lyricGenre: normalizeLyricGenre(p.lyric_genre),
    lyricVariant: normalizeLyricVariant(p.lyric_variant),
    lyricFontMain: normalizeFontId(p.lyric_font_main),
    lyricFontKey: normalizeFontId(p.lyric_font_key),
    lyricAltColor: normalizeHexOrEmpty(p.lyric_alt_color),
    lyricAccentColor: normalizeHexOrEmpty(p.lyric_accent_color),
  };
}

/** Copia normalizada de los 6 campos de un objeto camelCase. */
export function pickLookVariantFields(entry) {
  const e = entry || {};
  return {
    lyricGenre: normalizeLyricGenre(e.lyricGenre),
    lyricVariant: normalizeLyricVariant(e.lyricVariant),
    lyricFontMain: normalizeFontId(e.lyricFontMain),
    lyricFontKey: normalizeFontId(e.lyricFontKey),
    lyricAltColor: normalizeHexOrEmpty(e.lyricAltColor),
    lyricAccentColor: normalizeHexOrEmpty(e.lyricAccentColor),
  };
}

// ── Lo último que resolvió el backend ("pin") ───────────────────────────
// Para que el preview sea el render: si el operador NO pidió otra variante
// (lyric_variant ""), al enviar se manda el número que /resolve eligió para
// ESA canción. El género NO se pinea: "automático" viaja como "" y el render
// usa la misma detección cacheada que el preview (pinearlo lo convertía en
// un género "elegido por el operador" que después no se podía limpiar). El
// pin guarda look + canción + género/variante pedidos: si cualquiera cambió
// desde que se resolvió, no se aplica.

const songKey = (artist, title) =>
  `${String(artist || "").trim().toLowerCase()}|${String(title || "").trim().toLowerCase()}`;

/** Pin a guardar en la review a partir de una respuesta de /resolve. */
export function makeLookResolvedPin(resolved, song = null) {
  if (!resolved || !resolved.look) return null;
  // Por defecto, la canción para la que se pidió (useLookVariant la anota).
  const { artist = "", songTitle = "" } = song || resolved._request || {};
  const req = resolved._request || {};
  return {
    look: normalizeLyricLook(resolved.look),
    variant: normalizeLyricVariant(resolved.variant),
    genre: normalizeLyricGenre(resolved.genre),
    song: songKey(artist, songTitle),
    // Para qué género/variante del operador se resolvió.
    reqGenre: normalizeLyricGenre(req.genre),
    reqVariant: normalizeLyricVariant(req.variant),
    // Si la variante pinta su propio fondo (tarjetas/degradé) o usa el de la
    // canción: decide si vale la pena pre-generar el fondo.
    background: ["flat", "gradient", "source"].includes(resolved.background)
      ? resolved.background : "",
  };
}

function pinFor(entry) {
  const pin = entry && entry.lookResolved;
  if (!pin || !pin.look) return null;
  if (pin.look !== normalizeLyricLook(entry.lyricLook)) return null;
  if (pin.song !== songKey(entry.artist, entry.songTitle)) return null;
  // Resuelto para otro género/variante (el operador cambió algo y la
  // respuesta nueva todavía no llegó, o falló): no sirve.
  if ((pin.reqGenre || "") !== normalizeLyricGenre(entry.lyricGenre)) return null;
  if ((pin.reqVariant ?? "") !== normalizeLyricVariant(entry.lyricVariant)) return null;
  return pin;
}

/**
 * Valores EFECTIVOS a enviar (camelCase): lo que eligió el operador y, donde
 * dejó "automático", lo que el backend resolvió para esta canción.
 */
export function effectiveLookVariantFields(entry) {
  const base = pickLookVariantFields(entry);
  if (!getLyricLook(entry && entry.lyricLook)) return base;
  const pin = pinFor(entry);
  if (pin && base.lyricVariant === "" && pin.variant !== "") base.lyricVariant = pin.variant;
  return base;
}

/**
 * Campos snake_case para /generate y /upload: sólo los que tienen valor
 * (mismo criterio que lyric_look/lyric_color: sin look no viaja nada).
 */
export function lookVariantSubmitFields(entry) {
  if (!getLyricLook(entry && entry.lyricLook)) return {};
  const eff = effectiveLookVariantFields(entry);
  const out = {};
  for (const [camel, snake] of LOOK_VARIANT_FIELDS) {
    const v = eff[camel];
    if (v !== "" && v != null) out[snake] = v;
  }
  return out;
}

/** Igual que lookVariantSubmitFields pero sobre un FormData. */
export function appendLookVariantFields(formData, entry) {
  for (const [k, v] of Object.entries(lookVariantSubmitFields(entry))) {
    formData.append(k, String(v));
  }
  return formData;
}

/**
 * Estado ABSOLUTO para /variant: todos los campos, "" = automático. Sin
 * look, todo "" (la variante no hereda overrides de un look que ya no tiene).
 */
export function lookVariantAbsoluteFields(entry) {
  const hasLook = !!getLyricLook(entry && entry.lyricLook);
  const eff = hasLook ? effectiveLookVariantFields(entry) : null;
  const out = {};
  for (const [camel, snake] of LOOK_VARIANT_FIELDS) out[snake] = hasLook ? eff[camel] : "";
  return out;
}

// ── Body de /lyric-looks/resolve ────────────────────────────────────────
export function buildResolveBody({
  look, genre, variant, fontMain, fontKey, color, altColor, accentColor,
  jobId, artist, songTitle, fresh = false,
} = {}) {
  const c = normalizeHexOrEmpty(color);
  const body = {
    lyric_look: normalizeLyricLook(look),
    lyric_genre: normalizeLyricGenre(genre),
    lyric_variant: normalizeLyricVariant(variant),
    lyric_font_main: normalizeFontId(fontMain),
    lyric_font_key: normalizeFontId(fontKey),
    // Con look, el blanco default = "el de la variante".
    lyric_color: c === "#FFFFFF" ? "" : c,
    lyric_alt_color: normalizeHexOrEmpty(altColor),
    lyric_accent_color: normalizeHexOrEmpty(accentColor),
    artist: String(artist || "").trim(),
    song_title: String(songTitle || "").trim(),
  };
  // Sin job (wizard antes de transcribir, campañas) la key no viaja.
  if (jobId) body.job_id = String(jobId);
  // Variante NUEVA de un job: la del padre es una hermana a evitar.
  if (fresh) body.fresh = true;
  return body;
}

/** ¿Trae la respuesta de /resolve warnings de contraste? */
export function contrastWarnings(resolved) {
  const w = resolved && Array.isArray(resolved.warnings) ? resolved.warnings : [];
  return w.filter((x) => typeof x === "string"
    && (x.startsWith("low_contrast_") || x === "accent_same_as_text"));
}

// ── Red ─────────────────────────────────────────────────────────────────
// Defaults para los callers que no traen los suyos (mismo patrón que
// UploadZone: VITE_API_URL + token de localStorage).
export const LOOK_API = (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.VITE_API_URL) || "";

export function lookAuthHeaders() {
  try {
    const token = localStorage.getItem("genly_token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

// Cache de módulo: el catálogo se pide UNA vez por sesión; la detección de
// género una vez por canción (artista|título).
let catalogCache = null; // catálogo normalizado | Promise
const genreCache = new Map(); // songKey → resultado | Promise

/** Sólo para tests. */
export function _resetLookVariantCaches() {
  catalogCache = null;
  genreCache.clear();
}

export function cachedLookCatalog() {
  return catalogCache && typeof catalogCache.then !== "function" ? catalogCache : null;
}

export function fetchLookCatalog(api = "", authHeaders = () => ({})) {
  if (catalogCache) return Promise.resolve(catalogCache);
  const p = fetch(`${api}/lyric-looks/catalog`, { headers: authHeaders() })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      // Un fallo deja el respaldo estático por el resto de la sesión (una
      // sola llamada por sesión, como pide el contrato).
      catalogCache = data ? normalizeCatalog(data) : STATIC_LOOK_CATALOG;
      return catalogCache;
    })
    .catch(() => {
      catalogCache = STATIC_LOOK_CATALOG;
      return catalogCache;
    });
  catalogCache = p;
  return p;
}

export function detectGenreCacheKey(artist, songTitle) {
  return songKey(artist, songTitle);
}

export function cachedDetectedGenre(artist, songTitle) {
  const hit = genreCache.get(songKey(artist, songTitle));
  return hit && typeof hit.then !== "function" ? hit : null;
}

export function fetchDetectGenre({ artist, songTitle, lyricsText = "", jobId = null } = {}, api = "", authHeaders = () => ({})) {
  const key = songKey(artist, songTitle);
  const hit = genreCache.get(key);
  if (hit !== undefined) return Promise.resolve(hit);
  const body = {
    artist: String(artist || "").trim(),
    song_title: String(songTitle || "").trim(),
    lyrics_text: String(lyricsText || "").slice(0, 6000),
    ...(jobId ? { job_id: jobId } : {}),
  };
  const p = fetch(`${api}/lyric-looks/detect-genre`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      const result = data
        ? { genre: normalizeLyricGenre(data.genre), family: data.family || "", source: data.source || "none" }
        : null;
      // Un fallo no se cachea: la próxima apertura reintenta.
      if (result) genreCache.set(key, result);
      else genreCache.delete(key);
      return result;
    })
    .catch(() => {
      genreCache.delete(key);
      return null;
    });
  genreCache.set(key, p);
  return p;
}

export function fetchResolveLook(body, api = "", authHeaders = () => ({}), signal) {
  return fetch(`${api}/lyric-looks/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
    signal,
  }).then((res) => {
    if (!res.ok) throw new Error(`resolve ${res.status}`);
    return res.json();
  });
}

// ── Fuentes en el navegador ─────────────────────────────────────────────
// index.html precarga las fuentes de los looks y las de las variantes. Si
// el catálogo trae una familia que no está, se pide a Google Fonts una vez.
const PRELOADED_FAMILIES = new Set([
  "Anton", "Audiowide", "Bebas Neue", "Big Shoulders Display", "Caveat", "Fredoka", "Jost",
  "Knewave", "Marcellus", "Michroma", "Montserrat", "Neonderthaw", "Nunito", "Outfit", "Oswald",
  "Permanent Marker", "Poppins", "Quicksand", "Roboto", "Sacramento", "Shrikhand", "Tilt Neon",
  "Metal Mania", "New Rocker", "Pirata One", "UnifrakturCook", "Black Ops One", "Bungee",
  "Rubik Glitch", "Rubik Wet Paint", "Rubik Dirt", "Creepster",
]);
const requestedFamilies = new Set();

export function ensureFontLoaded(cssFamily, weight) {
  const name = String(cssFamily || "").replace(/['"\\;{}]/g, "").trim();
  if (!name || PRELOADED_FAMILIES.has(name) || requestedFamilies.has(name)) return false;
  if (typeof document === "undefined" || !document.head) return false;
  requestedFamilies.add(name);
  const w = Number.isFinite(Number(weight)) && Number(weight) !== 400 ? `:wght@${Number(weight)}` : "";
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `https://fonts.googleapis.com/css2?family=${encodeURIComponent(name).replace(/%20/g, "+")}${w}&display=swap`;
  link.dataset.lookFont = name;
  document.head.appendChild(link);
  return true;
}

// ── Preview: el look con la variante aplicada ───────────────────────────
/**
 * Devuelve una copia del look (lib/lyricLooks) con las fuentes y colores de
 * la variante, para el preview CSS:
 *   - con `resolved` (respuesta de /resolve para ESTE look): fuentes
 *     principal/destacada, tinta/secundario/acento y tipo de fondo
 *     ("source" saca las tarjetas/degradé propios del look);
 *   - sin respuesta todavía: sólo los overrides crudos del operador
 *     (fuentes del catálogo, secundario, destacado).
 * Sin nada que aplicar devuelve el mismo objeto (comportamiento histórico).
 */
export function lookWithVariant(look, {
  resolved = null, fontMain = "", fontKey = "", altColor = "", accentColor = "", catalog = null,
} = {}) {
  if (!look || !look.preview) return look;
  const r = resolved && normalizeLyricLook(resolved.look) === look.code ? resolved : null;
  const face = (rf, overrideId) => {
    if (r && rf && rf.css_family) return { css: fontCss(rf.css_family), weight: Number(rf.weight) || 400 };
    const f = overrideId ? catalogFont(catalog, overrideId) : null;
    return f ? { css: fontCss(f.css_family), weight: Number(f.weight) || 400 } : null;
  };
  const main = face(r?.fonts?.main, fontMain);
  const key = face(r?.fonts?.key, fontKey);
  const ink = r ? normalizeHexOrEmpty(r.colors?.ink) : "";
  const alt = (r ? normalizeHexOrEmpty(r.colors?.alt) : "") || normalizeHexOrEmpty(altColor);
  const accent = (r ? normalizeHexOrEmpty(r.colors?.accent) : "") || normalizeHexOrEmpty(accentColor);
  if (!r && !main && !key && !alt && !accent) return look;

  const lp = { ...look.preview };
  const style = r && r.style && typeof r.style === "object" ? r.style : null;
  if (key) {
    lp.keyFont = key;
    // Sin `style` (backend viejo): las "manuscritas" del look (Cinético /
    // Neón) toman la destacada. Con `style`, los conectores tienen su propia
    // fuente (style.connector_font) — ver abajo.
    if (lp.scriptFont && !style) lp.scriptFont = key;
  }
  if (style) {
    // Composición de la variante tal cual la arma el render (resolve.style).
    if (typeof style.letterbox === "boolean") lp.letterbox = style.letterbox;
    if (lp.layout === "kinetic") {
      const cf = style.connector_font ? catalogFont(catalog, style.connector_font) : null;
      lp.kineticStyle = {
        decorations: Array.isArray(style.decorations) ? style.decorations.filter((d) => typeof d === "string") : [],
        connectorFont: cf ? { css: fontCss(cf.css_family), weight: Number(cf.weight) || 400 } : null,
        connectorColor: normalizeHexOrEmpty(style.connector_color) || null,
        connectorTilt: style.connector_tilt !== false,
        beatShake: !!style.beat_shake,
        cascade: !!style.cascade,
      };
    }
  }
  if (ink) lp.color = ink;
  if (accent) lp.accent = accent;
  const source = r && r.background === "source";
  if (source) {
    // La variante usa el fondo de la canción: sin tarjetas ni degradé.
    delete lp.cards;
    delete lp.cardAccents;
    delete lp.gradient;
    delete lp.mountains;
  }
  const flat = !!lp.cards;
  if (lp.cardPalettes || lp.layout === "kinetic") {
    if (!flat && r) {
      const i = ink || lp.color;
      lp.cardPalettes = [[i, alt || i, accent || i]];
    } else if (lp.cardPalettes) {
      const ov = r?.overridden || {};
      lp.cardPalettes = lp.cardPalettes.map(([pi, pa, pc]) => [
        r && ov.ink && ink ? ink : pi,
        (!r || ov.alt) && alt ? alt : pa,
        (!r || ov.accent) && accent ? accent : pc,
      ]);
    }
  }
  if (flat && lp.cardAccents && accent && (!r || r.overridden?.accent)) {
    lp.cardAccents = lp.cardAccents.map(() => accent);
  }
  if (lp.lineColors && r && !flat) {
    lp.lineColors = [ink || lp.color, alt, accent].filter(Boolean);
  }
  if (lp.layout === "chat" && r) {
    // Mismos roles que el render (lyric_recipes.apply_colors): Texto =
    // mensaje recibido, Secundario = su globo, Destacado = globo enviado;
    // el texto enviado es blanco o casi negro según lo que se lea.
    const [inc, out] = lp.bubbles || ["#E9E9EB", "#1F8BFF"];
    const outgoing = accent || out;
    lp.bubbles = [alt || inc, outgoing];
    lp.accent = hexLuminance(outgoing) < 0.6 ? "#FFFFFF" : "#111111";
  }
  return { ...look, font: main || look.font, preview: lp };
}

// Misma fórmula que lyric_recipes.luminance (sin linealizar).
function hexLuminance(hex) {
  const h = normalizeHexOrEmpty(hex);
  if (!h) return 0;
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
