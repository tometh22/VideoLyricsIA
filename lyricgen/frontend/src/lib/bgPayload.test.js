/**
 * Regresión del bug Ana M. (2026-06-09): 3 audios subidos a la vez
 * salieron con la imagen custom de un video anterior porque App mandaba
 * background_file siempre que estuviera seteado, ignorando que el tab
 * visible del wizard era "Generar con IA". La invariante que fija este
 * archivo: el payload de fondo refleja EXCLUSIVAMENTE el tab activo.
 */
import { describe, it, expect } from "vitest";
import { appendBackgroundFields } from "./bgPayload";

const file = new File(["x"], "mi-foto.png", { type: "image/png" });

function build(state) {
  const body = new FormData();
  appendBackgroundFields(body, state);
  return body;
}

describe("appendBackgroundFields", () => {
  it("modo auto NO manda fondo aunque haya backgroundFile stale de otro batch", () => {
    const body = build({
      bgSelectMode: "auto",
      backgroundFile: file,
      backgroundId: null,
      backgroundMode: "as_is",
      animateImage: true,
    });
    expect(body.has("background_file")).toBe(false);
    expect(body.has("background_id")).toBe(false);
    expect(body.has("animate_image")).toBe(false);
  });

  it("modo auto NO manda fondo aunque haya backgroundId stale", () => {
    const body = build({
      bgSelectMode: "auto",
      backgroundFile: null,
      backgroundId: 42,
      backgroundMode: "as_is",
      animateImage: false,
    });
    expect(body.has("background_id")).toBe(false);
  });

  it("modo custom manda el archivo + animate_image cuando está prendido", () => {
    const body = build({
      bgSelectMode: "custom",
      backgroundFile: file,
      backgroundId: 42, // id residual de una selección anterior: se ignora
      backgroundMode: "as_is",
      animateImage: true,
    });
    expect(body.get("background_file")).toBe(file);
    expect(body.get("animate_image")).toBe("true");
    expect(body.has("background_id")).toBe(false);
  });

  it("modo custom sin animateImage no manda animate_image", () => {
    const body = build({
      bgSelectMode: "custom",
      backgroundFile: file,
      backgroundId: null,
      backgroundMode: "as_is",
      animateImage: false,
    });
    expect(body.has("animate_image")).toBe(false);
  });

  it("modo custom sin archivo no manda nada (degrada a fondo IA)", () => {
    const body = build({
      bgSelectMode: "custom",
      backgroundFile: null,
      backgroundId: null,
      backgroundMode: "as_is",
      animateImage: true,
    });
    expect(body.has("background_file")).toBe(false);
    expect(body.has("animate_image")).toBe(false);
  });

  it("modo library manda id + mode, nunca el archivo", () => {
    const body = build({
      bgSelectMode: "library",
      backgroundFile: file, // residual: se ignora
      backgroundId: 7,
      backgroundMode: "variation",
      animateImage: true,
    });
    expect(body.get("background_id")).toBe("7");
    expect(body.get("background_mode")).toBe("variation");
    expect(body.has("background_file")).toBe(false);
    expect(body.has("animate_image")).toBe(false);
  });

  it("modo library sin id no manda nada", () => {
    const body = build({
      bgSelectMode: "library",
      backgroundFile: null,
      backgroundId: null,
      backgroundMode: "as_is",
      animateImage: false,
    });
    expect([...body.keys()]).toEqual([]);
  });
});
