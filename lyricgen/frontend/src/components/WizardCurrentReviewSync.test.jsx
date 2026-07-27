// Test del useEffect de App.jsx (Phase 2 2026-05-25) que sincroniza
// cambios en files[] (típicamente desde updateBatchDefault en
// UploadZone cuando el operador cambia font/case/animation en el
// paso 4 del stepper) hacia currentReview, para que el LyricsEditor
// montado en paso 6 vea los settings actualizados sin remount.
//
// La lógica vive inline en App.jsx (un useEffect adentro de un
// componente con cientos de hooks). Mountar App.jsx entero en test
// es impráctico — montaríamos auth, react-router, toast provider,
// useBackgroundPreview, y un montón de side-effects. En vez de eso,
// reproducimos el useEffect en un componente harness MÍNIMO y
// validamos su contrato. Si la lógica de App.jsx diverge, este
// archivo se actualiza con el mismo cambio.
//
// CONTRATO QUE PROTEGEMOS
//   1. Sin currentReview → useEffect noop.
//   2. Con currentReview pero sin match en files → noop.
//   3. Con match pero SIN drift (todos los campos iguales) → noop.
//   4. Con match Y drift en >=1 campo → setCurrentReview con los
//      campos mergeados desde el file.
//   5. Match por file.name (nuestra clave de join entre files[] y
//      currentReview).
//   6. Solo se mergean campos definidos en el archivo (undefined no
//      pisa el valor de currentReview).

import { render, act, cleanup } from "@testing-library/react";
import { afterEach, describe, it, expect } from "vitest";
import { useState, useEffect } from "react";

afterEach(cleanup);

// MIRROR EXACTO del useEffect en App.jsx (líneas ~382-403). Cualquier
// cambio en App.jsx debe replicarse aquí.
function _SyncFromFilesHarness({ files, initialReview, onReviewChange }) {
  const [currentReview, setCurrentReview] = useState(initialReview);

  useEffect(() => {
    if (!currentReview) return;
    const match = files.find(
      (f) => f?.file?.name === currentReview.file?.name,
    );
    if (!match) return;
    const fields = ["font", "textCase", "fontScale", "textContrast", "lyricsAnimation", "lineTransition"];
    const drift = fields.some((k) => (match[k] ?? "") !== (currentReview[k] ?? ""));
    if (!drift) return;
    setCurrentReview((r) => {
      if (!r) return r;
      const next = { ...r };
      for (const k of fields) {
        if (match[k] !== undefined) next[k] = match[k];
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, currentReview?.file?.name]);

  // Emit current state to test via callback (cleaner than reaching DOM).
  useEffect(() => {
    onReviewChange?.(currentReview);
  }, [currentReview, onReviewChange]);

  return null;
}

function _mockFile(name) {
  // Mimic enough of the File API for the join key (we only use .name).
  return { name };
}

describe("App.jsx currentReview ← files sync (Phase 2)", () => {
  it("does nothing when currentReview is null", () => {
    let lastReview = "sentinel";
    const onReviewChange = (r) => { lastReview = r; };
    render(
      <_SyncFromFilesHarness
        files={[{ file: _mockFile("song.mp3"), font: "anton" }]}
        initialReview={null}
        onReviewChange={onReviewChange}
      />,
    );
    // currentReview siguió siendo null (no se llamó setCurrentReview).
    expect(lastReview).toBeNull();
  });

  it("does nothing when no file in files[] matches currentReview.file.name", () => {
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    render(
      <_SyncFromFilesHarness
        files={[{ file: _mockFile("otra.mp3"), font: "bebas" }]}
        initialReview={{
          file: _mockFile("song.mp3"),
          font: "anton",
          textCase: "upper",
        }}
        onReviewChange={onReviewChange}
      />,
    );
    // El match falló — currentReview no se tocó.
    expect(lastReview.font).toBe("anton");
    expect(lastReview.textCase).toBe("upper");
  });

  it("does nothing when match exists but fields are identical (no drift)", () => {
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    render(
      <_SyncFromFilesHarness
        files={[{
          file: _mockFile("song.mp3"),
          font: "anton", textCase: "upper", fontScale: "1.0",
          textContrast: "medium", lyricsAnimation: "karaoke", lineTransition: "slide_up",
        }]}
        initialReview={{
          file: _mockFile("song.mp3"),
          font: "anton", textCase: "upper", fontScale: "1.0",
          textContrast: "medium", lyricsAnimation: "karaoke", lineTransition: "slide_up",
        }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("anton");
    expect(lastReview.lyricsAnimation).toBe("karaoke");
  });

  it("merges drifted fields from files into currentReview on initial mount", () => {
    // Escenario: el operador estuvo en paso 4 cambiando font ANTES de
    // empezar a transcribir, después clickeó "Revisar lyrics" y el
    // currentReview se construye desde otra fuente que quedó stale.
    // El useEffect lo arregla en el primer render.
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    render(
      <_SyncFromFilesHarness
        files={[{ file: _mockFile("song.mp3"), font: "bebas", textCase: "title" }]}
        initialReview={{
          file: _mockFile("song.mp3"),
          font: "anton",       // ← stale
          textCase: "upper",   // ← stale
        }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("bebas");
    expect(lastReview.textCase).toBe("title");
  });

  it("re-syncs when files[] mutates (operator changes font in step 4 during review)", () => {
    // Escenario crítico: operador EN paso 6 viendo review. Clickea
    // paso 4, cambia font de 'anton' a 'bebas' (updateBatchDefault →
    // fanea a files[*]). Vuelve a paso 6. El useEffect debe propagar
    // el cambio a currentReview para que el editor vea la font nueva.
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    const file = _mockFile("song.mp3");
    const { rerender } = render(
      <_SyncFromFilesHarness
        files={[{ file, font: "anton", textCase: "upper" }]}
        initialReview={{ file, font: "anton", textCase: "upper" }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("anton");
    // updateBatchDefault dispara onFiles((prev) => prev.map(...{font: "bebas"})).
    // Reproducimos eso: nueva referencia de files con el campo cambiado.
    act(() => {
      rerender(
        <_SyncFromFilesHarness
          files={[{ file, font: "bebas", textCase: "upper" }]}
          initialReview={{ file, font: "anton", textCase: "upper" }}
          onReviewChange={onReviewChange}
        />,
      );
    });
    expect(lastReview.font).toBe("bebas");
    expect(lastReview.textCase).toBe("upper");
  });

  it("preserves currentReview fields not present in files (segments, queueIdx, etc.)", () => {
    // currentReview tiene MÁS campos que los que el useEffect mergea
    // (segments, queueIdx, referenceLyrics, etc.). El merge debe ser
    // shallow + selectivo — no perdemos los segments cuando llega
    // un drift de font.
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    const file = _mockFile("song.mp3");
    render(
      <_SyncFromFilesHarness
        files={[{ file, font: "bebas" }]}
        initialReview={{
          file,
          font: "anton",  // ← drift
          segments: [{ start: 0, end: 2, text: "primera línea" }],
          queueIdx: 0,
          referenceLyrics: "letra completa de Genius",
        }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("bebas");
    expect(lastReview.segments).toHaveLength(1);
    expect(lastReview.segments[0].text).toBe("primera línea");
    expect(lastReview.queueIdx).toBe(0);
    expect(lastReview.referenceLyrics).toBe("letra completa de Genius");
  });

  it("does not pollute currentReview with undefined fields from files (regression guard)", () => {
    // Si el campo en files[i] es undefined (e.g., operator nunca tocó
    // textContrast en el wizard), NO debe pisar el valor que ya tenía
    // currentReview. La condición es `match[k] !== undefined`.
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    const file = _mockFile("song.mp3");
    render(
      <_SyncFromFilesHarness
        files={[{
          file,
          font: "bebas",  // ← solo este se setea
          // textContrast NO está definido en files
        }]}
        initialReview={{
          file,
          font: "anton",
          textContrast: "strong",  // ← previo, debe preservarse
        }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("bebas");
    expect(lastReview.textContrast).toBe("strong");
  });

  it("handles all six typography fields in one pass", () => {
    // Si el operador hace múltiples cambios en paso 4 antes de volver
    // a paso 6, el merge debe aplicar TODOS en un solo setCurrentReview.
    let lastReview = null;
    const onReviewChange = (r) => { lastReview = r; };
    const file = _mockFile("song.mp3");
    render(
      <_SyncFromFilesHarness
        files={[{
          file,
          font: "bebas",
          textCase: "title",
          fontScale: "1.3",
          textContrast: "strong",
          lyricsAnimation: "pop",
          lineTransition: "dissolve_blur",
        }]}
        initialReview={{
          file,
          font: "anton",
          textCase: "upper",
          fontScale: "1.0",
          textContrast: "medium",
          lyricsAnimation: "none",
          lineTransition: "none",
        }}
        onReviewChange={onReviewChange}
      />,
    );
    expect(lastReview.font).toBe("bebas");
    expect(lastReview.textCase).toBe("title");
    expect(lastReview.fontScale).toBe("1.3");
    expect(lastReview.textContrast).toBe("strong");
    expect(lastReview.lyricsAnimation).toBe("pop");
    expect(lastReview.lineTransition).toBe("dissolve_blur");
  });
});
