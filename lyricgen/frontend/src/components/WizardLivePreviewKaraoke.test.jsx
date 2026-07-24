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
function _Harness({ initialTick = null, refOut = null, lyricsAnimation = "none" }) {
  const ref = useRef({ activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0 });
  if (initialTick) ref.current = initialTick;
  if (refOut) refOut.current = ref;
  return (
    <WizardLivePreview
      style="oscuro"
      movementStyle="estatico"
      effect=""
      lyricsAnimation={lyricsAnimation}
      playbackTickRef={ref}
      // 2026-05-26: el default productivo (textCase=upper) upper-casearía
      // los samples ("ESTA ES TU LETRA", "TRES"…) y rompería los asserts
      // que buscan texto en su forma original. Estos tests cubren la
      // lógica de karaoke/word-anim, no el case rendering — forzamos
      // "original" para mantener el espíritu del test.
      textCase="original"
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

// Fix 2026-05-25: Phase C antes rendeaba word-jump (color #19E0BC en la
// palabra activa) sin importar el lyricsAnimation elegido — el operador
// elegía pop/glow/none/word_reveal y veía karaoke. Estos tests fijan el
// branching por lyricsAnimation para evitar regresiones.
describe("WizardLivePreview — Phase C respects lyricsAnimation", () => {
  // Helper: encuentra el span de una palabra específica del DOM rendeado.
  const findWordSpan = (container, word) => {
    return [...container.querySelectorAll("span")].find(
      (el) => el.textContent.trim() === word && el.children.length === 0,
    );
  };

  it("karaoke: active word gets the live highlight color (#19E0BC)", async () => {
    const refOut = { current: null };
    const { container } = render(
      <_Harness refOut={refOut} lyricsAnimation="karaoke" />,
    );
    await act(async () => {
      refOut.current.current = {
        activeLine: "uno dos tres",
        activeStart: 0,
        activeEnd: 3,
        // currentTime=1.5 → activeWordIdx=1 (= "dos" en una línea de 3 palabras)
        currentTime: 1.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("dos"), { timeout: 1000 });
    const activeSpan = findWordSpan(container, "dos");
    expect(activeSpan).toBeTruthy();
    // Inline style.color contiene el highlight color (RGB del #19E0BC).
    expect(activeSpan.style.color).toMatch(/(19E0BC|25,\s*224,\s*188|0f9b83|15,\s*155,\s*131)/i);
  });

  it("none: no word-jump styling — line rendered as plain text", async () => {
    const refOut = { current: null };
    const { container } = render(
      <_Harness refOut={refOut} lyricsAnimation="none" />,
    );
    await act(async () => {
      refOut.current.current = {
        activeLine: "uno dos tres",
        activeStart: 0,
        activeEnd: 3,
        currentTime: 1.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("dos"), { timeout: 1000 });
    // No debe haber spans per-word con color #19E0BC (eso es la regresión
    // que estamos previniendo).
    const allSpans = [...container.querySelectorAll("span")];
    const hasKaraokeColor = allSpans.some((el) => /19E0BC|25,\s*224,\s*188/i.test(el.style.color || ""));
    expect(hasKaraokeColor).toBe(false);
    // `none` also means no line-level fade/translate. Besides matching the
    // selected option, this prevents a composited text-layer rectangle from
    // flashing over moving video when the active line changes.
    const lyric = [...container.querySelectorAll("div")].find(
      (el) => el.classList.contains("font-extrabold") && el.textContent.includes("uno dos tres"),
    );
    expect(lyric).toBeTruthy();
    expect(lyric.style.animation).toBe("");
  });

  it("pop: line rendered plain (the line-level animation runs on the wrapper, not per word)", async () => {
    const refOut = { current: null };
    const { container } = render(
      <_Harness refOut={refOut} lyricsAnimation="pop" />,
    );
    await act(async () => {
      refOut.current.current = {
        activeLine: "uno dos tres",
        activeStart: 0,
        activeEnd: 3,
        currentTime: 1.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("dos"), { timeout: 1000 });
    const allSpans = [...container.querySelectorAll("span")];
    const hasKaraokeColor = allSpans.some((el) => /19E0BC|25,\s*224,\s*188/i.test(el.style.color || ""));
    expect(hasKaraokeColor).toBe(false);
  });

  it("word_reveal: future words have opacity 0 until they're sung", async () => {
    const refOut = { current: null };
    const { container } = render(
      <_Harness refOut={refOut} lyricsAnimation="word_reveal" />,
    );
    await act(async () => {
      refOut.current.current = {
        activeLine: "uno dos tres cuatro",
        activeStart: 0,
        activeEnd: 4,
        // currentTime=1.5 → activeWordIdx=1 (="dos"); "tres"/"cuatro" deben estar invisibles.
        currentTime: 1.5,
      };
      await new Promise((res) => setTimeout(res, 80));
    });
    await waitFor(() => expect(container.textContent).toContain("tres"), { timeout: 1000 });
    const tresSpan = findWordSpan(container, "tres");
    const cuatroSpan = findWordSpan(container, "cuatro");
    expect(tresSpan).toBeTruthy();
    expect(cuatroSpan).toBeTruthy();
    // Las palabras futuras tienen opacity 0 inline.
    expect(tresSpan.style.opacity).toBe("0");
    expect(cuatroSpan.style.opacity).toBe("0");
    // La palabra activa "dos" es visible.
    const dosSpan = findWordSpan(container, "dos");
    expect(dosSpan.style.opacity).toBe("1");
  });
});
