/**
 * Picker de "looks" de letra en el paso 4 del wizard (lib/lyricLooks).
 *
 * Contrato que fija:
 *   - el look del video se marca (elegido + "En el video") al editar;
 *   - con un look activo, tipografía / animación / transición quedan
 *     bloqueadas con una nota (el render las ignora), y tamaño/mayúsculas
 *     siguen editables;
 *   - "Sin look" devuelve los pickers y viaja como lyric_look="" al review.
 *
 * Mismo harness que UploadZoneEditSeedDisplay.test.jsx (UploadZone montado en
 * modo edición, navegando el stepper como el operador).
 */
import { useState } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({ UploadTour: () => null, MotionStudioCoach: () => null, EditorTour: () => null }));
// El preview real se testea aparte (WizardLivePreviewLooks.test.jsx); acá
// sólo importa QUÉ look recibe.
vi.mock("./WizardLivePreview", () => ({
  default: ({ lyricLook }) => <div data-testid="preview" data-preview-look={lyricLook} />,
}));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

const JOB_FIELDS = {
  movementStyle: "estatico",
  effect: "",
  font: "anton",
  textCase: "upper",
  fontScale: "1.0",
  textContrast: "medium",
  lyricsAnimation: "karaoke",
  lineTransition: "wipe",
  lyricLook: "cine",
  frameFormat: "full",
  titleTemplate: "auto",
  titleSize: "1.0",
  titleArtistFont: "",
  titleSongFont: "",
  titleSongBreak: "",
};

const reviews = [];

function Harness({ jobFields = JOB_FIELDS }) {
  const [, setReview] = useState({ ...jobFields });
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
      onEditFieldChange={(field, value) => setReview((r) => {
        const next = { ...r, [field]: value };
        reviews.push(next);
        return next;
      })}
      editSeed={{ jobId: "job-1", genre: "", concept: "", backgroundHint: "", bgVerbatim: false, matchLyrics: true, wizardFields: jobFields }}
      editBaseline={jobFields}
    />
  );
}

function goStep(n) {
  const step = document.querySelector(`[data-wizard-step="${n}"]`);
  expect(step).not.toBeNull();
  fireEvent.click(step);
}

const lookCard = (code) => document.querySelector(`[data-lyric-look="${code || "none"}"]`);
const fontTrigger = () => screen.getAllByRole("button", { name: "upload.font_label" })[0];

afterEach(() => {
  cleanup();
  localStorage.clear();
  reviews.length = 0;
});

describe("paso 4: picker de looks", () => {
  it("muestra los 6 looks arriba de la tipografía, con 'sin look' primero", () => {
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "" }} />);
    goStep(4);
    const cards = [...document.querySelectorAll('[data-testid="lyric-look-picker"] [data-lyric-look]')];
    expect(cards.map((c) => c.dataset.lyricLook)).toEqual(["none", "cosmico", "cine", "pincel", "pop70", "pelicula"]);
    expect(lookCard("").getAttribute("aria-pressed")).toBe("true");
    // Sin look: todo se personaliza a mano.
    expect(fontTrigger().disabled).toBe(false);
    expect(document.querySelector('[data-testid="look-motion-locked"]')).toBeNull();
  });

  it("en edición marca el look DEL VIDEO y bloquea fuente / animación / transición", () => {
    render(<Harness />);
    goStep(4);
    expect(lookCard("cine").getAttribute("aria-pressed")).toBe("true");
    expect(lookCard("cine").dataset.inVideo).toBe("true");
    expect(fontTrigger().disabled).toBe(true);
    expect(screen.getByTestId("look-font-locked").textContent).toBe("upload.look_locked_note");
    // Animación y transición colapsan a una nota en vez de las galerías.
    expect(screen.getByTestId("look-motion-locked")).toBeTruthy();
    expect(document.body.textContent).not.toContain("upload.trans_slide_up");
    // Tamaño y mayúsculas siguen editables.
    const caseBtn = document.querySelector('[data-text-case="lower"]');
    expect(caseBtn.disabled).toBe(false);
    // El preview recibe el look.
    expect(screen.getByTestId("preview").dataset.previewLook).toBe("cine");
  });

  it("elegir otro look viaja al review y al preview", () => {
    render(<Harness />);
    goStep(4);
    fireEvent.click(lookCard("pop70"));
    expect(reviews.at(-1).lyricLook).toBe("pop70");
    expect(lookCard("pop70").getAttribute("aria-pressed")).toBe("true");
    // Hover = preview sin comprometer.
    fireEvent.mouseEnter(lookCard("pincel"));
    expect(screen.getByTestId("preview").dataset.previewLook).toBe("pincel");
    fireEvent.mouseLeave(lookCard("pincel"));
    expect(screen.getByTestId("preview").dataset.previewLook).toBe("pop70");
  });

  it("'Quitar look' vuelve a 'sin look' y devuelve los pickers con lo elegido antes", () => {
    render(<Harness />);
    goStep(4);
    fireEvent.click(screen.getByText("upload.look_clear"));
    expect(reviews.at(-1).lyricLook).toBe("");
    expect(document.querySelector('[data-testid="look-motion-locked"]')).toBeNull();
    expect(fontTrigger().disabled).toBe(false);
    // Lo elegido a mano sigue ahí (karaoke / anton no se pisaron).
    expect(reviews.at(-1).lyricsAnimation).toBe("karaoke");
    expect(reviews.at(-1).font).toBe("anton");
  });
});
