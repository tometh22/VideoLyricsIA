import { describe, it, expect } from "vitest";
import { anchorLyricsForEntry } from "./anchorPayload";

describe("anchorLyricsForEntry", () => {
  it("official + texto → devuelve el texto trimmeado", () => {
    expect(anchorLyricsForEntry({
      lyricsSource: "official",
      anchorLyrics: "  linea uno\nlinea dos\nlinea tres  ",
    })).toBe("linea uno\nlinea dos\nlinea tres");
  });

  it("official pero textarea vacío → \"\"", () => {
    expect(anchorLyricsForEntry({ lyricsSource: "official", anchorLyrics: "   " })).toBe("");
    expect(anchorLyricsForEntry({ lyricsSource: "official" })).toBe("");
  });

  it("fuente IA (default) → \"\" aunque haya texto pegado", () => {
    expect(anchorLyricsForEntry({ lyricsSource: "auto", anchorLyrics: "linea uno" })).toBe("");
    expect(anchorLyricsForEntry({ anchorLyrics: "linea uno" })).toBe("");
  });

  it("entry nula → \"\"", () => {
    expect(anchorLyricsForEntry(null)).toBe("");
    expect(anchorLyricsForEntry(undefined)).toBe("");
  });
});
