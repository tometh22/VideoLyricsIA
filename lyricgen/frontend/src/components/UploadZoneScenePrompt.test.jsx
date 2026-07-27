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
  const rail = document.querySelectorAll(".wizard-step-rail button");
  fireEvent.click(rail[n - 1]);
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
    fireEvent.click(document.querySelector('[data-effect="snow"]'));
    expect(screen.queryByTestId("foto-fija-warning")).toBeNull();
  });

  it("'Sin efecto' se renombra para que elegirlo sea una decisión", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    expect(screen.getByText("upload.effect_none_still")).toBeTruthy();
  });

  it("y NO se bloquea seguir: 'Sin efecto' es una opción válida", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);
    const none = document.querySelector('[data-effect="none"]');
    expect(none).not.toBeNull();
    expect(none.disabled).toBe(false);
  });
});

describe("galería moderna de efectos", () => {
  it("filtra por familia sin cambiar el valor elegido", () => {
    render(<Harness movementStyle="foto-parallax" />);
    goStep(3);

    expect(document.querySelectorAll("[data-effect]")).toHaveLength(16);
    fireEvent.click(document.querySelector('[data-effect-category="stylized"]'));

    const visible = [...document.querySelectorAll("[data-effect]")]
      .map((button) => button.dataset.effect);
    expect(visible).toEqual(["prism", "film", "scanlines", "shapes"]);
    expect(visible).not.toContain("none");
    expect(document.querySelector('[data-effect-category="stylized"]')
      .getAttribute("aria-selected")).toBe("true");
  });

  it("el preview protagonista sigue hover/focus y el click confirma la opción", () => {
    render(<Harness />);
    goStep(3);

    const film = document.querySelector('[data-effect="film"]');
    fireEvent.focus(film);
    expect(screen.getByTestId("effect-featured").querySelector("video")
      .getAttribute("src")).toBe("/fx_samples/film.mp4");

    fireEvent.click(film);
    fireEvent.blur(film);
    expect(film.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("effect-featured").querySelector("video")
      .getAttribute("src")).toBe("/fx_samples/film.mp4");
  });
});
