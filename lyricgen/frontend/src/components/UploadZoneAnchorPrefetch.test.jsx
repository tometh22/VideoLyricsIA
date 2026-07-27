/**
 * Fix del bug staging e77f84aefe33: el prefetch de transcripción se
 * disparaba al SOLTAR el archivo (cuando la fuente de letra todavía era el
 * default "IA" y la letra oficial no estaba pegada) → el job salía sin
 * anclar y servía Versión A pese a la letra oficial.
 *
 * Ahora el prefetch se dispara al AVANZAR del paso "Subí" (goStep →
 * onUploadAdvance), con la fuente de letra + la letra oficial ya resueltas
 * por canción. Estos tests confirman, a nivel de UploadZone:
 *   1. Avanzar del paso 1 dispara onUploadAdvance con el estado ya resuelto
 *      → el POST /transcribe-uploaded (construido con el helper real
 *      anchorLyricsForEntry) sale con anchor_lyrics NO vacío.
 *   2. Soltar el archivo NO dispara transcripción (no hay prefetch al drop).
 *   3. Edge case (a): editar la fuente de letra / la letra oficial tras
 *      avanzar invalida el prefetch de esa canción (onInvalidatePrefetch).
 */
import { useState, useRef } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";
import { anchorLyricsForEntry } from "../lib/anchorPayload";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => "", lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({
  UploadTour: () => null,
  EditorTour: () => null,
}));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("./Listbox", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

const FLAG_USER = { features: { anchor_lyrics: true } };

function makeEntry() {
  return {
    file: { name: "cancion.mp3", lastModified: 1, size: 1000 },
    artist: "Intoxicados",
    songTitle: "Nunca quise",
    language: "es",
  };
}

afterEach(() => cleanup());

// Harness que simula lo que hace App: mantiene `files`, dispara un "prefetch"
// (fetch mockeado) al avanzar de paso construyendo el body con el helper
// REAL anchorLyricsForEntry, e invalida por archivo. Reproduce el borde
// exacto del bug (elegir official DESPUÉS del drop) y verifica que el anchor
// llega porque el disparo es al avance, no al drop.
function PrefetchHarness({ fetchSpy, onInvalidate }) {
  const [files, setFiles] = useState([makeEntry()]);
  const filesRef = useRef(files);
  filesRef.current = files;
  return (
    <UploadZone
      files={files}
      onFiles={setFiles}
      user={FLAG_USER}
      allHaveArtist
      onStartReview={() => {}}
      onGenerateDirect={() => {}}
      onUploadAdvance={() => {
        // Mismo camino que prefetchRemaining en App: POST por canción con
        // anchor_lyrics derivado del helper compartido.
        for (const entry of filesRef.current) {
          fetchSpy("/transcribe-uploaded", {
            method: "POST",
            body: JSON.stringify({ anchor_lyrics: anchorLyricsForEntry(entry) }),
          });
        }
      }}
      onInvalidatePrefetch={onInvalidate}
    />
  );
}

describe("prefetch de transcripción al avanzar del paso Subí", () => {
  it("elegir 'letra oficial' + pegar y avanzar → POST con anchor_lyrics no vacío", () => {
    const fetchSpy = vi.fn();
    render(<PrefetchHarness fetchSpy={fetchSpy} onInvalidate={() => {}} />);

    // El operador elige la letra oficial y la pega DESPUÉS del drop — el
    // escenario que rompía antes.
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));
    fireEvent.change(document.querySelector("textarea"), {
      target: { value: "linea uno\nlinea dos\nlinea tres" },
    });
    // Nada se transcribió todavía (no hay prefetch al drop ni al editar).
    expect(fetchSpy).not.toHaveBeenCalled();

    // Avanza del paso "Subí" con "Continuar".
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchSpy.mock.calls[0];
    expect(url).toBe("/transcribe-uploaded");
    const body = JSON.parse(opts.body);
    expect(body.anchor_lyrics).toBe("linea uno\nlinea dos\nlinea tres");
  });

  it("con fuente IA (default) al avanzar → POST con anchor_lyrics vacío", () => {
    const fetchSpy = vi.fn();
    render(<PrefetchHarness fetchSpy={fetchSpy} onInvalidate={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchSpy.mock.calls[0][1].body).anchor_lyrics).toBe("");
  });

  it("edge case (a): editar la fuente de letra invalida el prefetch de esa canción", () => {
    const onInvalidate = vi.fn();
    render(<PrefetchHarness fetchSpy={vi.fn()} onInvalidate={onInvalidate} />);
    // Elegir official invalida (por si ya había prefetch de esa canción).
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));
    expect(onInvalidate).toHaveBeenCalled();
    expect(onInvalidate.mock.calls[0][0]).toMatchObject({ name: "cancion.mp3" });

    // Editar la letra oficial también invalida.
    onInvalidate.mockClear();
    fireEvent.change(document.querySelector("textarea"), {
      target: { value: "otra letra" },
    });
    expect(onInvalidate).toHaveBeenCalled();
  });
});
