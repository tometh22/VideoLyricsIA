/**
 * PR E adversarial audit — FIX 1 (P1, silent data loss for jobId-less reviews).
 *
 * Una review puede tener transcribeJobId Y editingJobId AMBOS null (real:
 * handleBackInReview setea `transcribeJobId: last.transcribeJobId || null`;
 * paths de resume/recovery). PR E borró el espejo `onEditedChange` que antes
 * empujaba esos edits jobId-less a currentReview.segments → snapshot de
 * wizardPersistence. Sin una key de store estable, esos edits caían al useState
 * local del hook y MORÍAN en el unmount del editor (paso 6 → 4) o en un refresh.
 *
 * FIX: DECOUPLE la key del store del backend job id. App pasa un `storeKey`
 * sintético estable (reviewStoreKey → "local:<filename>:<queueIdx>") que existe
 * aunque no haya job de backend. El autosave sigue gateado por transcribeJobId
 * (null → NO postea al backend), pero el store + el snapshot de
 * wizardPersistence ahora sí persisten el edit.
 *
 * Este test reproduce el ciclo mount → edit → unmount → remount con el MISMO
 * storeKey y un prop `segments` stale, y verifica que el edit sobrevive SIN que
 * se dispare ningún POST al backend.
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

const STALE_PROP_SEGMENTS = [
  { start: 1.0, end: 2.0, text: "alpha original" },
  { start: 3.0, end: 4.0, text: "beta original" },
];

const STORE_KEY = "local:song.mp3:0";

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

describe("LyricsEditor — review SIN job de backend (storeKey sintético) no pierde edits", () => {
  it("el edit sobrevive unmount → remount bajo el mismo storeKey, SIN postear al backend", () => {
    const onPersistSegments = vi.fn();
    const props = {
      segments: STALE_PROP_SEGMENTS,
      filename: "song.mp3",
      audioFile: null,
      referenceLyrics: "",
      onApprove: vi.fn(),
      onBack: vi.fn(),
      // La marca del bug: NO hay job de backend...
      transcribeJobId: null,
      onPersistSegments,
      // ...pero SÍ hay una identidad de store estable.
      storeKey: STORE_KEY,
    };

    const { unmount } = render(<LyricsEditor {...props} />);

    fireEvent.change(screen.getByDisplayValue("alpha original"), {
      target: { value: "alpha CORREGIDA" },
    });
    expect(screen.getByDisplayValue("alpha CORREGIDA")).toBeInTheDocument();

    // El edit está en el store bajo el storeKey sintético (no en un jobId).
    expect(segmentsStore.get(STORE_KEY)?.[0].text).toBe("alpha CORREGIDA");

    // Paso 6 → 4: unmount. Paso 4 → 6: remount con el MISMO storeKey + prop stale.
    unmount();
    render(<LyricsEditor {...props} />);

    // El edit sobrevive — antes moría en el useState local del hook.
    expect(screen.getByDisplayValue("alpha CORREGIDA")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha original")).not.toBeInTheDocument();

    // DECOUPLE: aunque onPersistSegments está pasado, transcribeJobId null
    // gatea el autosave → nunca se postea al backend (no hay job real).
    expect(onPersistSegments).not.toHaveBeenCalled();
  });
});
