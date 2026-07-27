/**
 * En modo EDICIÓN el wizard tiene que mostrar los ajustes DEL VIDEO, no el
 * sticky de localStorage del último batch.
 *
 * El bug que fija (reclamo 25-jul-2026, staging /videos/5faa4b3f810b): las
 * galerías pintan de `batchDefaults`, que en edición arrancaba del sticky
 * `genly:wizardBatchDefaultsV1` porque `editSeed.wizardFields` sólo llegaba en
 * modo variante. El operador abría "editar fondo", veía "Estático" ya
 * resaltado (su último batch, no este video), no clickeaba — y como el submit
 * es un DIFF, nada viajaba y el render salía con el valor viejo. Siete veces.
 *
 * Por qué este test no existía: ningún test montaba UploadZone con `editMode`
 * (`grep -rl "editSeed\|wizardFields"` sobre los tests → cero archivos). Toda
 * la suite validaba funciones puras que reciben un objeto ya correcto, así que
 * el tramo entre `job.render_params` y el pixel no tenía cobertura — y el bug
 * vivía exactamente ahí.
 *
 * Anclaje de las aserciones: `aria-pressed` + `data-*` en las tarjetas (se
 * agregaron en este PR). Antes la única señal era el string de clases de
 * Tailwind del anillo, que cualquier restyle rompe.
 */
import { useState } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";
import { normalizeMovementCode } from "../lib/catalogCodes";

// i18n real-ish: la firma verdadera es t(key, vars) y devuelve el key cuando
// falta. El mock `t: (_k, fallback) => fallback` que usan otros tests ROMPE
// UploadZone (hay un t("upload.sample_words").split(" ") en el paso 4).
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({ UploadTour: () => null, EditorTour: () => null }));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

const STICKY_KEY = "genly:wizardBatchDefaultsV1";

/** Ajustes del VIDEO: distintos del sticky en todos los ejes que importan. */
const JOB_FIELDS = {
  movementStyle: "animado",
  effect: "snow",
  font: "anton",
  textCase: "lower",
  fontScale: "1.3",
  textContrast: "high",
  lyricsAnimation: "karaoke",
  lineTransition: "fade",
  frameFormat: "cine",
  titleTemplate: "hero",
  titleSize: "1.4",
  titleArtistFont: "bebas",
  titleSongFont: "anton",
  titleSongBreak: "",
};

/** Sticky contaminado por el batch anterior — el valor que NO debe ganar. */
const STICKY_FIELDS = {
  movementStyle: "estatico",
  effect: "rain",
  font: "poppins-bold",
  textCase: "upper",
  fontScale: "1.0",
  textContrast: "medium",
  lyricsAnimation: "none",
  lineTransition: "none",
  frameFormat: "full",
  titleTemplate: "auto",
  titleSize: "1.0",
};

function Harness({ variantMode = false, jobFields = JOB_FIELDS }) {
  const [review, setReview] = useState({ ...jobFields });
  return (
    <UploadZone
      files={[]}
      onFiles={() => {}}
      editMode
      variantMode={variantMode}
      // lockedSteps + hasReviewableContent son lo que monta el WIZARD en vez
      // del dropzone vacío: es lo que App pasa en modo edición (lockedSteps
      // [1,5] = "Subí" y "Fondo" no navegables sobre un job ya renderizado).
      lockedSteps={[1, 5]}
      hasReviewableContent
      user={{ role: "admin", features: {} }}
      allHaveArtist
      onStartReview={() => {}}
      onGenerateDirect={() => {}}
      onUploadAdvance={() => {}}
      onEditFieldChange={(field, value) => setReview((r) => ({ ...r, [field]: value }))}
      editSeed={{ jobId: "job-1", genre: "rock", concept: "", backgroundHint: "", bgVerbatim: false, matchLyrics: true, wizardFields: jobFields }}
      editBaseline={jobFields}
    />
  );
}

// El paso del wizard es state INTERNO de UploadZone, no un prop: hay que
// navegar como el operador. El rail tiene un botón por paso, en orden.
// Paso 3 = Movimiento + Efecto; paso 4 = Tipografía.
function goStep(n) {
  const rail = document.querySelectorAll(".wizard-step-rail button");
  expect(rail.length).toBeGreaterThanOrEqual(n);
  fireEvent.click(rail[n - 1]);
}

const pressed = (selector) =>
  [...document.querySelectorAll(selector)]
    .filter((el) => el.getAttribute("aria-pressed") === "true")
    .map((el) => el.dataset.movement ?? el.dataset.effect
      ?? el.dataset.movementSummary ?? el.dataset.effectSummary);

const anchored = (selector) =>
  [...document.querySelectorAll(selector)]
    .filter((el) => el.dataset.inVideo === "true")
    .map((el) => el.dataset.movement ?? el.dataset.effect
      ?? el.dataset.movementSummary ?? el.dataset.effectSummary);

describe("edición: los controles muestran el video, no el sticky", () => {
  beforeEach(() => {
    localStorage.setItem(STICKY_KEY, JSON.stringify(STICKY_FIELDS));
  });
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("Movimiento: la tarjeta marcada es la del video (animado), no la del sticky (estatico)", () => {
    render(<Harness />);
    goStep(3);
    expect(pressed("[data-movement-summary]")).toEqual(["animado"]);
  });

  it("Efecto: la tarjeta marcada es la del video (snow), no la del sticky (rain)", () => {
    render(<Harness />);
    goStep(3);
    expect(pressed("[data-effect-summary]")).toEqual(["snow"]);
  });

  it("exactamente UNA tarjeta marcada por galería (no cero, no dos)", () => {
    render(<Harness />);
    goStep(3);
    expect(pressed("[data-movement-summary]")).toHaveLength(1);
    expect(pressed("[data-effect-summary]")).toHaveLength(1);
  });

  it.each([
    ["dinamico", "estandar"],
    ["static", "estatico"],
    ["fija", "estatico"],
  ])("un valor NO canónico del job (%s) igual resalta una tarjeta", (raw, expected) => {
    // El backend persiste movement_style CRUDO. La versión anterior de este
    // test usaba "estandar" — que ya es canónico — así que pasaba sin ejercitar
    // el espejo de normalización: una aserción vacía. Estos SÍ son valores que
    // el backend genera ("dinamico" lo emiten SceneEditModal y el derivado por
    // energía) y que sin normalizar no resaltan NINGUNA tarjeta.
    render(<Harness jobFields={{ ...JOB_FIELDS, movementStyle: normalizeMovementCode(raw) }} />);
    goStep(3);
    expect(pressed("[data-movement-summary]")).toEqual([expected]);
  });
});

describe("chip EN EL VIDEO: marca el presente, no el cambio", () => {
  beforeEach(() => localStorage.setItem(STICKY_KEY, JSON.stringify(STICKY_FIELDS)));
  afterEach(() => { cleanup(); localStorage.clear(); });

  it("al abrir, la misma tarjeta lleva el check y el ancla", () => {
    render(<Harness />);
    goStep(3);
    expect(pressed("[data-movement-summary]")).toEqual(["animado"]);
    expect(anchored("[data-movement-summary]")).toEqual(["animado"]);
  });

  it("al cambiar de opción quedan DOS tarjetas marcadas: elegida y la del video", () => {
    render(<Harness />);
    goStep(3);
    fireEvent.click(screen.getByTestId("movement-picker-toggle"));
    fireEvent.click(document.querySelector('[data-movement="estatico"]'));
    expect(pressed("[data-movement]")).toEqual(["estatico"]);   // lo que elegí
    expect(anchored("[data-movement]")).toEqual(["animado"]);   // lo que el video tiene
  });

  it("sin editBaseline (creación) no hay ancla", () => {
    cleanup();
    render(
      <UploadZone
        files={[]} onFiles={() => {}} user={{ role: "admin", features: {} }}
        allHaveArtist onStartReview={() => {}} onGenerateDirect={() => {}}
        onUploadAdvance={() => {}} hasReviewableContent
      />,
    );
    goStep(3);
    expect(anchored("[data-movement-summary]")).toEqual([]);
  });
});

describe("el sticky no se contamina editando", () => {
  beforeEach(() => localStorage.setItem(STICKY_KEY, JSON.stringify(STICKY_FIELDS)));
  afterEach(() => { cleanup(); localStorage.clear(); });

  it("clickear en modo edición NO reescribe genly:wizardBatchDefaultsV1", () => {
    render(<Harness />);
    goStep(3);
    fireEvent.click(screen.getByTestId("movement-picker-toggle"));
    fireEvent.click(document.querySelector('[data-movement="sutil"]'));
    const after = JSON.parse(localStorage.getItem(STICKY_KEY));
    // Arreglar el fondo de un video no puede cambiar el default del próximo
    // batch — y era además la vía por la que el sticky quedaba contaminado con
    // los ajustes de otro video.
    expect(after.movementStyle).toBe("estatico");
  });

  it("pero el cambio SÍ llega al submit (onEditFieldChange)", () => {
    const seen = [];
    render(
      <UploadZone
        files={[]} onFiles={() => {}} editMode
        user={{ role: "admin", features: {} }} allHaveArtist
        onStartReview={() => {}} onGenerateDirect={() => {}} onUploadAdvance={() => {}}
        lockedSteps={[1, 5]} hasReviewableContent
        onEditFieldChange={(f, v) => seen.push([f, v])}
        editSeed={{ jobId: "j", wizardFields: JOB_FIELDS, matchLyrics: true }}
        editBaseline={JOB_FIELDS}
      />,
    );
    goStep(3);
    fireEvent.click(screen.getByTestId("movement-picker-toggle"));
    fireEvent.click(document.querySelector('[data-movement="sutil"]'));
    expect(seen).toContainEqual(["movementStyle", "sutil"]);
  });
});

describe("los colores de letra no se ofrecen en edición (editables e ignorados)", () => {
  afterEach(() => { cleanup(); localStorage.clear(); });

  it("en edición no se renderiza el picker de color", () => {
    render(<Harness />);
    goStep(4);
    expect(screen.queryByLabelText("upload.lyric_color_label")).toBeNull();
  });
});

describe("el wizard deja de prometer lo que el backend va a descartar", () => {
  beforeEach(() => localStorage.setItem(STICKY_KEY, JSON.stringify(STICKY_FIELDS)));
  afterEach(() => { cleanup(); localStorage.clear(); });

  const withPlan = (plan, extra = {}) => (
    <UploadZone
      files={[]} onFiles={() => {}} editMode lockedSteps={[1, 5]} hasReviewableContent
      user={{ role: "admin", features: {} }} allHaveArtist
      onStartReview={() => {}} onGenerateDirect={() => {}} onUploadAdvance={() => {}}
      onEditFieldChange={() => {}}
      editSeed={{ jobId: "j", wizardFields: JOB_FIELDS, matchLyrics: true }}
      editBaseline={JOB_FIELDS}
      editPlan={plan}
      {...extra}
    />
  );

  it("job multi-escena: avisa ARRIBA del bloque de fondo, SIN cambios previos", () => {
    // El motivo llega por `bgBlockedReason`, independiente del plan: derivarlo
    // del plan hacía que el aviso saliera sólo DESPUÉS de configurar el fondo.
    render(withPlan(null, { bgBlockedReason: "scenes" }));
    goStep(3);
    const notice = screen.getByTestId("bg-blocked-notice");
    expect(notice.textContent).toContain("edit.bg_locked_scenes_title");
    // Y dice la salida real: regenerar la escena desde el filmstrip.
    expect(notice.textContent).toContain("edit.bg_locked_scenes_desc");
  });

  it("job aprobado: el motivo es OTRO y el mensaje también", () => {
    render(withPlan(null, { bgBlockedReason: "status" }));
    goStep(3);
    expect(screen.getByTestId("bg-blocked-notice").textContent)
      .toContain("edit.bg_locked_done_title");
  });

  it("job normal: sin aviso", () => {
    render(withPlan(null, { bgBlockedReason: null }));
    goStep(3);
    expect(screen.queryByTestId("bg-blocked-notice")).toBeNull();
  });

  it("el cupo que se muestra es el REAL, no '1 de tus 3'", () => {
    render(withPlan({ blocked: null, willApply: {}, willDrop: [] }, { editsRemaining: 0 }));
    goStep(3);
    expect(screen.getByTestId("edit-quota").textContent).toContain("upload.regen_quota_none");
  });

  it("y un cupo exento no muestra contador", () => {
    render(withPlan({ blocked: null, willApply: {}, willDrop: [] }, { editsRemaining: 2, editLimitExempt: true }));
    goStep(3);
    expect(screen.queryByTestId("edit-quota")).toBeNull();
  });
});
