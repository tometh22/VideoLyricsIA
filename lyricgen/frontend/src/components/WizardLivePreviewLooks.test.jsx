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

const NEW_LOOKS = ["cinetico", "neon", "chat", "cuaderno", "bloque", "arco", "perspectiva", "duotono", "romantico", "degrade", "y2k"];

async function playLine(refOut, tick) {
  await act(async () => {
    refOut.current.current = tick;
    await new Promise((res) => setTimeout(res, 80));
  });
}

const LIVE_TICK = {
  activeLine: "me and my friends at the table doing shots",
  activeStart: 10,
  activeEnd: 14,
  currentTime: 11.5,
  words: [10.1, 10.4, 10.7, 11.0, 11.3, 11.6, 11.9, 12.3, 12.8].map((start) => ({ start })),
};

describe("WizardLivePreview — looks nuevos", () => {
  it.each(NEW_LOOKS)("%s renderiza en el loop de muestra y nombra el look", (code) => {
    const { container } = renderLook({ lyricLook: code });
    expect(container.querySelector(`[data-lyric-look="${code}"]`)).not.toBeNull();
    expect(container.querySelector('[data-testid="look-lyric"]')).not.toBeNull();
    expect(container.textContent).toMatch(/Look: /);
  });

  it.each(NEW_LOOKS)("%s renderiza en vivo con la línea real", async (code) => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook={code} textCase="upper" />);
    await playLine(refOut, LIVE_TICK);
    await waitFor(() => expect(container.querySelector('[data-testid="look-lyric"]').textContent.toLowerCase()).toContain("friends"));
  });

  it("Cinético: tarjeta navy/celeste que alterna, clave grande en el acento y salida barrida", () => {
    vi.useFakeTimers();
    const { container } = renderLook({ lyricLook: "cinetico" });
    const card = () => container.querySelector('[data-look-layer="card"]').getAttribute("data-look-card");
    expect(card()).toBe("#1F2244");
    const key = container.querySelector('[data-look-key="true"]');
    expect(key.textContent).toBe("LETRA");
    expect(key.style.color).toBe("rgb(255, 122, 69)");
    // Palabras función en manuscrita.
    const script = container.querySelector('[data-look-kind="script"]');
    expect(script.style.fontFamily).toContain("Caveat");
    expect(container.querySelector('[data-testid="look-lyric"]').style.animation).toContain("wlp-look-smear");
    // Grano encima de la tarjeta.
    expect(container.querySelector('[data-look-layer="grain"]')).not.toBeNull();
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000); });
    expect(card()).toBe("#86C8EE");
    expect(container.querySelector('[data-look-key="true"]').style.color).toBe("rgb(230, 57, 70)");
  });

  it("Cinético en vivo: línea larga en dos columnas y palabras que entran al cantarse", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="cinetico" textCase="upper" />);
    await playLine(refOut, { ...LIVE_TICK, currentTime: 10.8 });
    await waitFor(() => expect(container.querySelectorAll("[data-look-column]")).toHaveLength(2));
    const rows = [...container.querySelectorAll("[data-look-row]")];
    // Sólo las filas cuya primera palabra ya se cantó están visibles.
    const opacities = rows.map((r) => r.lastElementChild.style.opacity);
    expect(opacities[0]).toBe("1");
    expect(opacities.at(-1)).toBe("0");
  });

  it("Neón: tubo con color por línea, letra encendida y marco", () => {
    vi.useFakeTimers();
    const { container } = renderLook({ lyricLook: "neon" });
    const tube = () => container.querySelector("[data-look-neon]").getAttribute("data-look-neon");
    expect(tube()).toBe("#FF3EA5");
    expect(container.querySelector('[data-look-lit="true"]').style.textShadow.toUpperCase()).toContain("#FF3EA5");
    expect(container.querySelector('[data-look-frame="box"]')).not.toBeNull();
    // Fondo oscurecido.
    expect(container.querySelector('[data-testid="photo-effect-stage"]').style.filter).toMatch(/brightness\(0?\.55\)/);
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000); });
    expect(tube()).toBe("#2EE6FF");
    // Línea impar: tubo manuscrito.
    expect(container.querySelector("[data-look-neon]").style.fontFamily).toContain("Neonderthaw");
  });

  it("Chat: burbuja nueva abajo, las anteriores atenuadas y del otro lado; case original", () => {
    vi.useFakeTimers();
    const { container } = renderLook({ lyricLook: "chat", textCase: "upper" });
    let bubbles = [...container.querySelectorAll("[data-look-bubble]")];
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].textContent).toBe("esta es tu letra");
    expect(bubbles[0].dataset.lookBubble).toBe("in");
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000 * 2); });
    bubbles = [...container.querySelectorAll("[data-look-bubble]")];
    expect(bubbles).toHaveLength(3);
    expect(bubbles.map((b) => b.dataset.lookBubble)).toEqual(["in", "out", "in"]);
    expect(bubbles[0].dataset.lookOld).toBe("true");
    expect(bubbles[2].dataset.lookOld).toBeUndefined();
    expect(bubbles[1].style.background).toBe("rgb(31, 139, 255)");
  });

  it("Chat en vivo: la línea anterior queda como burbuja vieja", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="chat" />);
    await playLine(refOut, { activeLine: "Primera línea", activeStart: 1, activeEnd: 3, currentTime: 2 });
    await playLine(refOut, { activeLine: "Segunda línea", activeStart: 3, activeEnd: 5, currentTime: 4 });
    await waitFor(() => expect(container.querySelectorAll("[data-look-bubble]")).toHaveLength(2));
    const [old, cur] = container.querySelectorAll("[data-look-bubble]");
    expect(old.textContent).toBe("Primera línea");
    expect(old.dataset.lookOld).toBe("true");
    expect(cur.textContent).toBe("Segunda línea");
  });

  it("Cuaderno: manuscrita en case original, clave con círculo rojo y dibujitos", () => {
    const { container } = renderLook({ lyricLook: "cuaderno", textCase: "upper" });
    const lyric = container.querySelector('[data-testid="look-lyric"]');
    expect(lyric.style.fontFamily).toContain("Caveat");
    const key = container.querySelector('[data-look-key="true"]');
    expect(key.textContent).toBe("letra");
    expect(key.querySelector('[data-look-circle="true"] ellipse').getAttribute("stroke")).toBe("#E8322E");
    expect(container.querySelector('[data-look-doodle="heart"]')).not.toBeNull();
    expect(container.querySelector('[data-look-doodle="star"]')).not.toBeNull();
    // Cada palabra escrita a mano: revelado por clip y con su inclinación.
    expect(container.querySelector('[data-look-word="0"] span').style.animation).toContain("wlp-look-write");
    expect(container.querySelector('[data-look-word="0"]').style.transform).toMatch(/rotate\(/);
  });

  it("Cuaderno en vivo: las palabras sin cantar quedan sin escribir y el círculo espera a la clave", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="cuaderno" />);
    await playLine(refOut, {
      activeLine: "pinta todo de negro", activeStart: 10, activeEnd: 14, currentTime: 11.2,
      words: [{ start: 10.2 }, { start: 11.0 }, { start: 12.0 }, { start: 13.0 }],
    });
    await waitFor(() => expect(container.querySelector('[data-look-word="3"]')).not.toBeNull());
    expect(container.querySelector('[data-look-word="1"]').dataset.lookVisible).toBe("true");
    expect(container.querySelector('[data-look-word="2"]').dataset.lookVisible).toBe("false");
    expect(container.querySelector('[data-look-circle="true"]')).toBeNull();
    expect(container.querySelector("[data-look-doodle]")).toBeNull();
  });

  it("Bloque: filas fina/gruesa y la clave sola en coral", () => {
    const { container } = renderLook({ lyricLook: "bloque" });
    const rows = [...container.querySelectorAll("[data-look-row]")];
    expect(rows.map((r) => r.textContent)).toEqual(["ESTA ES TU", "LETRA"]);
    expect(rows[0].dataset.lookWeight).toBe("light");
    expect(rows[0].style.fontWeight).toBe("300");
    expect(rows[1].dataset.lookKey).toBe("true");
    expect(rows[1].style.color).toBe("rgb(255, 107, 91)");
    // Estiradas al mismo ancho: la fila corta va en letra más grande.
    expect(parseFloat(rows[1].style.fontSize)).toBeGreaterThan(parseFloat(rows[0].style.fontSize));
  });

  it("Arco: la frase sobre un anillo que gira y la clave grande en el medio", () => {
    const { container } = renderLook({ lyricLook: "arco" });
    const svg = container.querySelector('[data-look-arc="true"]');
    expect(svg.querySelector("circle")).not.toBeNull();
    expect(svg.querySelector("textPath").textContent).toBe("ESTA ES TU");
    const key = svg.querySelector('text[data-look-key="true"]');
    expect(key.textContent).toBe("LETRA");
    expect(key.getAttribute("fill")).toBe("#FFD23F");
    expect(svg.querySelector("g").style.animation).toContain("wlp-arc-spin");
  });

  it("Perspectiva: la letra acostada en el piso, clave en verde agua", () => {
    const { container } = renderLook({ lyricLook: "perspectiva" });
    const floor = container.querySelector('[data-look-floor="true"]');
    expect(floor.style.animation).toContain("wlp-floor");
    expect(container.querySelector('[data-look-key="true"]').style.color).toBe("rgb(124, 245, 214)");
  });

  it("Perspectiva en vivo: avanza hacia la cámara y crece", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="perspectiva" />);
    await playLine(refOut, { activeLine: "hola mundo", activeStart: 0, activeEnd: 4, currentTime: 3 });
    await waitFor(() => expect(container.querySelector('[data-look-floor="true"]')).not.toBeNull());
    expect(container.querySelector('[data-look-floor="true"]').style.transform).toMatch(/rotateX\(48deg\).*scale\(1\.\d+\)/);
  });

  it("Duotono: póster a dos tintas y tinta que cambia por línea", () => {
    vi.useFakeTimers();
    const { container } = renderLook({ lyricLook: "duotono" });
    expect(container.querySelector('[data-look-layer="duotone"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="photo-effect-stage"]').style.filter).toContain("grayscale(1)");
    expect(container.querySelector("[data-look-ink]").getAttribute("data-look-ink")).toBe("#FFF7E6");
    act(() => { vi.advanceTimersByTime(LOOK_LOOP_S * 1000); });
    expect(container.querySelector("[data-look-ink]").getAttribute("data-look-ink")).toBe("#FFD23F");
    expect(container.querySelector('[data-testid="look-lyric"]').style.animation).toContain("wlp-look-boilline");
  });

  it("Romántico: cursiva en case original que se escribe sola, con franjas", () => {
    const { container } = renderLook({ lyricLook: "romantico" });
    const lyric = container.querySelector('[data-testid="look-lyric"]');
    expect(lyric.textContent).toBe("esta es tu letra");
    expect(lyric.style.fontFamily).toContain("Sacramento");
    expect(lyric.style.animation).toContain("wlp-look-writeon");
    const bars = [...container.querySelectorAll("div")].filter((d) => d.style.height === "12.8%");
    expect(bars).toHaveLength(2);
  });

  it("Degradé: reemplaza el fondo por el atardecer con montañas", () => {
    const { container } = renderLook({ lyricLook: "degrade" });
    const stops = [...container.querySelectorAll("stop")].map((s) => s.getAttribute("stop-color"));
    expect(stops).toEqual(["#9D4EDD", "#C850C0", "#FF4F8B"]);
    expect(container.querySelectorAll("svg path").length).toBeGreaterThanOrEqual(2);
  });

  it("Y2K: ecos que convergen, filtraciones de luz y tono helado", () => {
    const { container } = renderLook({ lyricLook: "y2k" });
    expect(container.querySelectorAll('[data-look-echo="l"]')).toHaveLength(4);
    expect(container.querySelectorAll('[data-look-echo="r"]')).toHaveLength(4);
    expect(container.querySelector('[data-look-layer="light-leak"]')).not.toBeNull();
    expect(container.querySelector('[data-look-layer="tint"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="photo-effect-stage"]').style.filter).toContain("hue-rotate");
  });

  it("Y2K en vivo: los ecos aparecen sólo en palabras ya cantadas", async () => {
    const refOut = { current: null };
    const { container } = render(<LiveHarness refOut={refOut} lyricLook="y2k" />);
    await playLine(refOut, {
      activeLine: "pinta todo de negro", activeStart: 10, activeEnd: 14, currentTime: 11.2,
      words: [{ start: 10.2 }, { start: 11.0 }, { start: 12.0 }, { start: 13.0 }],
    });
    await waitFor(() => expect(container.querySelector('[data-look-word="3"]')).not.toBeNull());
    expect(container.querySelectorAll('[data-look-echo="l"]')).toHaveLength(2);
  });

  it("los looks originales no cambian: Pincel sigue sin ecos ni círculos", () => {
    const { container } = renderLook({ lyricLook: "pincel" });
    expect(container.querySelector("[data-look-echo]")).toBeNull();
    expect(container.querySelector("[data-look-circle]")).toBeNull();
    expect(container.querySelector('[data-look-word="0"]').style.animation).toContain("wlp-look-wordpop");
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

describe("lookLineMotionStyle — movimientos nuevos", () => {
  it("smear: quieto hasta el final y se barre de costado", () => {
    expect(lookLineMotionStyle("smear", 1, 3)).toMatchObject({ opacity: 1, transform: "none" });
    const end = lookLineMotionStyle("smear", 3, 3);
    expect(end.opacity).toBeCloseTo(0, 6);
    expect(end.transform).toBe("scaleX(2.300)");
  });

  it("boil: crece al entrar y se estira al salir", () => {
    expect(lookLineMotionStyle("boil", 0, 3)).toMatchObject({ opacity: 0, transform: "scale(0.700)" });
    expect(lookLineMotionStyle("boil", 1.5, 3)).toMatchObject({ opacity: 1, transform: "none" });
    expect(lookLineMotionStyle("boil", 3, 3).opacity).toBeCloseTo(0, 6);
  });

  it("write_on: se escribe de izquierda a derecha y se desvanece al final", () => {
    expect(lookLineMotionStyle("write_on", 0, 3, { textLen: 16 }).clipPath).toBe("inset(-40% 100.00% -40% -5%)");
    expect(lookLineMotionStyle("write_on", 1.5, 3, { textLen: 16 })).toMatchObject({ clipPath: "inset(-40% -5.00% -40% -5%)", opacity: 1 });
    expect(lookLineMotionStyle("write_on", 3, 3, { textLen: 16 }).opacity).toBeCloseTo(0, 6);
  });
});
