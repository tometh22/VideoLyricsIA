// Catálogo de looks de letra: contrato con backend/lyric_looks.py.
import { describe, it, expect } from "vitest";
import {
  LYRIC_LOOK_CODES,
  LOOK_LOCKED_FIELDS,
  normalizeLyricLook,
  getLyricLook,
  isLockedByLook,
  lyricLookOptions,
  lyricLookLabel,
  pickKeyword,
  keywordSplit,
  buildRows,
  lyricColorForSubmit,
  lookLockedNote,
} from "./lyricLooks.js";

const tNull = () => null;

describe("catálogo", () => {
  it("expone exactamente los códigos que acepta el backend, con sin-look primero", () => {
    expect(LYRIC_LOOK_CODES).toEqual(["", "cosmico", "cine", "pincel", "pop70", "pelicula"]);
    expect(lyricLookOptions(tNull).map((o) => o.code)).toEqual(LYRIC_LOOK_CODES);
  });

  it("cada look tiene label, descripción, fuente y datos de preview", () => {
    for (const o of lyricLookOptions(tNull)) {
      expect(o.label, o.code).toBeTruthy();
      expect(o.desc, o.code).toBeTruthy();
      if (!o.code) continue;
      expect(o.font.css, o.code).toMatch(/'/);
      expect(o.preview.color, o.code).toMatch(/^#[0-9A-F]{6}$/i);
      expect(["line", "keyword", "build"]).toContain(o.preview.layout);
    }
  });

  it("las fuentes de los looks son las que preparó el backend", () => {
    const fam = Object.fromEntries(lyricLookOptions(tNull).filter((o) => o.code).map((o) => [o.code, o.font.css]));
    expect(fam.cosmico).toContain("Audiowide");
    expect(fam.cine).toContain("Marcellus");
    expect(fam.pincel).toContain("Knewave");
    expect(fam.pop70).toContain("Shrikhand");
    expect(fam.pelicula).toContain("Oswald");
  });

  it("Pop 70s reemplaza el fondo por tarjetas de color con acento por tarjeta", () => {
    const p = getLyricLook("pop70").preview;
    expect(p.cards).toEqual(["#EE5FA0", "#6E8EDB", "#8FCB6B", "#F4ECDD"]);
    expect(p.cardAccents).toEqual(["#FFD23F", "#FFD23F", "#EE5FA0", "#6E8EDB"]);
    // La descripción avisa que no usa el fondo.
    expect(lyricLookOptions(tNull).find((o) => o.code === "pop70").desc).toMatch(/No usa el fondo/);
  });

  it("Cine trae franjas; los demás no", () => {
    expect(getLyricLook("cine").preview.letterbox).toBe(true);
    for (const code of ["cosmico", "pincel", "pop70", "pelicula"]) {
      expect(getLyricLook(code).preview.letterbox).toBeFalsy();
    }
  });

  it("usa el t() cuando hay traducción", () => {
    const t = (k) => (k === "upload.look_cine" ? "Cinema" : null);
    expect(lyricLookLabel(t, "cine")).toBe("Cinema");
    expect(lyricLookLabel(tNull, "")).toBe("Sin look");
  });
});

describe("lookLockedNote", () => {
  it("interpola el nombre del look con el fallback y con la traducción", () => {
    expect(lookLockedNote(tNull, "cine")).toBe("Definido por el look «Cine». Elegí «Sin look» para personalizarlo.");
    const t = (k) => ({ "upload.look_locked_note": "Set by «{look}».", "upload.look_pop70": "70s Pop" }[k]);
    expect(lookLockedNote(t, "pop70")).toBe("Set by «70s Pop».");
  });
});

describe("normalizeLyricLook / getLyricLook", () => {
  it("baja a '' lo desconocido, igual que el backend", () => {
    expect(normalizeLyricLook("CINE ")).toBe("cine");
    expect(normalizeLyricLook("neon")).toBe("");
    expect(normalizeLyricLook(null)).toBe("");
    expect(normalizeLyricLook(undefined)).toBe("");
    expect(normalizeLyricLook("constructor")).toBe("");
    expect(getLyricLook("")).toBeNull();
    expect(getLyricLook("neon")).toBeNull();
    expect(getLyricLook("pincel").code).toBe("pincel");
  });

  it("un look bloquea font/animación/transición y nada más", () => {
    expect(LOOK_LOCKED_FIELDS).toEqual(["font", "lyricsAnimation", "lineTransition"]);
    expect(isLockedByLook("cine", "font")).toBe(true);
    expect(isLockedByLook("cine", "fontScale")).toBe(false);
    expect(isLockedByLook("cine", "textCase")).toBe(false);
    expect(isLockedByLook("cine", "effect")).toBe(false);
    expect(isLockedByLook("", "font")).toBe(false);
  });
});

describe("palabra clave (espejo de lyric_looks.pick_keyword)", () => {
  it("toma la ÚLTIMA palabra de contenido", () => {
    expect(pickKeyword("ESTA ES TU LETRA".split(" "))).toBe(3);
    expect(pickKeyword("quiero bailar con vos".split(" "))).toBe(1);
  });

  it("ignora stopwords y palabras de menos de 4 letras (incluye puntuación)", () => {
    expect(pickKeyword(["oh", "yeah", "la", "na"])).toBeNull();
    expect(pickKeyword(["corazón,", "y", "más"])).toBe(0);
  });

  it("Cine: ≤2 palabras van enteras en grande", () => {
    expect(keywordSplit(["HOLA", "MUNDO"])).toEqual({ lead: "", key: "HOLA MUNDO" });
  });

  it("Cine: línea chica arriba + clave grande sólo si la clave es la última", () => {
    expect(keywordSplit("ESTA ES TU LETRA".split(" "))).toEqual({ lead: "ESTA ES TU", key: "LETRA" });
    // Clave en el medio → una fila calma a tamaño base.
    expect(keywordSplit("bailar con vos".split(" "))).toBeNull();
  });
});

describe("filas de la composición build", () => {
  it("la palabra clave va en su propia fila cuando la línea es larga", () => {
    const tokens = "voy a pintar todo de negro".split(" ");
    const { rows, keyIndex } = buildRows(tokens, { baseFontPx: 85, keyScale: 1.45, rowWidth: 0.62, glyphWidth: 0.62 });
    expect(keyIndex).toBe(5);
    expect(rows[rows.length - 1]).toEqual([5]);
    expect(rows.flat()).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("parte las filas por ancho", () => {
    const tokens = "uno dos tres cuatro cinco seis siete ocho".split(" ");
    const { rows } = buildRows(tokens, { baseFontPx: 120, rowWidth: 0.3, glyphWidth: 0.7 });
    expect(rows.length).toBeGreaterThan(1);
    expect(rows.flat()).toEqual(tokens.map((_, i) => i));
  });
});

describe("lyricColorForSubmit", () => {
  it("con look, el blanco default viaja vacío para no pisar el color del look", () => {
    expect(lyricColorForSubmit("pelicula", "#FFFFFF")).toBe("");
    expect(lyricColorForSubmit("pelicula", "#ffffff")).toBe("");
    expect(lyricColorForSubmit("pelicula", undefined)).toBe("");
  });

  it("un color elegido pisa el del look", () => {
    expect(lyricColorForSubmit("pelicula", "#FF0000")).toBe("#FF0000");
  });

  it("sin look se mantiene el contrato histórico", () => {
    expect(lyricColorForSubmit("", "#FFFFFF")).toBe("#FFFFFF");
    expect(lyricColorForSubmit("", undefined)).toBe("#FFFFFF");
    expect(lyricColorForSubmit("neon", "#FFFFFF")).toBe("#FFFFFF");
  });
});
