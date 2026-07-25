/**
 * Ficha de ajustes en la página del video.
 *
 * El caso que cierra: en el reclamo original el operador regeneró el fondo 7
 * veces sin poder ver que el video tenía guardado `movement_style: "animado"`.
 * Con este panel se lee en dos segundos.
 */
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import JobSettingsCard from "./JobSettingsCard";

// i18n con la firma real t(key, vars). Devuelve etiquetas realistas para las
// claves de opción: si devolviera el key, la aserción de "no se ven códigos
// internos" pasaría por el mock y no por el código (el key
// "upload.movement_estatico" contiene la subcadena "estatico").
const LABELS = {
  "upload.movement_estatico": "Estático (cámara fija)",
  "upload.movement_animado": "Animado (ilustración)",
  "upload.effect_snow": "Nieve",
  "upload.font_auto": "Auto",
  // También los nombres de eje: si quedaran como keys, la aserción de "no se
  // ven códigos internos" fallaría por el MOCK (detail.axis_font_scale contiene
  // la subcadena "font_scale") en vez de por el código.
  "detail.settings_group_bg": "Fondo",
  "detail.settings_group_lyrics": "Letra",
  "detail.settings_group_title": "Portada",
  "detail.axis_movement": "Movimiento",
  "detail.axis_effect": "Efecto",
  "detail.axis_font": "Tipografía",
  "detail.axis_font_scale": "Tamaño",
};
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => LABELS_REF[key] ?? key, lang: "es" }),
}));
// Indirección para que el mock (hoisted) vea el mapa.
const LABELS_REF = LABELS;

afterEach(cleanup);

const open = () => fireEvent.click(screen.getByTestId("job-settings-toggle"));

describe("JobSettingsCard", () => {
  it("el caso del reclamo: muestra que el video es Animado", () => {
    render(<JobSettingsCard renderParams={{ movement_style: "animado" }} />);
    open();
    const chip = document.querySelector('[data-setting="movement_style"]');
    expect(chip).not.toBeNull();
    expect(chip.textContent).toContain("Animado");
  });

  it("arranca colapsada (es diagnóstico, no la acción principal)", () => {
    render(<JobSettingsCard renderParams={{ movement_style: "animado" }} />);
    expect(screen.queryByTestId("job-settings-body")).toBeNull();
    expect(screen.getByTestId("job-settings-toggle").getAttribute("aria-expanded")).toBe("false");
    open();
    expect(screen.getByTestId("job-settings-body")).toBeTruthy();
  });

  it("la cabecera dice de dónde salió la escena sin necesidad de abrirla", () => {
    render(<JobSettingsCard renderParams={{ background_hint: "un carnaval", bg_verbatim: true }} />);
    expect(screen.getByText("detail.scene_prompt_verbatim")).toBeTruthy();
  });

  it("el prompt va detrás de un details (es el único campo de texto largo)", () => {
    render(<JobSettingsCard renderParams={{ background_hint: "carnaval al atardecer" }} />);
    open();
    expect(screen.getByText("carnaval al atardecer")).toBeTruthy();
    // No como chip: los chips son para valores cortos.
    expect(document.querySelector('[data-setting="background_hint"]')).toBeNull();
  });

  it("un video sin ajustes explícitos no renderiza panel (nada que decir)", () => {
    const { container } = render(
      <JobSettingsCard renderParams={{ movement_style: "", effect: "", title_template: "auto" }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("render_params ausente no rompe", () => {
    const { container } = render(<JobSettingsCard renderParams={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("el link a Provenance dispara el cambio de tab", () => {
    const go = vi.fn();
    render(<JobSettingsCard renderParams={{ movement_style: "animado" }} provenanceHref={go} />);
    open();
    fireEvent.click(screen.getByText("detail.settings_see_provenance"));
    expect(go).toHaveBeenCalled();
  });

  it("no muestra códigos internos ni snake_case al operador", () => {
    render(<JobSettingsCard renderParams={{
      movement_style: "estatico", effect: "snow", font: "anton", font_scale: 1.15,
    }} />);
    open();
    const text = screen.getByTestId("job-settings-body").textContent;
    expect(text).not.toMatch(/movement_style|font_scale|estatico|snow/);
  });
});
