import { render, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  EditorialPreview,
  EDITORIAL_LAYOUTS,
  editorialStack,
  editorialMeasure,
} from "./editorialLookLayouts";
import { getLyricLook, lookOwnsBackground } from "../lib/lyricLooks";
import { lookWithVariant, buildResolveBody } from "../lib/lyricLookVariants";

afterEach(cleanup);
const words = [
  { word: "sigue", start: 1.2, end: 1.6 },
  { word: "vivo", start: 2.3, end: 3 },
];
function context(code, time = 1.8) {
  const look = getLyricLook(code);
  return {
    look,
    lp: look.preview,
    tokens: ["sigue", "vivo"],
    text: "sigue vivo",
    lineIdx: 0,
    textColor: look.preview.color,
    sungIdx: time >= 2.3 ? 1 : 0,
    fontScale: 1,
    uid: "test",
    live: { words, activeStart: 1, activeEnd: 3, currentTime: time },
    elapsed: time - 1,
    dur: 2,
  };
}

describe("looks editoriales aprobados", () => {
  it.each(EDITORIAL_LAYOUTS)(
    "%s aparece en el catálogo y conserva el fondo",
    (code) => {
      expect(buildResolveBody({ look: code }).lyric_look).toBe(code);
      expect(lookOwnsBackground(code)).toBe(false);
      const { container } = render(<EditorialPreview ctx={context(code)} />);
      expect(
        container.querySelector(`[data-editorial-look="${code}"]`),
      ).not.toBeNull();
      expect(
        container
          .querySelector('[data-look-word="1"]')
          .getAttribute("data-word-visible"),
      ).toBe("false");
      expect(
        container
          .querySelector('[data-look-word="0"]')
          .getAttribute("data-word-visible"),
      ).toBe("true");
      const fullFrame = [...container.querySelectorAll("rect")].filter(
        (r) =>
          r.getAttribute("width") === "1280" &&
          r.getAttribute("height") === "720",
      );
      expect(fullFrame).toHaveLength(0);
    },
  );
  it.each(["golpe", "nube", "diptico"])(
    "%s reemplaza la palabra al llegar la siguiente",
    (code) => {
      const { container, rerender } = render(
        <EditorialPreview ctx={context(code, 1.8)} />,
      );
      rerender(<EditorialPreview ctx={context(code, 2.4)} />);
      const hero = [...container.querySelectorAll('[data-look-word="1"]')];
      expect(
        hero.some((n) => n.getAttribute("data-word-visible") === "true"),
      ).toBe(true);
      if (code !== "diptico")
        expect(
          [...container.querySelectorAll('[data-look-word="0"]')].every(
            (n) => n.getAttribute("data-word-visible") === "false",
          ),
        ).toBe(true);
    },
  );
  it("la palabra futura no aparece durante el lead-in", () => {
    const { container } = render(
      <EditorialPreview ctx={context("nube", 1.05)} />,
    );
    expect(
      [...container.querySelectorAll("[data-look-word]")].every(
        (n) => n.getAttribute("data-word-visible") === "false",
      ),
    ).toBe(true);
  });
  it("Puntada usa altura visible y un espacio compacto entre filas", () => {
    const look = getLyricLook("puntada"),
      tokens = "el que yo conocí como el jefe".split(" ");
    const g = editorialStack(
      tokens,
      look.font,
      look.font,
      720,
      405,
      102,
      1.4,
      true,
    );
    expect(g.rows).toHaveLength(3);
    g.rows.forEach((row, r) => {
      const ink = Math.max(
        ...row.map(
          (i) => editorialMeasure(tokens[i], g.sizes[i], look.font).height,
        ),
      );
      expect(g.heights[r] - ink).toBeCloseTo(18);
    });
    expect(g.heights.reduce((a, b) => a + b, 0)).toBeLessThan(270);
  });
  it("la variante del operador cambia la fuente y el color usados por la palabra clave", () => {
    const base = getLyricLook("nube");
    const look = lookWithVariant(base, {
      fontKey: "bungee",
      accentColor: "#1234AB",
    });
    const ctx = { ...context("nube", 2.5), look, lp: look.preview };
    const { container } = render(<EditorialPreview ctx={ctx} />);
    const key = container.querySelector('[data-look-word="1"] text');
    expect(key.getAttribute("font-family")).toContain("Bungee");
    expect(key.getAttribute("fill")).toBe("#1234AB");
  });
});
