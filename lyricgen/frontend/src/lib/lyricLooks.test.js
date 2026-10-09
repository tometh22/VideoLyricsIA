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
  lookOwnsBackground,
  isFunctionWord,
  kineticRows,
  blockRows,
  handWordTilt,
} from "./lyricLooks.js";

const tNull = () => null;

describe("catálogo", () => {
  it("expone exactamente los códigos que acepta el backend, con sin-look primero", () => {
    expect(LYRIC_LOOK_CODES).toEqual([
      "", "cosmico", "cine", "pincel", "pop70", "pelicula",
      "cinetico", "neon", "chat", "cuaderno", "bloque", "arco", "perspectiva",
      "duotono", "romantico", "degrade", "y2k",
    ]);
    expect(lyricLookOptions(tNull).map((o) => o.code)).toEqual(LYRIC_LOOK_CODES);
  });

  it("cada look tiene label, descripción, fuente y datos de preview", () => {
    for (const o of lyricLookOptions(tNull)) {
      expect(o.label, o.code).toBeTruthy();
      expect(o.desc, o.code).toBeTruthy();
      if (!o.code) continue;
      expect(o.font.css, o.code).toMatch(/'/);
      expect(o.preview.color, o.code).toMatch(/^#[0-9A-F]{6}$/i);
      expect(["line", "keyword", "build", "kinetic", "neon", "chat", "block", "arc", "floor"]).toContain(o.preview.layout);
      expect(o.preview.thumbBg, o.code).toBeTruthy();
    }
  });

  it("las fuentes de los looks son las que preparó el backend", () => {
    const fam = Object.fromEntries(lyricLookOptions(tNull).filter((o) => o.code).map((o) => [o.code, o.font.css]));
    expect(fam.cosmico).toContain("Audiowide");
    expect(fam.cine).toContain("Marcellus");
    expect(fam.pincel).toContain("Knewave");
    expect(fam.pop70).toContain("Shrikhand");
    expect(fam.pelicula).toContain("Oswald");
    expect(fam.cinetico).toContain("Big Shoulders Display");
    expect(getLyricLook("cinetico").preview.scriptFont.css).toContain("Caveat");
    expect(fam.neon).toContain("Tilt Neon");
    expect(getLyricLook("neon").preview.scriptFont.css).toContain("Neonderthaw");
    expect(fam.chat).toContain("Roboto");
    expect(fam.cuaderno).toContain("Caveat");
    expect(fam.bloque).toContain("Big Shoulders Display");
    expect(fam.arco).toContain("Montserrat");
    expect(fam.perspectiva).toContain("Montserrat");
    expect(fam.duotono).toContain("Permanent Marker");
    expect(fam.romantico).toContain("Sacramento");
    expect(fam.degrade).toContain("Oswald");
    expect(fam.y2k).toContain("Michroma");
  });

  it("los colores de los looks nuevos espejan el backend", () => {
    const p = (code) => getLyricLook(code).preview;
    expect(p("cinetico").cards).toEqual(["#1F2244", "#86C8EE"]);
    expect(p("cinetico").cardPalettes).toEqual([["#FFFFFF", "#6EC6FF", "#FF7A45"], ["#1F2244", "#FFFFFF", "#E63946"]]);
    expect(p("neon").lineColors).toEqual(["#FF3EA5", "#2EE6FF", "#B07BFF", "#FFB13B"]);
    expect(p("chat").bubbles).toEqual(["#E9E9EB", "#1F8BFF"]);
    expect(p("cuaderno").circleKey).toBe("#E8322E");
    expect(p("bloque").accent).toBe("#FF6B5B");
    expect(p("arco").accent).toBe("#FFD23F");
    expect(p("perspectiva").accent).toBe("#7CF5D6");
    expect(p("duotono").lineColors).toEqual(["#FFF7E6", "#FFD23F", "#FF8FB1", "#9EF0C8"]);
    expect(p("degrade").gradient).toEqual(["#9D4EDD", "#C850C0", "#FF4F8B"]);
    expect(p("y2k").stroke).toContain("#5ED8FF");
  });

  it("chat, cuaderno y romántico conservan el case original de la letra", () => {
    const keep = lyricLookOptions(tNull).filter((o) => o.preview?.forceCase === "original").map((o) => o.code);
    expect(keep).toEqual(["chat", "cuaderno", "romantico"]);
  });

  it("las descripciones de Cinético y Degradé avisan que no usan el fondo", () => {
    const desc = (code) => lyricLookOptions(tNull).find((o) => o.code === code).desc;
    expect(desc("cinetico")).toMatch(/No usa el fondo/);
    expect(desc("degrade")).toMatch(/No usa el fondo/);
  });

  it("Pop 70s reemplaza el fondo por tarjetas de color con acento por tarjeta", () => {
    const p = getLyricLook("pop70").preview;
    expect(p.cards).toEqual(["#EE5FA0", "#6E8EDB", "#8FCB6B", "#F4ECDD"]);
    expect(p.cardAccents).toEqual(["#FFD23F", "#FFD23F", "#EE5FA0", "#6E8EDB"]);
    // La descripción avisa que no usa el fondo.
    expect(lyricLookOptions(tNull).find((o) => o.code === "pop70").desc).toMatch(/No usa el fondo/);
  });

  it("Cine y Romántico traen franjas; los demás no", () => {
    expect(getLyricLook("cine").preview.letterbox).toBe(true);
    expect(getLyricLook("romantico").preview.letterbox).toBe(true);
    for (const code of LYRIC_LOOK_CODES.filter((c) => c && c !== "cine" && c !== "romantico")) {
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
    expect(normalizeLyricLook("vaporwave")).toBe("");
    expect(normalizeLyricLook(null)).toBe("");
    expect(normalizeLyricLook(undefined)).toBe("");
    expect(normalizeLyricLook("constructor")).toBe("");
    expect(getLyricLook("")).toBeNull();
    expect(getLyricLook("vaporwave")).toBeNull();
    expect(getLyricLook("pincel").code).toBe("pincel");
  });

  it("cada código nuevo normaliza (también con mayúsculas y espacios)", () => {
    for (const code of ["cinetico", "neon", "chat", "cuaderno", "bloque", "arco", "perspectiva", "duotono", "romantico", "degrade", "y2k"]) {
      expect(normalizeLyricLook(` ${code.toUpperCase()} `)).toBe(code);
      expect(getLyricLook(code).code).toBe(code);
    }
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
    expect(lyricColorForSubmit("vaporwave", "#FFFFFF")).toBe("#FFFFFF");
  });
});

describe("lookOwnsBackground", () => {
  it("sólo Pop 70s, Cinético y Degradé pintan todo el cuadro", () => {
    const owners = LYRIC_LOOK_CODES.filter((c) => lookOwnsBackground(c));
    expect(owners).toEqual(["pop70", "cinetico", "degrade"]);
  });

  it("acepta cualquier valor y lo normaliza", () => {
    expect(lookOwnsBackground(" Degrade ")).toBe(true);
    expect(lookOwnsBackground("")).toBe(false);
    expect(lookOwnsBackground(null)).toBe(false);
    expect(lookOwnsBackground("vaporwave")).toBe(false);
    expect(lookOwnsBackground("neon")).toBe(false);
  });
});

describe("composiciones de los looks nuevos (espejo de backend)", () => {
  it("palabras función: stopwords o ≤2 letras", () => {
    expect(isFunctionWord("the")).toBe(true);
    expect(isFunctionWord("TU")).toBe(true);
    expect(isFunctionWord("ok")).toBe(true);
    expect(isFunctionWord("corazón")).toBe(false);
  });

  it("Cinético: función en filas manuscritas juntas, clave sola, gruesas de hasta 12 letras", () => {
    const tokens = "ME AND MY FRIENDS AT THE TABLE DOING SHOTS".split(" ");
    const { rows, keyIndex } = kineticRows(tokens);
    expect(keyIndex).toBe(8);
    expect(rows.map((r) => r.idx)).toEqual([[0, 1, 2], [3], [4, 5], [6, 7], [8]]);
    expect(rows.map((r) => r.kind)).toEqual(["script", "heavy", "script", "heavy", "key"]);
    expect(rows.flatMap((r) => r.idx)).toEqual(tokens.map((_, i) => i));
  });

  it("Bloque: filas de hasta 11 letras, clave sola y gruesa, alternando fina/gruesa", () => {
    const tokens = "quiero bailar con vos toda la noche".split(" ");
    const { rows, keyIndex } = blockRows(tokens);
    expect(keyIndex).toBe(6);
    expect(rows.flatMap((r) => r.idx)).toEqual(tokens.map((_, i) => i));
    expect(rows.at(-1)).toMatchObject({ idx: [6], key: true, heavy: true });
    expect(rows[0].heavy).toBe(false);
    expect(rows[1].heavy).toBe(true);
    for (const r of rows) expect(r.idx.map((i) => tokens[i]).join(" ").length <= 11 || r.idx.length === 1).toBe(true);
    // Una línea de una sola fila va gruesa.
    expect(blockRows(["hola"]).rows).toEqual([{ idx: [0], key: false, heavy: true }]);
  });

  it("Cuaderno: inclinación por palabra determinística y acotada", () => {
    expect(handWordTilt(0, 0, 3)).toBe(-3);
    expect(handWordTilt(1, 0, 3)).toBe(handWordTilt(1, 0, 3));
    for (let i = 0; i < 12; i += 1) expect(Math.abs(handWordTilt(i, 5, 3))).toBeLessThanOrEqual(3);
  });
});
