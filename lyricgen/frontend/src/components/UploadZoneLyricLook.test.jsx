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
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";
import { _resetLookVariantCaches } from "../lib/lyricLookVariants";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({ UploadTour: () => null, MotionStudioCoach: () => null, EditorTour: () => null }));
// El preview real se testea aparte (WizardLivePreviewLooks.test.jsx); acá
// sólo importa QUÉ look recibe.
vi.mock("./WizardLivePreview", () => ({
  default: ({ lyricLook, lookResolved, lyricFontMain }) => (
    <div
      data-testid="preview"
      data-preview-look={lyricLook}
      data-preview-variant={lookResolved ? String(lookResolved.variant) : ""}
      data-preview-font-main={lyricFontMain || ""}
    />
  ),
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

function Harness({ jobFields = JOB_FIELDS, existingBgOwnedByLook, artist = "", song = "" }) {
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
      existingBgOwnedByLook={existingBgOwnedByLook}
      titlePreviewArtist={artist}
      titlePreviewSong={song}
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
  it("muestra los 16 looks arriba de la tipografía, con 'sin look' primero", () => {
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "" }} />);
    goStep(4);
    const cards = [...document.querySelectorAll('[data-testid="lyric-look-picker"] [data-lyric-look]')];
    expect(cards.map((c) => c.dataset.lyricLook)).toEqual([
      "none", "cosmico", "cine", "pincel", "pop70", "pelicula",
      "cinetico", "neon", "chat", "cuaderno", "bloque", "arco", "perspectiva",
      "duotono", "romantico", "degrade", "y2k",
    ]);
    // Cada tarjeta trae su miniatura (fondo + demo) y su descripción.
    for (const card of cards) {
      expect(card.querySelector(".aspect-video").style.background, card.dataset.lyricLook).toBeTruthy();
      expect(card.getAttribute("title"), card.dataset.lyricLook).toBeTruthy();
    }
    // La grilla envuelve y scrollea adentro en vez de empujar todo el paso.
    expect(screen.getByTestId("lyric-look-grid").className).toMatch(/overflow-y-auto/);
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
    expect(screen.getByTestId("look-font-locked").textContent).toBe("upload.look_font_locked_note");
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

describe("paso 4: looks que no usan el fondo", () => {
  it("job nuevo: Pop 70s / Cinético / Degradé avisan que no se genera fondo", () => {
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "" }} />);
    goStep(4);
    expect(screen.queryByTestId("look-owns-bg-note")).toBeNull();
    for (const code of ["pop70", "cinetico", "degrade"]) {
      fireEvent.click(lookCard(code));
      expect(screen.getByTestId("look-owns-bg-note").textContent).toBe("upload.look_owns_bg_note");
    }
    fireEvent.click(lookCard("neon"));
    expect(screen.queryByTestId("look-owns-bg-note")).toBeNull();
    expect(screen.queryByTestId("look-needs-bg-note")).toBeNull();
  });

  it("edición de un video hecho sin fondo: elegir un look que usa fondo avisa que hay que regenerarlo", () => {
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "pop70" }} existingBgOwnedByLook />);
    goStep(4);
    // Sigue con un look que pinta todo el cuadro: nada que avisar.
    expect(screen.queryByTestId("look-needs-bg-note")).toBeNull();
    // En la edición de un job existente no se promete "no se genera".
    expect(screen.queryByTestId("look-owns-bg-note")).toBeNull();
    fireEvent.click(lookCard("degrade"));
    expect(screen.queryByTestId("look-needs-bg-note")).toBeNull();
    fireEvent.click(lookCard("cine"));
    expect(screen.getByTestId("look-needs-bg-note").textContent).toBe("upload.look_needs_bg_note");
    // "Sin look" también necesita fondo.
    fireEvent.click(lookCard(""));
    expect(screen.getByTestId("look-needs-bg-note")).toBeTruthy();
    // Sólo informa: el look elegido viaja igual que siempre.
    expect(reviews.at(-1).lyricLook).toBe("");
  });

  it("edición de un video CON fondo: no hay aviso de regenerar", () => {
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "pop70" }} existingBgOwnedByLook={false} />);
    goStep(4);
    fireEvent.click(lookCard("cine"));
    expect(screen.queryByTestId("look-needs-bg-note")).toBeNull();
    expect(screen.queryByTestId("look-owns-bg-note")).toBeNull();
  });
});

describe("paso 4: variante del look por género", () => {
  const realFetch = globalThis.fetch;
  let resolveBodies;
  const RESOLVED = (body) => ({
    look: body.lyric_look, genre: "metal", family: "heavy", genre_source: "detected",
    variant: body.lyric_variant === "" ? 3 : body.lyric_variant,
    description: "Rubik Dirt + Metal Mania · sangre · oscuro",
    fonts: { main: { id: "rubik-dirt", css_family: "Rubik Dirt", weight: 400 }, key: { id: "metal-mania", css_family: "Metal Mania", weight: 400 } },
    colors: { ink: "#F2EEE6", alt: "#CFC9BE", accent: "#D0101E" },
    overridden: {}, background: "source", warnings: [],
  });
  const ok = (data) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });

  afterEach(() => {
    globalThis.fetch = realFetch;
    _resetLookVariantCaches();
  });

  function mockApi() {
    resolveBodies = [];
    globalThis.fetch = vi.fn((url, init = {}) => {
      if (String(url).endsWith("/lyric-looks/catalog")) return Promise.reject(new TypeError("offline"));
      if (String(url).endsWith("/lyric-looks/detect-genre")) return ok({ genre: "metal", family: "heavy", source: "ai" });
      if (String(url).endsWith("/lyric-looks/resolve")) {
        const body = JSON.parse(init.body);
        resolveBodies.push(body);
        return ok(RESOLVED(body));
      }
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
    });
  }

  it("con look: panel «Variante», galería con recomendados primero y preview con lo resuelto", async () => {
    mockApi();
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "cinetico" }} artist="Almafuerte" song="Toro y Pampa" />);
    goStep(4);
    expect(screen.getByTestId("variant-panel")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("preview").dataset.previewVariant).toBe("3"));
    // El pedido lleva el job y la canción.
    expect(resolveBodies.at(-1)).toMatchObject({ lyric_look: "cinetico", job_id: "job-1", artist: "Almafuerte", song_title: "Toro y Pampa" });
    // Familia heavy: recomendados primero (sin look siempre primero) con su marca.
    const cards = [...document.querySelectorAll('[data-testid="lyric-look-picker"] [data-lyric-look]')].map((c) => c.dataset.lyricLook);
    expect(cards.slice(0, 9)).toEqual(["none", "cinetico", "pincel", "bloque", "neon", "cine", "duotono", "perspectiva", "cosmico"]);
    const badges = screen.getAllByTestId("look-recommended");
    expect(badges).toHaveLength(8);
    // (t mockeado devuelve la key; el texto real interpola {family}.)
    expect(badges[0].textContent).toBe("upload.variant_recommended_for");
    expect(badges[0].closest("[data-lyric-look]").dataset.lyricLook).toBe("cinetico");
    // Lo resuelto viaja como pin con la review (para mandar el número al generar).
    await waitFor(() => expect(reviews.some((r) => r.lookResolved?.variant === 3)).toBe(true));
    // La tipografía queda bloqueada: se elige en «Variante».
    expect(screen.getByTestId("look-font-locked").textContent).toBe("upload.look_font_locked_note");
  });

  it("'Otra variante' y los overrides viajan al review; 'Restaurar' los limpia", async () => {
    mockApi();
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "cinetico" }} artist="A" song="T" />);
    goStep(4);
    await waitFor(() => expect(screen.getByTestId("variant-description").textContent).toContain("Variante 3"));
    fireEvent.click(screen.getByTestId("variant-next"));
    expect(reviews.at(-1).lyricVariant).toBe(4);
    await waitFor(() => expect(resolveBodies.at(-1).lyric_variant).toBe(4));
    fireEvent.click(screen.getByRole("button", { name: "Letra principal" }));
    fireEvent.click(screen.getByRole("option", { name: "Creepster" }));
    expect(reviews.at(-1).lyricFontMain).toBe("creepster");
    expect(screen.getByTestId("preview").dataset.previewFontMain).toBe("creepster");
    fireEvent.click(screen.getByTestId("variant-restore"));
    const last = reviews.at(-1);
    expect(last.lyricVariant).toBe("");
    expect(last.lyricFontMain).toBe("");
    expect(last.lyricColor).toBe("#FFFFFF");
  });

  it("sin look no hay panel ni pedidos de variante", async () => {
    mockApi();
    render(<Harness jobFields={{ ...JOB_FIELDS, lyricLook: "" }} artist="A" song="T" />);
    goStep(4);
    expect(screen.queryByTestId("variant-panel")).toBeNull();
    await new Promise((r) => setTimeout(r, 300));
    expect(resolveBodies).toHaveLength(0);
  });
});
