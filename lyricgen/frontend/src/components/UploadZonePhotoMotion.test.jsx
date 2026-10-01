/**
 * Eje "¿qué hace tu foto?" para un fondo SUBIDO por el operador.
 *
 * Contexto (auditoría 30-jul-2026). Cuando el fondo era una foto propia, el
 * wizard mostraba las 6 tarjetas del eje de Movimiento de la IA, que el render
 * NO lee en ninguna de las dos ramas: con `animate_image` el prompt de Veo sale
 * por `elif image_path` antes de mirar el movimiento, y sin `animate_image` el
 * pipeline ni entra a `_ensure_background`. Seis controles vivos que no hacían
 * nada, más un toggle "Animar con AI" en OTRO paso preguntando por el mecanismo
 * ("...en lugar de usar zoom/pan"), más el efecto `foto_viva` como tercera
 * puerta a lo mismo.
 *
 * Ahora hay UN eje con dos opciones reales —quieta / animada— y el efecto sigue
 * siendo el eje hermano, independiente y aditivo.
 *
 * Estos tests cubren el tramo que no tenía cobertura: qué ve y qué manda el
 * wizard cuando hay una foto propia. Anclados en `data-photo-motion` y
 * `aria-checked`, no en clases de Tailwind.
 */
import { useState } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";

// Misma razón que en UploadZoneEditSeedDisplay.test.jsx: la firma real es
// t(key, vars) y devuelve el key cuando falta. El mock `(_k, fallback) => fallback`
// rompe UploadZone (hay un t("upload.sample_words").split(" ") en el paso 4).
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({ UploadTour: () => null, MotionStudioCoach: () => null, EditorTour: () => null }));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

const jpg = () => new File([new Uint8Array([1, 2, 3])], "arte_del_sello.jpg", { type: "image/jpeg" });
const mp4 = () => new File([new Uint8Array([1, 2, 3])], "clip_propio.mp4", { type: "video/mp4" });

/** Un archivo de audio para que el wizard tenga una canción en la lista. */
const audioEntry = () => ({
  file: new File([new Uint8Array([1])], "tema.mp3", { type: "audio/mpeg" }),
  artist: "Artista",
  songTitle: "Tema",
});

function Harness({ backgroundFile = jpg(), bgMode = "custom", reviewResume = false }) {
  const [animateImage, setAnimateImage] = useState(false);
  // `files` en estado a propósito: el fan-out de los batch defaults a cada
  // canción pasa por `onFiles`, y de ahí App lee `jobList[i].movementStyle` para
  // armar el FormData. Un `onFiles={() => {}}` descartaría justo el camino que
  // queremos probar.
  const [files, setFiles] = useState(reviewResume ? [] : [audioEntry()]);
  return (
    <>
      <UploadZone
        files={files}
        onFiles={setFiles}
        user={{ role: "admin", features: {} }}
        allHaveArtist
        bgMode={bgMode}
        backgroundFile={backgroundFile}
        animateImage={animateImage}
        onAnimateImage={setAnimateImage}
        onBackgroundFile={() => {}}
        onStartReview={() => {}}
        onGenerateDirect={() => {}}
        onUploadAdvance={() => {}}
        hasReviewableContent={reviewResume}
      />
      {/* Espejo de lo que viajaría al backend: `animate_image` sale del estado de
          App y `movement_style` del entry de la canción. Sin espiar el FormData. */}
      <output data-testid="wire">
        {JSON.stringify({
          animateImage,
          movementStyle: files[0]?.movementStyle ?? "",
          effect: files[0]?.effect ?? "",
        })}
      </output>
    </>
  );
}

/** El paso es state interno de UploadZone: hay que navegar como el operador. */
function goStep(n) {
  const step = document.querySelector(`[data-wizard-step="${n}"]`);
  expect(step).not.toBeNull();
  fireEvent.click(step);
}

/** El slot 01 arranca colapsado; abrirlo es lo que muestra las tarjetas. */
function openMovementSlot() {
  const trigger = document.querySelector('[data-testid="movement-picker-toggle"]');
  if (trigger) fireEvent.click(trigger);
}

const cards = () =>
  [...document.querySelectorAll("[data-photo-motion]")].map((el) => el.dataset.photoMotion);

const checked = () =>
  [...document.querySelectorAll("[data-photo-motion]")]
    .filter((el) => el.getAttribute("aria-checked") === "true")
    .map((el) => el.dataset.photoMotion);

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("foto subida: el eje de movimiento son 2 opciones reales", () => {
  it("no deja vacío Movimiento al retomar /review sin un File de audio local", () => {
    render(<Harness reviewResume />);
    goStep(3);
    openMovementSlot();
    expect(cards()).toEqual(["quieta", "animar"]);
  });

  it("muestra exactamente 'quieta' y 'animar', no las 6 tarjetas del eje de IA", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    expect(cards()).toEqual(["quieta", "animar"]);
    // Las tarjetas del eje de IA no deben coexistir: son las que no hacen nada
    // cuando el fondo es una foto propia.
    expect(document.querySelectorAll("[data-movement]")).toHaveLength(0);
  });

  it("'quieta' viene preseleccionada (no se le toca el arte por default)", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    expect(checked()).toEqual(["quieta"]);
  });

  it("elegir 'animar' cambia la selección y prende animate_image", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    fireEvent.click(document.querySelector('[data-photo-motion="animar"]'));
    expect(checked()).toEqual(["animar"]);
    expect(JSON.parse(screen.getByTestId("wire").textContent).animateImage).toBe(true);
    expect(document.querySelector('[data-testid="photo-motion-preview-pending"]')).not.toBeNull();
  });

  it("siempre hay UNA opción marcada (no cero, no dos)", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    expect(checked()).toHaveLength(1);
    fireEvent.click(document.querySelector('[data-photo-motion="animar"]'));
    expect(checked()).toHaveLength(1);
  });

  it("manda movement_style='estatico' para que el backend NO le meta el zoom del 15%", () => {
    render(<Harness />);
    // Sin abrir el paso 3: la coerción tiene que valer aunque el operador nunca
    // llegue a Movimiento, o su foto recibe el zoom por omisión.
    expect(JSON.parse(screen.getByTestId("wire").textContent).movementStyle).toBe("estatico");
  });
});

describe("foto subida: el efecto sigue disponible y foto_viva no compite", () => {
  it("esconde foto_viva (era la segunda puerta al mismo image-to-video)", () => {
    render(<Harness />);
    goStep(3);
    const fxTrigger = document.querySelector('[data-testid="effect-picker-toggle"]');
    if (fxTrigger) fireEvent.click(fxTrigger);
    expect(document.querySelector('[data-effect="foto_viva"]')).toBeNull();
  });

  it("los demás efectos siguen disponibles: es el eje aditivo", () => {
    render(<Harness />);
    goStep(3);
    const fxTrigger = document.querySelector('[data-testid="effect-picker-toggle"]');
    if (fxTrigger) fireEvent.click(fxTrigger);
    expect(document.querySelector('[data-effect="snow"]')).not.toBeNull();
  });
});

describe("video subido: el eje no aplica", () => {
  it("no muestra tarjetas de foto ni las del eje de IA, y explica por qué", () => {
    render(<Harness backgroundFile={mp4()} />);
    goStep(3);
    openMovementSlot();
    expect(cards()).toEqual([]);
    expect(document.querySelector('[data-testid="photo-motion-video-note"]')).not.toBeNull();
  });
});

describe("fondo de IA: el eje de siempre queda intacto", () => {
  it("con bgMode=auto se muestran las tarjetas de movimiento de IA", () => {
    render(<Harness bgMode="auto" backgroundFile={null} />);
    goStep(3);
    openMovementSlot();
    expect(cards()).toEqual([]);
    expect(document.querySelectorAll("[data-movement]").length).toBeGreaterThan(1);
  });

  it("con bgMode=auto foto_viva sigue siendo un efecto elegible", () => {
    render(<Harness bgMode="auto" backgroundFile={null} />);
    goStep(3);
    const fxTrigger = document.querySelector('[data-testid="effect-picker-toggle"]');
    if (fxTrigger) fireEvent.click(fxTrigger);
    expect(document.querySelector('[data-effect="foto_viva"]')).not.toBeNull();
  });
});

describe("teclado: el grupo se navega con flechas", () => {
  // Declarar role="radio" sin las flechas es PEOR que no declararlo: el lector
  // anuncia "1 de 2" y con roving tabindex la única tarjeta alcanzable con Tab
  // es la ya seleccionada, así que la otra quedaría inalcanzable.
  it("una sola parada de tabulación: la opción activa", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    const tabbables = [...document.querySelectorAll("[data-photo-motion]")]
      .filter((el) => el.getAttribute("tabindex") === "0")
      .map((el) => el.dataset.photoMotion);
    expect(tabbables).toEqual(["quieta"]);
  });

  it.each(["ArrowRight", "ArrowDown"])("%s mueve la selección a la siguiente", (key) => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    fireEvent.keyDown(document.querySelector('[data-photo-motion="quieta"]'), { key });
    expect(checked()).toEqual(["animar"]);
  });

  it.each(["ArrowLeft", "ArrowUp"])("%s vuelve a la anterior (con wrap)", (key) => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    fireEvent.keyDown(document.querySelector('[data-photo-motion="quieta"]'), { key });
    expect(checked()).toEqual(["animar"]);
  });
});

describe("empujón a Efecto cuando la foto queda inmóvil", () => {
  // El aviso existía sólo para el eje de IA, justo donde menos falta. De 197
  // videos con fondo propio, 3 llevaban efecto: el efecto ya funcionaba, no se
  // encontraba.
  it("avisa cuando la foto está quieta y sin efecto", () => {
    render(<Harness />);
    goStep(3);
    expect(document.querySelector('[data-testid="foto-fija-warning"]')).not.toBeNull();
  });

  it("no avisa si la foto se anima (ahí ya hay movimiento)", () => {
    render(<Harness />);
    goStep(3);
    openMovementSlot();
    fireEvent.click(document.querySelector('[data-photo-motion="animar"]'));
    // Volver al resumen: el aviso vive fuera del composer abierto.
    const back = document.querySelector('[data-testid="motion-composer-back"]');
    if (back) fireEvent.click(back);
    expect(document.querySelector('[data-testid="foto-fija-warning"]')).toBeNull();
  });
});

describe("el toggle de mecanismo ya no existe", () => {
  it("no queda ningún checkbox 'Animar con AI' en el paso del archivo", () => {
    render(<Harness />);
    goStep(2);
    // El toggle era el único checkbox de ese bloque; su copy hablaba de
    // "zoom/pan" y vivía separado del eje que hace la misma pregunta.
    expect(document.querySelector('input[type="checkbox"].sr-only')).toBeNull();
  });
});
