// Variantes de looks por género: helpers puros (orden de galería, payloads,
// pin de lo resuelto, preview). Contrato: .context/variants_contract.md.
import { describe, expect, it } from "vitest";
import {
  STATIC_LOOK_CATALOG, appendLookVariantFields, buildResolveBody, contrastWarnings,
  effectiveLookVariantFields, lookVariantAbsoluteFields, lookVariantFieldsFromParams,
  lookVariantSubmitFields, lookWithVariant, makeLookResolvedPin, normalizeCatalog,
  orderLooksForFamily, orderedFonts,
} from "./lyricLookVariants.js";
import { getLyricLook, lyricLookOptions } from "./lyricLooks.js";

const t = (_k) => "";
const RESOLVED = {
  look: "cinetico", genre: "metal", family: "heavy", genre_source: "detected", variant: 3,
  description: "Rubik Dirt + Metal Mania · sangre · oscuro",
  fonts: { main: { id: "rubik-dirt", css_family: "Rubik Dirt", weight: 400 }, key: { id: "metal-mania", css_family: "Metal Mania", weight: 400 } },
  colors: { ink: "#F2EEE6", alt: "#CFC9BE", accent: "#D0101E" },
  overridden: { font_main: false, font_key: false, ink: false, alt: false, accent: false },
  background: "source",
  warnings: [],
};

describe("galería: recomendados primero", () => {
  const looks = lyricLookOptions(t);

  it("sin familia conocida conserva el orden del catálogo", () => {
    const out = orderLooksForFamily(looks, STATIC_LOOK_CATALOG, "");
    expect(out.map((l) => l.code)).toEqual(looks.map((l) => l.code));
    expect(out.some((l) => l.recommended)).toBe(false);
  });

  it("con familia: 'sin look' primero, después recommended[family] en su orden, después el resto", () => {
    const out = orderLooksForFamily(looks, STATIC_LOOK_CATALOG, "heavy");
    const rec = STATIC_LOOK_CATALOG.recommended.heavy;
    expect(out[0].code).toBe("");
    expect(out.slice(1, 1 + rec.length).map((l) => l.code)).toEqual(rec);
    expect(out.slice(1, 1 + rec.length).every((l) => l.recommended)).toBe(true);
    expect(out.slice(1 + rec.length).some((l) => l.recommended)).toBe(false);
    expect(out).toHaveLength(looks.length);
  });

  it("las fuentes de la familia del género van primero", () => {
    const fonts = orderedFonts(STATIC_LOOK_CATALOG, "soft");
    const firstOther = fonts.findIndex((f) => !f.families.includes("soft"));
    expect(firstOther).toBeGreaterThan(0);
    expect(fonts.slice(firstOther).some((f) => f.families.includes("soft"))).toBe(false);
  });

  it("un catálogo incompleto del backend cae al respaldo por partes", () => {
    const c = normalizeCatalog({ families: [], fonts: [{ id: "x", label: "X", css_family: "X" }] });
    expect(c.families).toBe(STATIC_LOOK_CATALOG.families);
    expect(c.fonts.map((f) => f.id)).toEqual(["x"]);
    expect(c.recommended).toBe(STATIC_LOOK_CATALOG.recommended);
  });
});

describe("payload de /generate: sólo campos con valor", () => {
  const base = { lyricLook: "cinetico", artist: "Almafuerte", songTitle: "Toro y Pampa" };

  it("sin overrides ni pin no manda nada nuevo", () => {
    expect(lookVariantSubmitFields(base)).toEqual({});
  });

  it("manda los overrides elegidos", () => {
    const out = lookVariantSubmitFields({
      ...base, lyricGenre: "rock", lyricVariant: 4, lyricFontMain: "anton", lyricFontKey: "",
      lyricAltColor: "#112233", lyricAccentColor: "",
    });
    expect(out).toEqual({ lyric_genre: "rock", lyric_variant: 4, lyric_font_main: "anton", lyric_alt_color: "#112233" });
  });

  it("sin look no viaja ningún campo de variante", () => {
    expect(lookVariantSubmitFields({ lyricLook: "", lyricGenre: "rock", lyricVariant: 2 })).toEqual({});
  });

  it("en automático manda el número que /resolve eligió para ESTA canción (no el género)", () => {
    const pin = makeLookResolvedPin(RESOLVED, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(lookVariantSubmitFields({ ...base, lookResolved: pin })).toEqual({ lyric_variant: 3 });
    // El operador gana sobre el pin.
    expect(lookVariantSubmitFields({ ...base, lookResolved: pin, lyricVariant: 7, lyricGenre: "punk" }))
      .toEqual({ lyric_genre: "punk", lyric_variant: 7 });
  });

  it("el pin recuerda si la variante pinta su propio fondo", () => {
    expect(makeLookResolvedPin({ ...RESOLVED, background: "source" }, { artist: "A", songTitle: "B" }).background).toBe("source");
    expect(makeLookResolvedPin({ ...RESOLVED, background: "flat" }, { artist: "A", songTitle: "B" }).background).toBe("flat");
    expect(makeLookResolvedPin({ ...RESOLVED, background: "raro" }, { artist: "A", songTitle: "B" }).background).toBe("");
  });

  it("un pin de otra canción u otro look no se aplica", () => {
    const pin = makeLookResolvedPin(RESOLVED, { artist: "Otro", songTitle: "Tema" });
    expect(lookVariantSubmitFields({ ...base, lookResolved: pin })).toEqual({});
    const pin2 = makeLookResolvedPin(RESOLVED, { artist: "Almafuerte", songTitle: "Toro y Pampa" });
    expect(lookVariantSubmitFields({ ...base, lyricLook: "neon", lookResolved: pin2 })).toEqual({});
  });

  it("el pin toma la canción del pedido cuando no se le pasa", () => {
    const pin = makeLookResolvedPin({ ...RESOLVED, _request: { artist: "Almafuerte", songTitle: "Toro y Pampa" } });
    expect(effectiveLookVariantFields({ ...base, lookResolved: pin }).lyricVariant).toBe(3);
  });

  it("appendLookVariantFields escribe en el FormData", () => {
    const fd = new FormData();
    appendLookVariantFields(fd, { ...base, lyricVariant: 0, lyricAccentColor: "#ff0000" });
    expect(fd.get("lyric_variant")).toBe("0");
    expect(fd.get("lyric_accent_color")).toBe("#FF0000");
    expect(fd.has("lyric_genre")).toBe(false);
  });
});

describe("payload de /variant: absoluto", () => {
  it("con look manda los 6 campos ('' = automático)", () => {
    expect(lookVariantAbsoluteFields({ lyricLook: "cine", lyricFontKey: "metal-mania" })).toEqual({
      lyric_genre: "", lyric_variant: "", lyric_font_main: "", lyric_font_key: "metal-mania",
      lyric_alt_color: "", lyric_accent_color: "",
    });
  });
  it("sin look todo vacío aunque haya overrides viejos", () => {
    expect(Object.values(lookVariantAbsoluteFields({ lyricLook: "", lyricFontKey: "x", lyricVariant: 3 })))
      .toEqual(["", "", "", "", "", ""]);
  });
});

describe("render_params → campos", () => {
  it("normaliza valores inválidos a automático", () => {
    expect(lookVariantFieldsFromParams({
      lyric_genre: "METAL", lyric_variant: "5", lyric_font_main: "rubik-dirt",
      lyric_alt_color: "#abcdef", lyric_accent_color: "rojo",
    })).toEqual({
      lyricGenre: "metal", lyricVariant: 5, lyricFontMain: "rubik-dirt", lyricFontKey: "",
      lyricAltColor: "#ABCDEF", lyricAccentColor: "",
    });
    expect(lookVariantFieldsFromParams({ lyric_genre: "jazz", lyric_variant: -1 }).lyricGenre).toBe("");
    expect(lookVariantFieldsFromParams({ lyric_variant: -1 }).lyricVariant).toBe("");
  });
});

describe("body de /resolve", () => {
  it("el blanco default del texto viaja como '' (= el de la variante)", () => {
    const body = buildResolveBody({ look: "cinetico", color: "#ffffff", jobId: "j1", artist: " A ", songTitle: "T" });
    expect(body).toMatchObject({ lyric_look: "cinetico", lyric_color: "", job_id: "j1", artist: "A", song_title: "T", lyric_variant: "" });
    expect(buildResolveBody({ look: "cinetico", color: "#112233" }).lyric_color).toBe("#112233");
  });
  it("sólo low_contrast_* cuentan como warnings de contraste", () => {
    expect(contrastWarnings({ warnings: ["low_contrast_ink", "other"] })).toEqual(["low_contrast_ink"]);
    expect(contrastWarnings(null)).toEqual([]);
  });
});

describe("preview: look con la variante aplicada", () => {
  const cinetico = getLyricLook("cinetico");

  it("sin nada que aplicar devuelve el mismo look", () => {
    expect(lookWithVariant(cinetico, {})).toBe(cinetico);
  });

  it("resolved: fuentes y colores resueltos; fondo 'source' saca las tarjetas", () => {
    const out = lookWithVariant(cinetico, { resolved: RESOLVED });
    expect(out.font.css).toContain("Rubik Dirt");
    expect(out.preview.keyFont.css).toContain("Metal Mania");
    expect(out.preview.color).toBe("#F2EEE6");
    expect(out.preview.accent).toBe("#D0101E");
    expect(out.preview.cards).toBeUndefined();
    expect(out.preview.cardPalettes).toEqual([["#F2EEE6", "#CFC9BE", "#D0101E"]]);
    // No muta el catálogo.
    expect(cinetico.preview.cards).toBeTruthy();
  });

  it("un resolved de otro look se ignora", () => {
    expect(lookWithVariant(getLyricLook("cine"), { resolved: RESOLVED })).toBe(getLyricLook("cine"));
  });

  it("sin resolved aplica los overrides crudos del catálogo", () => {
    const out = lookWithVariant(getLyricLook("cine"), { fontMain: "pirata-one", accentColor: "#00ff00" });
    expect(out.font.css).toContain("Pirata One");
    expect(out.preview.accent).toBe("#00FF00");
  });
});

describe("chat: roles de color iguales al render", () => {
  it("Texto = recibido, Secundario = su globo, Destacado = globo enviado con texto legible", async () => {
    const { getLyricLook } = await import("./lyricLooks.js");
    const { lookWithVariant } = await import("./lyricLookVariants.js");
    const chat = getLyricLook("chat");
    const out = lookWithVariant(chat, {
      resolved: { look: "chat", colors: { ink: "#222222", alt: "#DDEEFF", accent: "#FFE680" }, fonts: {} },
    });
    expect(out.preview.color).toBe("#222222");
    expect(out.preview.bubbles).toEqual(["#DDEEFF", "#FFE680"]);
    expect(out.preview.accent).toBe("#111111");
    const dark = lookWithVariant(chat, { resolved: { look: "chat", colors: { ink: "#111111", alt: "#E9E9EB", accent: "#0A3D91" }, fonts: {} } });
    expect(dark.preview.accent).toBe("#FFFFFF");
  });
});
