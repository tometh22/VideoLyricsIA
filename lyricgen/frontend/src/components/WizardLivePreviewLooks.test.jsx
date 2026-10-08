// Preview de los looks de letra (lib/lyricLooks): cada look tiene que
// comunicarse en el preview — composición, color, fondo y franjas — tanto en
// el loop de muestra como con el audio en vivo del editor.

import { render, cleanup, act, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { useRef } from "react";
import WizardLivePreview, { lookLineMotionStyle, LOOK_LOOP_S } from "./WizardLivePreview";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const renderLook = (props) => render(
  <WizardLivePreview style="oscuro" movementStyle="estatico" effect="" {...props} />,
);

function LiveHarness({ refOut, ...props }) {
  const ref = useRef({ activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0 });
  refOut.current = ref;
  return <WizardLivePreview style="oscuro" movementStyle="estatico" effect="" playbackTickRef={ref} {...props} />;
}

describe("WizardLivePreview — looks de letra", () => {
  it("sin look renderiza la capa de letra de siempre", () => {
    const { container } = renderLook({});
    expect(container.querySelector("[data-lyric-look]")).toBeNull();
    expect(container.querySelector('[data-testid="look-lyric"]')).toBeNull();
  });

  it("Cine: línea chica arriba + palabra clave grande, franjas y fuente Marcellus", () => {
    const { container } = renderLook({ lyricLook: "cine" });
    const layer = container.querySelector('[data-lyric-look="cine"]');
    expect(layer).not.toBeNull();
    const key = layer.querySelector('[data-look-key="true"]');
    expect(key.textContent).toBe("LETRA");
    expect(layer.textContent).toContain("ESTA ES TU");
    const lyric = container.querySelector('[data-testid="look-lyric"]');
    expect(lyric.style.fontFamily).toContain("Marcellus");
    // Color propio del look (blanco cálido) — el blanco default no lo pisa.
    expect(lyric.style.color).toBe("rgb(246, 238, 226)");
    // Clave más grande que la línea de arriba.
    const [lead, big] = layer.querySelectorAll('[data-testid="look-lyric"] span');
    expect(parseFloat(big.style.fontSize)).toBeGreaterThan(parseFloat(lead.style.fontSize) * 2);
    // Franjas 2.39:1 aunque el formato sea "full".
    const bars = [...container.querySelectorAll("div")].filter((d) => d.style.height === "12.8%");
    expect(bars).toHaveLength(2);
  });

  it("un color elegido por el operador pisa el color del look", () => {
    const { container } = renderLook({ lyricLook: "pelicula", lyricColor: "#FF0000" });
    expect(container.querySelector('[data-testid="look-lyric"]').style.color).toBe("rgb(255, 0, 0)");
  });

  it("Pop 70s: tarjeta de color en vez del fondo, que cambia en cada ciclo", () => {
    vi.useFakeTimers();
    const { container } = renderLook({ lyricLook: "pop70" });
    const card = () => container.querySelector('[data-look-layer="card"]');
    expect(card().getAttribute("data-look-card")).toBe("#EE5FA0");
    // La palabra clave usa el acento de la tarjeta.
    expect(container.querySelector('[data-look-key="true"]').style.color).toBe("rgb(255, 210, 63)");
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000); });
    expect(card().getAttribute("data-look-card")).toBe("#6E8EDB");
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000 * 2); });
    expect(card().getAttribute("data-look-card")).toBe("#F4ECDD");
    expect(container.querySelector('[data-look-key="true"]').style.color).toBe("rgb(110, 142, 219)");
  });

  it("Pop 70s: filas escalonadas a izquierda y derecha", () => {
    const { container } = renderLook({ lyricLook: "pop70" });
    const rows = [...container.querySelectorAll("[data-look-row]")];
    expect(rows.length).toBeGreaterThan(1);
    expect(rows[0].style.transform).toMatch(/translateX\(-/);
    expect(rows[1].style.transform).toMatch(/translateX\(\d/);
  });

  it("Pincel: grano propio salvo que el operador elija un efecto", () => {
    const { container, rerender } = renderLook({ lyricLook: "pincel" });
    expect(container.querySelector('[data-look-layer="grain"]')).not.toBeNull();
    rerender(<WizardLivePreview style="oscuro" movementStyle="estatico" effect="snow" lyricLook="pincel" />);
    expect(container.querySelector('[data-look-layer="grain"]')).toBeNull();
  });

  it("respeta el textCase elegido", () => {
    const { container } = renderLook({ lyricLook: "cosmico", textCase: "lower" });
    expect(container.querySelector('[data-testid="look-lyric"]').textContent).toBe("esta es tu letra");
  });

  it("el caption nombra el look", () => {
    const { container } = renderLook({ lyricLook: "cosmico" });
    expect(container.textContent).toContain("Look: Cósmico");
  });

  it("en vivo, Pincel arma la línea palabra por palabra al ritmo del canto", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="pincel" textCase="original" />);
    await act(async () => {
      refOut.current.current = {
        activeLine: "pinta todo de negro",
        activeStart: 10,
        activeEnd: 14,
        currentTime: 11.2,
        words: [{ start: 10.2 }, { start: 11.0 }, { start: 12.0 }, { start: 13.0 }],
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("negro"));
    const word = (i) => container.querySelector(`[data-look-word="${i}"]`);
    expect(word(0).style.opacity).toBe("1");
    expect(word(1).style.opacity).toBe("1");
    expect(word(2).style.opacity).toBe("0");
    expect(word(3).style.opacity).toBe("0");
    // "negro" es la clave: va en rojo.
    expect(word(3).getAttribute("data-look-key")).toBe("true");
    expect(word(3).style.color).toBe("rgb(227, 38, 47)");
  });
});

describe("lookLineMotionStyle (modo en vivo)", () => {
  it("Cósmico: llega chico, se asienta y pasa de largo al final de SU ventana", () => {
    expect(lookLineMotionStyle("zoom_through", 0, 3).transform).toBe("scale(0.300)");
    expect(lookLineMotionStyle("zoom_through", 1.5, 3)).toMatchObject({ opacity: 1 });
    const end = lookLineMotionStyle("zoom_through", 3, 3);
    expect(end.opacity).toBeCloseTo(0, 6);
    expect(parseFloat(end.transform.slice(6))).toBeGreaterThan(8);
  });

  it("Cine: entra desenfocado y con letras abiertas que se cierran", () => {
    const start = lookLineMotionStyle("cine", 0, 3);
    expect(start.opacity).toBe(0);
    expect(start.filter).toBe("blur(10.00px)");
    expect(start.letterSpacing).toBe("0.220em");
    expect(lookLineMotionStyle("cine", 1.5, 3)).toMatchObject({ opacity: 1, letterSpacing: "0.03em" });
  });

  it("Película: fundido corto", () => {
    expect(lookLineMotionStyle("fade", 0, 3).opacity).toBe(0);
    expect(lookLineMotionStyle("fade", 1, 3).opacity).toBe(1);
    expect(lookLineMotionStyle("fade", 3, 3).opacity).toBe(0);
  });
});
