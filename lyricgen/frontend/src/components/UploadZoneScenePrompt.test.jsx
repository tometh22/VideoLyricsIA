/**
 * "Mi prompt" no se destruye al cambiar de modo de escena, y Foto fija avisa
 * que queda inmóvil.
 *
 * Bug que fija (reclamo 25-jul-2026): clickear "Auto" o "Inspirado en la letra"
 * ejecutaba un clear del prompt del operador — sin confirmación, sin undo, sin
 * registro. Y la trampa: un job CON prompt y match_lyrics=true se muestra en
 * modo "Mi prompt" (el hint le gana, fiel a resolve_creative_mode), así que el
 * operador cuya config YA era "inspirado en la letra" clickeaba esa tarjeta
 * para corregirla y en ese click perdía el prompt. En la cadena reportada pasó
 * DOS veces (jobs d34cef371408 y 5faa4b3f810b quedaron con background_hint="").
 *
 * Contrato que fijamos:
 *  - el prompt sale del PAYLOAD cuando el modo no es "prompt" (eso es el fix
 *    #982 y NO se revierte: en el backend un hint no vacío le gana siempre a
 *    match_lyrics, así que dejarlo en el payload haría que el modo se ignore);
 *  - pero NO se destruye: queda guardado y vuelve con un click;
 *  - borrarlo es una acción explícita.
 */
import { useState } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (key) => key, lang: "es" }) }));
vi.mock("./OnboardingTour", () => ({ UploadTour: () => null, EditorTour: () => null }));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

const PROMPT = "Carnaval argentino surrealista al atardecer, cámara fija";

function Harness({ hint = PROMPT, matchLyrics = true, movementStyle = "estatico", onField }) {
  const [seen] = useState([]);
  return (
    <UploadZone
      files={[]}
      onFiles={() => {}}
      editMode
      lockedSteps={[1, 5]}
      hasReviewableContent
      user={{ role: "admin", features: {} }}
      allHaveArtist
      onStartReview={() => {}}
      onGenerateDirect={() => {}}
      onUploadAdvance={() => {}}
      onEditFieldChange={(f, v) => { seen.push([f, v]); onField?.(f, v); }}
      editSeed={{
        jobId: "job-1", genre: "rock", concept: "",
        backgroundHint: hint, bgVerbatim: true, matchLyrics,
        wizardFields: { movementStyle, effect: "" },
      }}
      editBaseline={{ movementStyle, effect: "" }}
    />
  );
}

function goStep(n) {
  const step = document.querySelector(`[data-wizard-step="${n}"]`);
  expect(step).not.toBeNull();
  fireEvent.click(step);
}

const mode = (code) => document.querySelector(`[data-scene-mode="${code}"]`);
const activeMode = () =>
  [...document.querySelectorAll("[data-scene-mode]")]
    .find((b) => b.getAttribute("aria-pressed") === "true")?.dataset.sceneMode;

afterEach(() => { cleanup(); localStorage.clear(); });

describe("el prompt sobrevive al cambio de modo", () => {
  it("un job con prompt arranca en modo 'Mi prompt' con el texto puesto", () => {
    // Fiel a resolve_creative_mode: un hint no vacío le gana a match_lyrics.
    render(<Harness />);
    goStep(2);
    expect(activeMode()).toBe("prompt");
    expect(document.querySelector("textarea").value).toBe(PROMPT);
  });

  it("clickear 'Inspirado en la letra' saca el prompt del payload pero lo GUARDA", () => {
    const fields = [];
    render(<Harness onField={(f, v) => fields.push([f, v])} />);
    goStep(2);
    fireEvent.click(mode("lyrics"));

    // Contrato #982: el hint sale del payload — si quedara, el backend lo usaría
    // igual y el modo elegido se ignoraría.
    expect(fields).toContainEqual(["backgroundHint", ""]);
    expect(fields).toContainEqual(["matchLyrics", true]);
    // Pero el texto NO se destruyó: la tarjeta lo ofrece de vuelta.
    expect(screen.getByText(PROMPT)).toBeTruthy();
    expect(screen.getByText("upload.saved_prompt_unused")).toBeTruthy();
  });

  it("y volver a 'Mi prompt' lo restaura — el camino de vuelta que no existía", () => {
    const fields = [];
    render(<Harness onField={(f, v) => fields.push([f, v])} />);
    goStep(2);
    fireEvent.click(mode("lyrics"));
    fireEvent.click(screen.getByText("upload.saved_prompt_use"));
    expect(activeMode()).toBe("prompt");
    expect(document.querySelector("textarea").value).toBe(PROMPT);
    expect(fields).toContainEqual(["backgroundHint", PROMPT]);
  });

  it("clickear 'Auto' se comporta igual (guarda, no destruye)", () => {
    render(<Harness />);
    goStep(2);
    fireEvent.click(mode("auto"));
    expect(screen.getByText(PROMPT)).toBeTruthy();
  });

  it("Descartar es la ÚNICA vía de destrucción, y es explícita", () => {
    render(<Harness />);
    goStep(2);
    fireEvent.click(mode("lyrics"));
    expect(screen.getByText(PROMPT)).toBeTruthy();
    fireEvent.click(screen.getByText("upload.saved_prompt_discard"));
    expect(screen.queryByText(PROMPT)).toBeNull();
  });

  it("en modo 'Mi prompt' no se muestra la tarjeta de guardado (el texto está en el campo)", () => {
    render(<Harness />);
    goStep(2);
    expect(activeMode()).toBe("prompt");
    expect(screen.queryByText("upload.saved_prompt_unused")).toBeNull();
  });
});

describe("Foto fija avisa que queda inmóvil", () => {
  it("con foto-parallax y sin efecto muestra el aviso persistente", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    expect(screen.getByTestId("foto-fija-warning")).toBeTruthy();
    expect(screen.getByText("upload.foto_fija_goto_effect")).toBeTruthy();
  });

  it("no aparece con otros movimientos", () => {
    render(<Harness movementStyle="estatico" />);
    goStep(3);
    expect(screen.queryByTestId("foto-fija-warning")).toBeNull();
  });

  it("desaparece al elegir un efecto — y no vuelve", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    expect(screen.getByTestId("foto-fija-warning")).toBeTruthy();
    fireEvent.click(screen.getByText("upload.foto_fija_goto_effect"));
    fireEvent.click(document.querySelector('[data-effect="snow"]'));
    expect(screen.queryByTestId("foto-fija-warning")).toBeNull();
  });

  it("'Sin efecto' se renombra para que elegirlo sea una decisión", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));
    expect(screen.getByText("upload.effect_none_still")).toBeTruthy();
  });

  it("y NO se bloquea seguir: 'Sin efecto' es una opción válida", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));
    const none = document.querySelector('[data-effect="none"]');
    expect(none).not.toBeNull();
    expect(none.disabled).toBe(false);
  });
});

describe("selector compacto de efectos", () => {
  it("agrupa Movimiento y Efecto en un único studio responsive", () => {
    render(<Harness movementStyle="estatico" />);
    goStep(3);

    const studio = screen.getByTestId("motion-studio");
    expect(studio.className).toContain("sm:grid-cols-2");
    expect(studio.contains(screen.getByTestId("movement-picker-toggle"))).toBe(true);
    expect(studio.contains(screen.getByTestId("effect-picker-toggle"))).toBe(true);
    expect(screen.getByText("upload.motion_studio_title")).toBeTruthy();
    expect(screen.getByText("upload.motion_live_badge")).toBeTruthy();
  });

  it("arranca cerrado y resume la selección sin cargar el editor", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);

    const toggle = screen.getByTestId("effect-picker-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("effect-picker-panel")).toBeNull();
    expect(document.querySelectorAll("[data-effect]")).toHaveLength(0);
    expect(screen.getByText("upload.effect_none")).toBeTruthy();
  });

  it("filtra por familia sin cambiar el valor elegido", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));

    expect(document.querySelectorAll("[data-effect]")).toHaveLength(34);
    fireEvent.click(document.querySelector('[data-effect-category="stylized"]'));

    const visible = [...document.querySelectorAll("[data-effect]")]
      .map((button) => button.dataset.effect);
    expect(visible).toEqual([
      "prism", "film", "scanlines", "shapes", "rgb_glitch", "neon_edge",
      "kaleido", "halftone", "ink_reveal", "chromatic_pulse", "cutout_echo",
      "foto_viva",
    ]);
    expect(visible).not.toContain("none");
    expect(document.querySelector('[data-effect-category="stylized"]')
      .getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("effect-picker-toggle").textContent)
      .toContain("upload.motion_editing_badge");
  });

  it("el click confirma la opción y actualiza el resumen compacto", () => {
    render(<Harness />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));

    const film = document.querySelector('[data-effect="film"]');
    fireEvent.click(film);
    expect(film.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("effect-picker-toggle").querySelector("video")
      .getAttribute("src")).toBe("/fx_samples/film.mp4");
  });

  it("Escape vuelve al composer sin perder la selección confirmada", () => {
    render(<Harness />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));
    fireEvent.click(document.querySelector('[data-effect="fog"]'));
    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByTestId("effect-picker-panel")).toBeNull();
    expect(screen.getByTestId("effect-picker-toggle")
      .getAttribute("data-effect-summary")).toBe("fog");
  });

  it("Foto viva alinea el origen a Foto fija y se identifica como IA", () => {
    const fields = [];
    render(<Harness movementStyle="estatico" onField={(f, v) => fields.push([f, v])} />);
    goStep(3);
    fireEvent.click(screen.getByTestId("effect-picker-toggle"));

    const livingPhoto = document.querySelector('[data-effect="foto_viva"]');
    expect(livingPhoto).not.toBeNull();
    expect(livingPhoto.textContent).toContain("upload.effect_ai_badge");
    fireEvent.click(livingPhoto);
    fireEvent.keyDown(window, { key: "Escape" });

    expect(fields).toContainEqual(["movementStyle", "foto-parallax"]);
    expect(fields).toContainEqual(["effect", "foto_viva"]);
    expect(screen.getByTestId("movement-picker-toggle")
      .getAttribute("data-movement-summary")).toBe("foto-parallax");
    expect(screen.getByTestId("effect-picker-toggle")
      .getAttribute("data-effect-summary")).toBe("foto_viva");
    expect(screen.queryByTestId("foto-fija-warning")).toBeNull();
  });
});

describe("E2E del Motion Composer", () => {
  it("recorre Movimiento → Foto fija → Efectos → Bokeh y conserva el resultado", () => {
    render(<Harness movementStyle="estatico" />);
    goStep(3);

    // Estado normal: sólo las dos decisiones confirmadas.
    expect(screen.getByTestId("movement-picker-toggle")).toBeTruthy();
    expect(screen.getByTestId("effect-picker-toggle")).toBeTruthy();
    expect(document.querySelectorAll("[data-movement]")).toHaveLength(0);
    expect(document.querySelectorAll("[data-effect]")).toHaveLength(0);

    // Drill-in de Movimiento reemplaza el inspector, sin apilar Efectos.
    fireEvent.click(screen.getByTestId("movement-picker-toggle"));
    expect(screen.queryByTestId("effect-picker-toggle")).toBeNull();
    expect(document.querySelectorAll("[data-movement]")).toHaveLength(6);
    fireEvent.click(document.querySelector('[data-movement="foto-parallax"]'));
    expect(document.querySelector('[data-movement="foto-parallax"]')
      .getAttribute("aria-pressed")).toBe("true");

    // Escape vuelve al resumen con la selección confirmada y muestra el
    // estado neutro de imagen inmóvil.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("movement-picker-toggle")
      .getAttribute("data-movement-summary")).toBe("foto-parallax");
    expect(screen.getByTestId("foto-fija-warning")).toBeTruthy();

    // "Agregar vida" entra directo al catálogo. Cada efecto tiene poster
    // visible; sólo la opción activa/hovered monta video animado.
    fireEvent.click(screen.getByText("upload.foto_fija_goto_effect"));
    expect(screen.queryByTestId("movement-picker-toggle")).toBeNull();
    expect(screen.getByTestId("effect-picker-panel")).toBeTruthy();
    const cards = [...document.querySelectorAll("[data-effect]")]
      .filter((card) => card.dataset.effect !== "none");
    expect(cards).toHaveLength(33);
    for (const card of cards) {
      expect(card.querySelector("img")?.getAttribute("src"))
        .toBe(`/fx_samples/${card.dataset.effect}.jpg`);
    }

    const bokeh = document.querySelector('[data-effect="bokeh"]');
    fireEvent.click(bokeh);
    expect(bokeh.getAttribute("aria-pressed")).toBe("true");
    expect(bokeh.querySelector("video")?.getAttribute("src"))
      .toBe("/fx_samples/bokeh.mp4");

    // Volver conserva el efecto y elimina el nudge de imagen inmóvil.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("effect-picker-toggle")
      .getAttribute("data-effect-summary")).toBe("bokeh");
    expect(screen.queryByTestId("foto-fija-warning")).toBeNull();
  });
});
