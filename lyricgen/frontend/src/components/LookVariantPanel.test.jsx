// Panel «Variante»: controlado, cada acción sale por onChange(field, value).
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LookVariantPanel from "./LookVariantPanel";
import { STATIC_LOOK_CATALOG } from "../lib/lyricLookVariants";

afterEach(cleanup);

const RESOLVED = {
  look: "cinetico", genre: "metal", family: "heavy", genre_source: "detected", variant: 3,
  description: "Rubik Dirt + Metal Mania · sangre · oscuro",
  fonts: { main: { id: "rubik-dirt", css_family: "Rubik Dirt", weight: 400 }, key: { id: "metal-mania", css_family: "Metal Mania", weight: 400 } },
  colors: { ink: "#F2EEE6", alt: "#CFC9BE", accent: "#D0101E" },
  overridden: {}, background: "source", warnings: [],
};
const EMPTY = {
  lyricGenre: "", lyricVariant: "", lyricFontMain: "", lyricFontKey: "",
  lyricColor: "#FFFFFF", lyricAltColor: "", lyricAccentColor: "",
};

function renderPanel(props = {}) {
  const onChange = vi.fn();
  render(
    <LookVariantPanel
      t={(k) => k}
      catalog={STATIC_LOOK_CATALOG}
      resolved={RESOLVED}
      values={EMPTY}
      onChange={onChange}
      {...props}
    />,
  );
  return onChange;
}

const openListbox = (name) => fireEvent.click(screen.getByRole("button", { name }));

describe("LookVariantPanel", () => {
  it("muestra el género detectado y la descripción de la variante", () => {
    renderPanel();
    expect(screen.getByTestId("variant-genre-status").textContent).toBe("Detectado: Metal");
    expect(screen.getByTestId("variant-description").textContent).toContain("Variante 3");
    expect(screen.getByTestId("variant-description").textContent).toContain("Rubik Dirt + Metal Mania");
  });

  it("sin resolve todavía usa la detección", () => {
    renderPanel({ resolved: null, detected: { genre: "punk", family: "heavy" } });
    expect(screen.getByTestId("variant-genre-status").textContent).toBe("Detectado: Punk");
  });

  it("corregir el género setea lyricGenre; 'Automático' lo vuelve a ''", () => {
    const onChange = renderPanel();
    openListbox("Género");
    fireEvent.click(screen.getByRole("option", { name: "Reggaetón" }));
    expect(onChange).toHaveBeenLastCalledWith("lyricGenre", "reggaeton");
  });

  it("'Otra variante' = variante resuelta + 1", () => {
    const onChange = renderPanel();
    fireEvent.click(screen.getByTestId("variant-next"));
    expect(onChange).toHaveBeenCalledWith("lyricVariant", 4);
  });

  it("'Otra variante' sin resolve parte del número guardado (o de 0)", () => {
    const onChange = renderPanel({ resolved: null, values: { ...EMPTY, lyricVariant: 5 } });
    fireEvent.click(screen.getByTestId("variant-next"));
    expect(onChange).toHaveBeenCalledWith("lyricVariant", 6);
  });

  it("'Restaurar' limpia la variante y TODOS los overrides", () => {
    const onChange = renderPanel({
      values: {
        ...EMPTY, lyricGenre: "rock", lyricVariant: 4, lyricFontMain: "anton", lyricFontKey: "creepster",
        lyricColor: "#112233", lyricAltColor: "#445566", lyricAccentColor: "#778899",
      },
    });
    fireEvent.click(screen.getByTestId("variant-restore"));
    const calls = Object.fromEntries(onChange.mock.calls);
    expect(calls).toEqual({
      lyricVariant: "", lyricFontMain: "", lyricFontKey: "",
      lyricColor: "#FFFFFF", lyricAltColor: "", lyricAccentColor: "",
    });
  });

  it("'Restaurar' está deshabilitado si no hay nada que restaurar", () => {
    renderPanel();
    expect(screen.getByTestId("variant-restore").disabled).toBe(true);
  });

  it("las fuentes: default 'De la variante (X)', familia del género primero, cada una en su tipografía", () => {
    const onChange = renderPanel();
    const trigger = screen.getByRole("button", { name: "Letra principal" });
    expect(trigger.textContent).toBe("De la variante (Rubik Dirt)");
    fireEvent.click(trigger);
    const options = screen.getAllByRole("option");
    expect(options[0].textContent).toContain("De la variante");
    // Familia heavy primero.
    const heavyIds = STATIC_LOOK_CATALOG.fonts.filter((f) => f.families.includes("heavy")).map((f) => f.label);
    expect(options.slice(1, 1 + heavyIds.length).map((o) => o.textContent)).toEqual(heavyIds);
    const creepster = screen.getByRole("option", { name: "Creepster" });
    expect(creepster.style.fontFamily).toContain("Creepster");
    fireEvent.click(creepster);
    expect(onChange).toHaveBeenLastCalledWith("lyricFontMain", "creepster");

    fireEvent.click(screen.getByRole("button", { name: "Letra destacada" }));
    fireEvent.click(screen.getByRole("option", { name: "Pirata One" }));
    expect(onChange).toHaveBeenLastCalledWith("lyricFontKey", "pirata-one");
  });

  it("los colores muestran el de la variante y un cambio setea el override", () => {
    const onChange = renderPanel();
    const ink = screen.getByLabelText("Texto");
    expect(ink.value).toBe("#f2eee6");
    fireEvent.change(screen.getByLabelText("Secundario"), { target: { value: "#00ff00" } });
    expect(onChange).toHaveBeenLastCalledWith("lyricAltColor", "#00FF00");
    fireEvent.change(ink, { target: { value: "#123456" } });
    expect(onChange).toHaveBeenLastCalledWith("lyricColor", "#123456");
  });

  it("'usar el de la variante' resetea sólo ese color", () => {
    const onChange = renderPanel({ values: { ...EMPTY, lyricColor: "#123456", lyricAccentColor: "#FF0000" } });
    fireEvent.click(screen.getByTestId("variant-color-ink-reset"));
    expect(onChange).toHaveBeenLastCalledWith("lyricColor", "#FFFFFF");
    fireEvent.click(screen.getByTestId("variant-color-accent-reset"));
    expect(onChange).toHaveBeenLastCalledWith("lyricAccentColor", "");
    expect(screen.queryByTestId("variant-color-alt-reset")).toBeNull();
  });

  it("muestra los warnings low_contrast_*", () => {
    renderPanel({ resolved: { ...RESOLVED, warnings: ["low_contrast_ink", "unrelated"] } });
    const chips = screen.getAllByTestId("variant-contrast-warning");
    expect(chips).toHaveLength(1);
    expect(chips[0].dataset.warning).toBe("low_contrast_ink");
    expect(chips[0].textContent).toMatch(/poco contraste/);
  });
});
