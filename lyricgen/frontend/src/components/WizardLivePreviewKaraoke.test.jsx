// Tests for Phase C 2026-05-25: WizardLivePreview renders the active
// lyric line with word-jump karaoke when given a playbackTickRef from
// LyricsEditor. Replaces the legacy sample loop 'esta es tu letra'
// during real audio playback.
//
// Cobertura crítica: si esta lógica se rompe (regresion en el ref-based
// flow o en el rAF reader), el preview vuelve al sample y el operador
// pierde la coherencia visual con la lista del editor.

import { render, cleanup, act, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import WizardLivePreview from "./WizardLivePreview";
import { useRef } from "react";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

afterEach(cleanup);

// Harness: simula el flujo de App.jsx escribiendo al ref + el rAF de
// WizardLivePreview leyéndolo. NO necesitamos LyricsEditor entero —
// el ref es la única interfaz.
function _Harness({ initialTick = null, refOut = null }) {
  const ref = useRef({ activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0 });
  if (initialTick) ref.current = initialTick;
  if (refOut) refOut.current = ref;
  return (
    <WizardLivePreview
      style="oscuro"
      movementStyle="estatico"
      effect=""
      playbackTickRef={ref}
    />
  );
}

describe("WizardLivePreview — Phase C karaoke driven by ref", () => {
  it("renders the legacy sample when ref is empty (no live playback)", () => {
    const { container } = render(<_Harness initialTick={null} />);
    // El sample por defecto es 'esta es tu letra' (mock i18n devuelve fallback).
    expect(container.textContent.toLowerCase()).toContain("esta es tu letra");
  });

  it("renders the active line from the ref instead of the sample (after rAF tick)", async () => {
    const refOut = { current: null };
    const { container } = render(
      <_Harness refOut={refOut} initialTick={null} />,
    );
    // Inicial: sample.
    expect(container.textContent.toLowerCase()).toContain("esta es tu letra");

    // Simulamos que LyricsEditor pushea un tick al ref (línea activa).
    await act(async () => {
      refOut.current.current = {
        activeLine: "Hola mundo cantando",
        activeStart: 1.0,
        activeEnd: 2.5,
        currentTime: 1.6,
      };
      // Esperamos varios frames para que el rAF interno detecte el cambio.
      await new Promise((res) => setTimeout(res, 50));
    });
    await waitFor(() => {
      expect(container.textContent).toContain("Hola");
      expect(container.textContent).toContain("mundo");
      expect(container.textContent).toContain("cantando");
    }, { timeout: 1000 });
    // Y el sample original ya no debe estar visible.
    expect(container.textContent.toLowerCase()).not.toContain("esta es tu letra");
  });

  it("changes active line when ref updates to a different segment", async () => {
    // En vez de probar el clear (que depende de timing del rAF en jsdom),
    // probamos el switch entre dos segments distintos — uso real del
    // operador al avanzar el audio.
    const refOut = { current: null };
    const { container } = render(<_Harness refOut={refOut} />);

    await act(async () => {
      refOut.current.current = {
        activeLine: "primera linea del verso",
        activeStart: 0,
        activeEnd: 2,
        currentTime: 0.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("primera"), { timeout: 1000 });

    await act(async () => {
      refOut.current.current = {
        activeLine: "segunda linea diferente",
        activeStart: 2,
        activeEnd: 4,
        currentTime: 2.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => {
      expect(container.textContent).toContain("segunda");
      expect(container.textContent).toContain("diferente");
    }, { timeout: 1000 });
  });
});
