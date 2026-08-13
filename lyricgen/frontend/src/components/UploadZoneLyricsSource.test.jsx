/**
 * Versión B v2 — selector "Transcripción con IA de Genly" vs "Tengo la
 * letra oficial" en la fila de cada canción (iteración sobre #908: el
 * toggle colapsado era invisible y no comunicaba la alternativa).
 *
 * Contrato:
 *  - Gate: sin user.features.anchor_lyrics el selector NO se renderiza.
 *  - Default: opción A ("IA de Genly") seleccionada, sin textarea → el
 *    card no crece.
 *  - Elegir B expande el textarea (wiring a entry.anchorLyrics vía
 *    updateField) y muestra el contador de líneas; volver a A colapsa
 *    el textarea sin perder el texto (badge de líneas queda en el card B).
 */
import { useState } from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";

vi.mock("../i18n", () => ({
  useI18n: () => ({
    // Mantiene el test cerca del contrato real de i18n: el componente debe
    // entregar { n } para que nunca quede el placeholder visible en el estado listo.
    t: (key, vars) => key === "upload.anchor_lyrics_ready"
      ? "{n} líneas listas para sincronizar".replace("{n}", vars?.n ?? "{n}")
      : "",
    lang: "es",
  }),
}));
vi.mock("./OnboardingTour", () => ({
  UploadTour: () => null, MotionStudioCoach: () => null,
  EditorTour: () => null,
}));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("./Listbox", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

function makeEntry() {
  return {
    file: { name: "cancion.mp3", lastModified: 1, size: 1000 },
    artist: "Intoxicados",
    songTitle: "Nunca quise",
    language: "es",
  };
}

// Wrapper con estado real: onFiles recibe updaters funcionales (como App)
// y el re-render refleja la selección, igual que en producción.
function Harness({ user }) {
  const [files, setFiles] = useState([makeEntry()]);
  return (
    <UploadZone
      files={files}
      onFiles={setFiles}
      user={user}
      allHaveArtist
      onStartReview={() => {}}
      onGenerateDirect={() => {}}
      onUploadAdvance={() => {}}
    />
  );
}

const FLAG_USER = { features: { anchor_lyrics: true } };

afterEach(() => cleanup());

describe("selector de fuente de letra (IA de Genly vs letra oficial)", () => {
  it("sin features.anchor_lyrics no se muestra (gate del flag)", () => {
    render(<Harness user={{ features: {} }} />);
    expect(screen.queryByTestId("lyrics-source-0")).toBeNull();
  });

  it("default: IA de Genly seleccionada, sin textarea (el card no crece)", () => {
    render(<Harness user={FLAG_USER} />);
    expect(screen.getByTestId("lyrics-source-ai-0").getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("lyrics-source-official-0").getAttribute("aria-checked")).toBe("false");
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("elegir 'Tengo la letra oficial' expande el textarea y cuenta líneas", () => {
    render(<Harness user={FLAG_USER} />);
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));

    expect(screen.getByTestId("lyrics-source-official-0").getAttribute("aria-checked")).toBe("true");
    const textarea = document.querySelector("textarea");
    expect(textarea).toBeTruthy();
    expect(textarea.getAttribute("maxlength")).toBe("20000");

    fireEvent.change(textarea, {
      target: { value: "primera linea\nsegunda linea\n\n tercera linea " },
    });
    // Contador: 3 líneas no vacías (fallback "{n} líneas" del componente).
    expect(screen.getAllByText("3 líneas").length).toBeGreaterThan(0);
    expect(screen.getByText("3 líneas listas para sincronizar")).toBeTruthy();
  });

  it("no promete anclado exacto hasta que haya texto pegado", () => {
    render(<Harness user={FLAG_USER} />);
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));

    expect(screen.getByText("Pegá al menos una línea para activar la sincronización exacta.")).toBeTruthy();
  });

  it("volver a IA colapsa el textarea sin perder el texto pegado", () => {
    render(<Harness user={FLAG_USER} />);
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));
    fireEvent.change(document.querySelector("textarea"), {
      target: { value: "una\ndos\ntres" },
    });

    fireEvent.click(screen.getByTestId("lyrics-source-ai-0"));
    expect(screen.getByTestId("lyrics-source-ai-0").getAttribute("aria-checked")).toBe("true");
    expect(document.querySelector("textarea")).toBeNull();
    // El badge del card B conserva el conteo — el texto no se perdió.
    expect(screen.getAllByText("3 líneas").length).toBeGreaterThan(0);

    // Re-seleccionar B recupera el texto intacto.
    fireEvent.click(screen.getByTestId("lyrics-source-official-0"));
    expect(document.querySelector("textarea").value).toBe("una\ndos\ntres");
  });
});
