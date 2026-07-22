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

  it("una edición de timing hecha por el HANDLER REAL del componente sobrevive el remount", () => {
    // Audit F3c: la versión previa mutaba la entrada con un
    // segmentsStore.replace() hecho a mano — NO ejercitaba el componente.
    // Ahora manejamos el timing por el camino real del editor: el timestamp
    // editor inline (doble-click en el timestamp → input → Enter →
    // commitEditTimestamp → setEdited → store). El drag del timeline no es
    // drivable en jsdom (sin layout/pointer), pero start/end SÍ lo son por
    // acá, así que el estado adyacente a locked/pos/scale/rot pasa por el
    // componente de verdad antes del ciclo unmount → remount.
    const props = baseProps();
    const { unmount } = render(<LyricsEditor {...props} />);

    // alpha arranca en 1.0 → formatTimestamp = "0:01.0". Doble-click abre el
    // editor inline; cambiamos el start a 1.5 y confirmamos con Enter.
    const tsButton = screen.getByText("0:01.0");
    fireEvent.doubleClick(tsButton);
    const tsInput = screen.getByDisplayValue("0:01.0");
    fireEvent.change(tsInput, { target: { value: "0:01.5" } });
    fireEvent.keyDown(tsInput, { key: "Enter" });

    // El handler real escribió el nuevo start en el store (clamp: prev end 0,
    // next start 3.0 → 1.5 pasa tal cual).
    const live = segmentsStore.get("job-x");
    expect(live[0].start).toBeCloseTo(1.5, 3);

    // Paso 6 → 4 → 6: unmount + remount con el prop stale (start 1.0 original).
    unmount();
    render(<LyricsEditor {...props} />);

    // El timing editado sobrevive — el remount se enganchó al store, no
    // re-seedeó del prop.
    const after = segmentsStore.get("job-x");
    expect(after[0].start).toBeCloseTo(1.5, 3);
    // Y el texto sigue siendo el correcto (no volvió al prop por índice).
    expect(screen.getByDisplayValue("alpha original")).toBeInTheDocument();
  });

  it("F2: 'Resetear timings' apunta al ORIGINAL real tras un remount (no al ya editado)", () => {
    // Bug F2: originalSegmentsRef se sembraba con `edited` (la entrada YA
    // editada del store) en el remount → Reset restauraba las filas a sí
    // mismas (no-op). Fix: la baseline vive en el store (getOriginal) y
    // sobrevive edits/remount. Este test lo prueba de punta a punta con el
    // botón real "Resetear timings" del timeline.
    const props = baseProps({ audioUrl: "blob:fake" });
    const { unmount } = render(<LyricsEditor {...props} />);

    // Editar el start de alpha (1.0 → 1.5) por el editor inline de la lista.
    fireEvent.doubleClick(screen.getByText("0:01.0"));
    const tsInput = screen.getByDisplayValue("0:01.0");
    fireEvent.change(tsInput, { target: { value: "0:01.5" } });
    fireEvent.keyDown(tsInput, { key: "Enter" });
    expect(segmentsStore.get("job-x")[0].start).toBeCloseTo(1.5, 3);

    // Paso 6 → 4 → 6: unmount + remount (aquí originalSegmentsRef se re-siembra).
    unmount();
    render(<LyricsEditor {...props} />);
    // El edit sobrevive (store), como en los otros casos.
    expect(segmentsStore.get("job-x")[0].start).toBeCloseTo(1.5, 3);

    // Cambiar a timeline y apretar "Resetear timings" (el handler real).
    fireEvent.click(screen.getByLabelText("Línea de tiempo"));
    fireEvent.click(screen.getByText("Resetear timings"));

    // Con el fix, Reset restaura el ORIGINAL (1.0), no el ya editado (1.5).
    expect(segmentsStore.get("job-x")[0].start).toBeCloseTo(1.0, 3);
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
