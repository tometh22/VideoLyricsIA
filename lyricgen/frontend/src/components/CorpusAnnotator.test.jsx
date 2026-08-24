// Smoke tests for the standalone corpus-annotation page. This component is
// deliberately isolated from LyricsEditor.jsx (see corpus.py docstring) —
// these tests only cover CorpusAnnotator's own contract with the backend
// endpoints in lyricgen/backend/corpus.py.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import CorpusAnnotator from "./CorpusAnnotator";

function renderAt(token) {
  return render(
    <MemoryRouter initialEntries={[`/annotate/${token}`]}>
      <Routes>
        <Route path="/annotate/:token" element={<CorpusAnnotator />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockFetchByUrl(routes) {
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    for (const [pattern, response] of routes) {
      if (typeof pattern === "string" ? url.includes(pattern) : pattern.test(url)) {
        return Promise.resolve(response);
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("CorpusAnnotator", () => {
  it("shows a friendly message for an invalid/expired link", async () => {
    mockFetchByUrl([
      [/\/annotate\/bad-token$/, { ok: false, status: 404, json: async () => ({}) }],
    ]);

    renderAt("bad-token");

    await waitFor(() => expect(screen.getByText(/este link no funciona/i)).toBeTruthy());
  });

  it("greets the annotator and lists assigned songs", async () => {
    mockFetchByUrl([
      [/\/annotate\/good-token$/, { ok: true, json: async () => ({ name: "Marina" }) }],
      [/\/annotate\/good-token\/songs$/, {
        ok: true,
        json: async () => ({
          annotator_name: "Marina",
          songs: [
            { id: 1, artist: "Artista X", title: "Canción Uno", my_status: "not_started", my_segment_count: 0 },
            { id: 2, artist: "Artista Y", title: "Canción Dos", my_status: "submitted", my_segment_count: 12 },
          ],
        }),
      }],
    ]);

    renderAt("good-token");

    await waitFor(() => expect(screen.getByText(/hola, marina/i)).toBeTruthy());
    expect(screen.getByText("Canción Uno")).toBeTruthy();
    expect(screen.getByText("Canción Dos")).toBeTruthy();
    expect(screen.getByText("✓ Enviada")).toBeTruthy();
  });

  it("shows a retry screen instead of hanging forever when the network fails", async () => {
    // Regression test for the 24-ago incident: a rejected fetch (not a 404,
    // an actual thrown/rejected promise — a real network blip) left the
    // real annotator staring at "Cargando…" forever, with no error and no
    // way to retry. The unhandled rejection never updated `phase`.
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));

    renderAt("good-token");

    await waitFor(() => expect(screen.getByText(/no se pudo cargar/i)).toBeTruthy());
    expect(screen.queryByText(/^Cargando…$/)).toBeFalsy();

    // Reintentar debe disparar la carga de nuevo, no quedar pegado.
    mockFetchByUrl([
      [/\/annotate\/good-token$/, { ok: true, json: async () => ({ name: "Marina" }) }],
      [/\/annotate\/good-token\/songs$/, {
        ok: true,
        json: async () => ({ annotator_name: "Marina", songs: [] }),
      }],
    ]);
    fireEvent.click(screen.getByText(/reintentar/i));

    await waitFor(() => expect(screen.getByText(/hola, marina/i)).toBeTruthy());
  });

  it("opens a song and loads audio + waveform + existing draft", async () => {
    mockFetchByUrl([
      [/\/annotate\/good-token$/, { ok: true, json: async () => ({ name: "Marina" }) }],
      [/\/annotate\/good-token\/songs$/, {
        ok: true,
        json: async () => ({
          annotator_name: "Marina",
          songs: [{ id: 1, artist: "Artista X", title: "Canción Uno", my_status: "draft", my_segment_count: 1 }],
        }),
      }],
      [/\/songs\/1$/, {
        ok: true,
        json: async () => ({
          song: { id: 1, artist: "Artista X", title: "Canción Uno" },
          annotation: {
            segments: [{ start: 0, end: 1.5, text: "hola", event_type: "lexical" }],
            status: "draft",
          },
        }),
      }],
      [/\/songs\/1\/audio-url$/, { ok: true, json: async () => ({ url: "https://fake/audio.mp3", expires_in: 3600 }) }],
      [/\/songs\/1\/waveform$/, { ok: true, json: async () => ({ peaks: [0.1, 0.2, 0.3], duration: 10 }) }],
    ]);

    renderAt("good-token");

    await waitFor(() => expect(screen.getByText("Canción Uno")).toBeTruthy());
    fireEvent.click(screen.getByText("Canción Uno").closest("button"));

    await waitFor(() => expect(screen.getByText(/frases marcadas \(1\)/i)).toBeTruthy());
    expect(screen.getByText("hola")).toBeTruthy();
  });
});
