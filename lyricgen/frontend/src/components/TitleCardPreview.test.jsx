/**
 * Tests that the title-card preview is CONNECTED to the same font catalog the
 * backend renders with — i.e. it's not a hardcoded approximation:
 *   - artist always renders UPPERCASE in Montserrat (ExtraBold, weight 800)
 *   - song renders in the operator's chosen lyric font (FONT_BY_CODE), and
 *     follows the chosen textCase
 *   - changing the `font` prop changes the song's font-family live
 *
 * jsdom has no layout engine (ResizeObserver / getBoundingClientRect return
 * nothing), and the component gates its text on a measured box width, so we
 * stub both just enough to let it render. We assert wiring/fonts, NOT pixel
 * layout (the shrink/wrap fit is verified against real metrics in the backend
 * unit tests + the libass integration render).
 */
import { describe, it, expect, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import { FONT_BY_CODE } from "./fontCatalog";
import TitleCardPreview from "./TitleCardPreview";

beforeAll(() => {
  // ResizeObserver: fire once with a fixed box width so boxW > 0.
  global.ResizeObserver = class {
    constructor(cb) {
      this.cb = cb;
    }
    observe() {
      this.cb([{ contentRect: { width: 320 } }]);
    }
    disconnect() {}
  };
  // getBoundingClientRect: natural single-line width small enough to fit the
  // safe width (320*0.8=256), so no shrink → text renders at base size.
  Element.prototype.getBoundingClientRect = function () {
    return { width: 80, height: 20, top: 0, left: 0, right: 80, bottom: 20, x: 0, y: 0 };
  };
});

function songNodes(container) {
  // The song text appears in a hidden measuring <span> and a visible <div>;
  // both carry the resolved fontFamily. Return all matching nodes.
  return [...container.querySelectorAll("span, div")].filter(
    (el) => el.childNodes.length === 1 && el.textContent && !el.querySelector("*"),
  );
}

describe("TitleCardPreview — font fidelity / backend connection", () => {
  it("renders the artist UPPERCASE in Montserrat (ExtraBold)", () => {
    const { container } = render(
      <TitleCardPreview artist="Soda Stereo" song="De Música Ligera" font="oswald-bold" />,
    );
    const artist = [...container.querySelectorAll("div, span")].find(
      (el) => el.textContent === "SODA STEREO",
    );
    expect(artist).toBeTruthy();
    expect(artist.style.fontFamily).toMatch(/Montserrat/i);
    expect(artist.style.fontWeight).toBe("800");
  });

  it("renders the song in the operator's chosen font (FONT_BY_CODE)", () => {
    const { container } = render(
      <TitleCardPreview artist="X" song="Cancion" font="oswald-bold" textCase="original" />,
    );
    const expectedCss = FONT_BY_CODE["oswald-bold"].css; // 'Oswald', sans-serif
    const song = [...container.querySelectorAll("div, span")].find(
      (el) => el.textContent === "Cancion",
    );
    expect(song).toBeTruthy();
    expect(song.style.fontFamily).toContain("Oswald");
    expect(expectedCss).toContain("Oswald");
  });

  it("switches the song font-family when the font prop changes", () => {
    const { container, rerender } = render(
      <TitleCardPreview artist="X" song="Tema" font="anton" textCase="original" />,
    );
    let song = [...container.querySelectorAll("div, span")].find((el) => el.textContent === "Tema");
    expect(song.style.fontFamily).toContain("Anton");

    rerender(<TitleCardPreview artist="X" song="Tema" font="poppins-bold" textCase="original" />);
    song = [...container.querySelectorAll("div, span")].find((el) => el.textContent === "Tema");
    expect(song.style.fontFamily).toContain("Poppins");
  });

  it("applies the operator's textCase to the song", () => {
    render(<TitleCardPreview artist="X" song="grito" font="" textCase="upper" />);
    expect(screen.getAllByText("GRITO").length).toBeGreaterThan(0);
  });

  it("NFC-normalises decomposed accents (matches the render)", () => {
    // 'i' + U+0301 combining acute → precomposed "Í"
    const nfd = "Así Es El Calor";
    const { container } = render(<TitleCardPreview artist={nfd} song="x" font="" />);
    const artist = [...container.querySelectorAll("div, span")].find(
      (el) => el.textContent === "ASÍ ES EL CALOR",
    );
    expect(artist).toBeTruthy();
    expect(artist.textContent).not.toContain("́");
  });
});

/**
 * The "auto" template must resolve the SAME way the backend does
 * (ass_render.title_card_layout): a long instrumental intro (first sung line
 * > 0.8s) → centered hero; a short intro → compact lower-left badge. This is
 * the fix for the preview-vs-render divergence where "auto" always showed the
 * hero while short-intro songs rendered the tiny badge. We assert on the
 * layout container's flex/text alignment, which differs by template
 * (centered: center/center; badge: flex-end/left) — see PREVIEW_LAYOUTS.
 */
describe("TitleCardPreview — auto template mirrors the backend intro heuristic", () => {
  // The visible artist line is a <div> (the hidden measuring node is a <span>);
  // its parent is the flex layout container carrying the per-template style.
  function layoutOf(container, artistText) {
    const line = [...container.querySelectorAll("div")].find(
      (el) => el.tagName === "DIV" && el.textContent === artistText,
    );
    return line?.parentElement?.style;
  }

  it("auto + long intro (>0.8s) → centered hero", () => {
    const { container } = render(
      <TitleCardPreview artist="Banda" song="Tema" firstLyricStart={2.0} />,
    );
    const s = layoutOf(container, "BANDA");
    expect(s.justifyContent).toBe("center");
    expect(s.textAlign).toBe("center");
  });

  it("auto + short intro (≤0.8s) → compact badge", () => {
    const { container } = render(
      <TitleCardPreview artist="Banda" song="Tema" firstLyricStart={0.2} />,
    );
    const s = layoutOf(container, "BANDA");
    expect(s.justifyContent).toBe("flex-end");
    expect(s.alignItems).toBe("flex-start");
    expect(s.textAlign).toBe("left");
  });

  it("auto + unknown intro (no firstLyricStart) → centered hero (self-corrects later)", () => {
    const { container } = render(<TitleCardPreview artist="Banda" song="Tema" />);
    const s = layoutOf(container, "BANDA");
    expect(s.justifyContent).toBe("center");
    expect(s.textAlign).toBe("center");
  });

  it("an explicit template overrides the intro heuristic", () => {
    // Short intro would resolve badge under auto, but an explicit centered wins.
    const { container } = render(
      <TitleCardPreview artist="Banda" song="Tema" template="centered" firstLyricStart={0.2} />,
    );
    const s = layoutOf(container, "BANDA");
    expect(s.justifyContent).toBe("center");
    expect(s.textAlign).toBe("center");
  });
});
