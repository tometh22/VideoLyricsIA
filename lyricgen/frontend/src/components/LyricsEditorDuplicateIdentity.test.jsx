/**
 * Regresión del incidente f866cbcf0e49 (UMG Chile, 1-sep-2026).
 *
 * `segment_id` es la clave estable con la que `mergeThreeWay` identifica cada
 * línea. `duplicateSeg` clonaba la fila con `{ ...orig }`, así que la copia
 * heredaba el `segment_id` del padre. Con dos filas compartiendo clave, un
 * rebase cualquiera (un 409, un reconcile) devolvía N veces la MISMA línea y
 * el deduplicador de colisiones borraba las sobrantes: el operador duplicó el
 * estribillo a mano, aprobó, y el video salió con 6 líneas menos que nunca
 * tocó. Duplicar una línea tiene que crear una IDENTIDAD nueva, no una copia
 * de la identidad.
 */
import { render, cleanup, act, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
import { mergeThreeWay } from "../editorMerge";
import { canonicalizeEditorSegments } from "../lib/segmentTiming";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const JOB_ID = "job-dup-identity";

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

function renderEditor() {
  render(
    <LyricsEditor
      segments={[
        { segment_id: "4", start: 1.0, end: 2.0, text: "esta es nuestra fiesta" },
        { segment_id: "5", start: 3.0, end: 4.0, text: "tiro de gracia" },
      ]}
      filename="song.mp3"
      audioFile={null}
      referenceLyrics=""
      onApprove={vi.fn()}
      onBack={vi.fn()}
      transcribeJobId={JOB_ID}
    />,
  );
}

function duplicateFirstRow() {
  // El botón ✎ de la primera fila: "Duplicar línea (útil para estribillos
  // repetidos)" — exactamente el flujo que usó el operador del incidente.
  const [button] = document.querySelectorAll('[title*="Duplicar"]');
  act(() => { fireEvent.click(button); });
  return segmentsStore.get(JOB_ID);
}

describe("duplicar una línea crea identidad propia", () => {
  it("la copia no hereda el segment_id del original", () => {
    renderEditor();
    const segments = duplicateFirstRow();

    expect(segments).toHaveLength(3);
    const ids = segments.map((row) => String(row.segment_id));
    expect(new Set(ids).size).toBe(3);
    expect(ids.filter((id) => id === "4")).toHaveLength(1);
  });

  it("la copia sobrevive un rebase del editor (la forma exacta del incidente)", () => {
    renderEditor();
    const segments = duplicateFirstRow();

    // Un 409 o un reconcile sin cambios: base = local = remote.
    const merged = mergeThreeWay(segments, segments, segments).merged;
    // Y el paso que remataba borrando las copias idénticas.
    const canonical = canonicalizeEditorSegments(merged);

    expect(canonical).toHaveLength(segments.length);
    expect(canonical.map((row) => row.text)).toEqual(segments.map((row) => row.text));
  });
});
