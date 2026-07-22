/**
 * PR E — regresión Seba+Gaby (UMG): "se borran los tiempos/las ediciones
 * al navegar entre pasos del wizard".
 *
 * El wizard DES-MONTA el LyricsEditor al ir del paso 6 al 4 y lo re-monta
 * al volver. Antes, el estado `edited` era un useState local: el remount
 * re-seedeaba desde el prop `segments` (currentReview.segments), que
 * podía estar STALE (el autosave de 3 s no había flusheado, o el espejo
 * se perdió) → los tiempos, locks y textos editados desaparecían.
 *
 * Ahora `edited` vive en el segmentsStore (Map por jobId a nivel módulo):
 * el unmount NO lo destruye y el remount se re-engancha a la entrada
 * viva, IGNORANDO el prop stale. Este test reproduce el ciclo completo
 * mount → edit → unmount → remount con el MISMO prop stale y verifica
 * que se muestra lo editado.
 */
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

// El prop `segments` que App pasa en AMBOS mounts — como el wizard, que
// re-monta con currentReview.segments sin enterarse de las ediciones.
const STALE_PROP_SEGMENTS = [
  { start: 1.0, end: 2.0, text: "alpha original" },
  { start: 3.0, end: 4.0, text: "beta original" },
];

function baseProps(overrides = {}) {
  return {
    segments: STALE_PROP_SEGMENTS,
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-x",
    ...overrides,
  };
}

// El afterEach global (test-setup.js) ya hace segmentsStore._clearAll();
// lo repetimos explícito acá porque ESTE archivo depende de no leakear
// la entrada "job-x" entre tests.
afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

describe("LyricsEditor — el estado editado sobrevive unmount/remount (wizard 6 ↔ 4)", () => {
  it("una edición de texto sobrevive al ciclo unmount → remount con prop stale", () => {
    const props = baseProps();
    const { unmount } = render(<LyricsEditor {...props} />);

    // La operadora corrige una línea.
    fireEvent.change(screen.getByDisplayValue("alpha original"), {
      target: { value: "alpha CORREGIDA" },
    });
    expect(screen.getByDisplayValue("alpha CORREGIDA")).toBeInTheDocument();

    // Paso 6 → 4: el wizard des-monta el editor.
    unmount();

    // Paso 4 → 6: remount con el MISMO prop stale (el bug original).
    render(<LyricsEditor {...props} />);

    // Lo editado sigue en pantalla — el remount se enganchó al store,
    // no re-seedeó del prop.
    expect(screen.getByDisplayValue("alpha CORREGIDA")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha original")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("beta original")).toBeInTheDocument();
  });

  it("un cambio de timing/lock hecho por fuera del DOM (store) sobrevive igual", () => {
    // Los timings se editan vía drag del timeline (inviable en jsdom);
    // el camino de datos es el mismo setEdited → store, así que lo
    // simulamos mutando la entrada viva como lo haría el handler.
    const props = baseProps();
    const { unmount } = render(<LyricsEditor {...props} />);

    const live = segmentsStore.get("job-x");
    expect(live).toHaveLength(2);
    segmentsStore.replace(
      "job-x",
      live.map((s, i) => (i === 0 ? { ...s, start: 1.4, end: 2.4, locked: true } : s)),
    );

    unmount();
    render(<LyricsEditor {...props} />);

    const after = segmentsStore.get("job-x");
    expect(after[0].start).toBe(1.4);
    expect(after[0].locked).toBe(true);
    // Y el texto sigue siendo el correcto (no volvió al prop por índice).
    expect(screen.getByDisplayValue("alpha original")).toBeInTheDocument();
  });

  it("tras evict (aprobar la canción), el remount re-seedea del prop", () => {
    const props = baseProps();
    const { unmount } = render(<LyricsEditor {...props} />);
    fireEvent.change(screen.getByDisplayValue("alpha original"), {
      target: { value: "alpha CORREGIDA" },
    });
    unmount();

    // App aprueba y evicta la entrada (handleApproveLyrics).
    segmentsStore.evict("job-x");

    // Re-entrada al job: sin entrada viva, el seed vuelve al prop —
    // App es responsable de pasar los segments aprobados como prop.
    render(<LyricsEditor {...props} />);
    expect(screen.getByDisplayValue("alpha original")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha CORREGIDA")).not.toBeInTheDocument();
  });
});
