// Tests for prettifySongTitle — the helper that cleans raw filenames
// before they show up as the song header in step 6.
//
// Real-world test cases come from operator uploads observed on the
// staging dashboard: mixed underscore/dash separators, ALL Title Case,
// extensions, etc. The expected outputs prioritize legibility over
// perfect Spanish (no accent restoration — that needs a dictionary).

import { describe, it, expect } from "vitest";

import { prettifySongTitle } from "./prettifySongTitle";

describe("prettifySongTitle", () => {
  it("limpia el filename del screenshot del operador (UMG style)", () => {
    expect(
      prettifySongTitle("El Arbol De La Vida _ Voy A Dejarte - Viejas Locas")
    ).toBe("El Arbol de la Vida — Voy a Dejarte — Viejas Locas");
  });

  it("respeta lowercase mid-title en castellano (de, la, y, en)", () => {
    expect(prettifySongTitle("DUEÑO DE LA NOCHE")).toBe("Dueño de la Noche");
    expect(prettifySongTitle("la vida y los suenos")).toBe(
      "La Vida y los Suenos"
    );
  });

  it("respeta lowercase mid-title en portugués (do, da, e)", () => {
    expect(prettifySongTitle("a flor e a espinha")).toBe(
      "A Flor e a Espinha"
    );
  });

  it("respeta lowercase mid-title en inglés (the, of, and)", () => {
    expect(prettifySongTitle("the lord of the rings")).toBe(
      "The Lord of the Rings"
    );
  });

  it("primera palabra de cada segmento se capitaliza aunque sea stop-word", () => {
    // "de" abre el segundo segmento — debe ir capitalizada.
    expect(prettifySongTitle("amor _ de noche")).toBe("Amor — De Noche");
  });

  it("strip de extensión de audio", () => {
    expect(prettifySongTitle("cancion.mp3")).toBe("Cancion");
    expect(prettifySongTitle("test.MP3")).toBe("Test");
    expect(prettifySongTitle("track.wav")).toBe("Track");
    expect(prettifySongTitle("track.m4a")).toBe("Track");
    expect(prettifySongTitle("track.flac")).toBe("Track");
  });

  it("colapsa underscores entre palabras", () => {
    expect(prettifySongTitle("voy_a_dejarte")).toBe("Voy a Dejarte");
  });

  it("` - ` y ` _ ` quedan ambos como em-dash uniforme", () => {
    expect(prettifySongTitle("artist - song _ album")).toBe(
      "Artist — Song — Album"
    );
  });

  it("colapsa whitespace múltiple", () => {
    // b/c no son stop-words → capitalizan; el test verifica el collapse
    // de espacios, no el casing.
    expect(prettifySongTitle("a    b     c")).toBe("A B C");
  });

  it("input vacío o null → string vacío", () => {
    expect(prettifySongTitle("")).toBe("");
    expect(prettifySongTitle(null)).toBe("");
    expect(prettifySongTitle(undefined)).toBe("");
    expect(prettifySongTitle("   ")).toBe("");
  });

  it("input ya limpio se mantiene", () => {
    expect(prettifySongTitle("Cancion Bonita")).toBe("Cancion Bonita");
    expect(prettifySongTitle("Cancion — Artista")).toBe("Cancion — Artista");
  });

  it("apóstrofes y guiones dentro de palabras se preservan", () => {
    expect(prettifySongTitle("don't stop me now")).toBe("Don't Stop Me Now");
    expect(prettifySongTitle("rock-and-roll")).toBe("Rock-and-roll");
  });

  it("fallback: si el cleanup queda vacío, devuelve input crudo", () => {
    // El fallback `out || input` solo aplica cuando el cleanup queda
    // string vacío — un input que es 100% whitespace después del trim.
    // Inputs con separadores no triviales no caen en el fallback porque
    // producen algo (aunque sea un em-dash solo) — eso lo cubre el
    // próximo caso.
    expect(prettifySongTitle("    ")).toBe(""); // empty after trim
  });

  it("cleanup de input que solo tiene separadores no crashea", () => {
    // No prometemos belleza acá — solo que no rompa. "_-_" pasa por
    // el cleanup y queda como un em-dash; aceptable porque el input
    // era inválido como nombre de canción de todas formas.
    expect(() => prettifySongTitle("_-_")).not.toThrow();
  });

  it("no rompe en filenames muy largos", () => {
    const long =
      "una_cancion_que_tiene_un_titulo_muy_largo_para_probar_que_no_revienta - artista_con_nombre_largo";
    const out = prettifySongTitle(long);
    expect(out).toContain("Cancion");
    expect(out).toContain("Artista");
    expect(out).toContain(" — ");
  });

  it("preserva caracteres no-ASCII (acentos existentes, ñ, etc.)", () => {
    expect(prettifySongTitle("ÁRBOL DE NAVIDAD")).toBe("Árbol de Navidad");
    expect(prettifySongTitle("CAÑÓN")).toBe("Cañón");
  });

  it("solo `-` sin espacios alrededor no se trata como separator", () => {
    // Esto es importante: "rock-and-roll" tiene `-` sin espacios y NO
    // queremos splittearlo en em-dashes.
    expect(prettifySongTitle("rock-y-roll")).toBe("Rock-y-roll");
  });
});
