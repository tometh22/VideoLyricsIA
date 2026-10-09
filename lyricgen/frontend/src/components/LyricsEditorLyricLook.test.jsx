// Look de letra en la columna tipográfica del editor (modo standalone, sin
// hideTypographyControls): mismo contrato que el paso 4 del wizard.
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
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

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

const props = (overrides = {}) => ({
  segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
  filename: "song.mp3",
  audioFile: null,
  referenceLyrics: "",
  onApprove: vi.fn(),
  onBack: vi.fn(),
  ...overrides,
});

describe("LyricsEditor — look de letra", () => {
  it("se siembra del job y bloquea fuente / animación / transición", () => {
    render(<LyricsEditor {...props({ lyricLook: "cine", font: "anton", lyricsAnimation: "karaoke" })} />);
    const select = screen.getByTestId("editor-lyric-look");
    expect(select.value).toBe("cine");
    expect(screen.getByTestId("editor-look-locked")).toBeTruthy();
    const selects = [...document.querySelectorAll("select")];
    // Font, animación y transición deshabilitados; el look no.
    expect(selects.filter((s) => s.disabled)).toHaveLength(3);
    expect(select.disabled).toBe(false);
  });

  it("cambiar el look avisa al padre y 'Sin look' destraba los controles", () => {
    const onLyricLookChange = vi.fn();
    render(<LyricsEditor {...props({ lyricLook: "", onLyricLookChange })} />);
    const select = screen.getByTestId("editor-lyric-look");
    expect([...document.querySelectorAll("select")].filter((s) => s.disabled)).toHaveLength(0);

    fireEvent.change(select, { target: { value: "pop70" } });
    expect(onLyricLookChange).toHaveBeenLastCalledWith("pop70");
    expect([...document.querySelectorAll("select")].filter((s) => s.disabled)).toHaveLength(3);

    fireEvent.change(select, { target: { value: "" } });
    expect(onLyricLookChange).toHaveBeenLastCalledWith("");
    expect(screen.queryByTestId("editor-look-locked")).toBeNull();
  });
});

describe("LyricsEditor — video hecho sin fondo generado", () => {
  it("avisa al elegir un look que usa fondo (o 'Sin look'); no con los que pintan todo el cuadro", () => {
    const onLyricLookChange = vi.fn();
    render(<LyricsEditor {...props({ lyricLook: "pop70", backgroundOwnedByLook: true, onLyricLookChange })} />);
    const select = screen.getByTestId("editor-lyric-look");
    expect(screen.queryByTestId("editor-look-needs-bg")).toBeNull();
    fireEvent.change(select, { target: { value: "cinetico" } });
    expect(screen.queryByTestId("editor-look-needs-bg")).toBeNull();
    fireEvent.change(select, { target: { value: "neon" } });
    expect(screen.getByTestId("editor-look-needs-bg").textContent).toMatch(/regenerá el fondo en «Fondo»/);
    fireEvent.change(select, { target: { value: "" } });
    expect(screen.getByTestId("editor-look-needs-bg")).toBeTruthy();
    // Sólo informa: el cambio de look llega al padre igual.
    expect(onLyricLookChange).toHaveBeenLastCalledWith("");
  });

  it("sin la marca del job no avisa nada", () => {
    render(<LyricsEditor {...props({ lyricLook: "cine" })} />);
    expect(screen.queryByTestId("editor-look-needs-bg")).toBeNull();
  });
});
