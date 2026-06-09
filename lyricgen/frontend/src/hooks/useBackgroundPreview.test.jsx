/**
 * Regresión del audit 2026-06-09 (familia del bug Ana M.): cuando el
 * operador cambia un param del fondo, el hook descarta el preview viejo
 * internamente pero ANTES no avisaba al consumer — currentReview.bgCacheKey
 * retenía el hash de los params viejos durante el debounce (10s). Si el
 * operador aprobaba ahí adentro, /generate mandaba el key viejo y el
 * backend cache-hiteaba el fondo de los params anteriores.
 *
 * Invariante: cualquier cambio de params dispara onCacheKey(null)
 * sincrónicamente, antes de que el nuevo preview responda.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { act } from "react";
import { useBackgroundPreview } from "./useBackgroundPreview";

const entryBase = {
  artist: "Rata Blanca",
  songTitle: "La Leyenda Del Hada Y El Mago",
  style: "auto",
  movementStyle: "",
  effect: "",
  customColors: "",
  genre: "",
  concept: "",
  backgroundHint: "",
  bgVerbatim: false,
  backgroundMode: "veo",
  animateImage: false,
  matchLyrics: true,
};

describe("useBackgroundPreview — stale cache key", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ bg_cache_key: "key-v1", cached: true }),
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("al cambiar un param notifica onCacheKey(null) sin esperar el debounce", async () => {
    const onCacheKey = vi.fn();
    const { rerender } = renderHook(
      ({ entry }) =>
        useBackgroundPreview(entry, {
          api: "http://test",
          authHeaders: () => ({}),
          onCacheKey,
          debounceMs: 100,
        }),
      { initialProps: { entry: entryBase } },
    );

    // Primer preview completa → el consumer guarda key-v1 (fresco).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(onCacheKey).toHaveBeenCalledWith("key-v1", { stale: false });

    onCacheKey.mockClear();
    // El operador cambia un param del fondo. El key viejo quedó obsoleto:
    // el consumer tiene que enterarse YA (no 100ms después).
    rerender({ entry: { ...entryBase, effect: "light" } });
    expect(onCacheKey).toHaveBeenCalledWith(null);
  });

  it("re-run del effect con params idénticos no dispara null espurio ni re-fetch (dedupe)", async () => {
    // Audit adversarial 2026-06-09: la versión anterior de este test era
    // vacua — rerenderear con los mismos VALORES no corre el effect (las
    // deps no cambian), así que pasaba aunque borraras el dedupe. Para
    // ejercitar el dedupe de verdad hay que forzar un re-run del effect
    // con params iguales: toggling `enabled` (que SÍ está en las deps).
    const onCacheKey = vi.fn();
    const { rerender } = renderHook(
      ({ entry, enabled }) =>
        useBackgroundPreview(entry, {
          enabled,
          api: "http://test",
          authHeaders: () => ({}),
          onCacheKey,
          debounceMs: 100,
        }),
      { initialProps: { entry: entryBase, enabled: true } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    onCacheKey.mockClear();

    // enabled false → true re-corre el effect con los mismos params: el
    // dedupe por paramsKey debe evitar null espurio y segundo POST.
    rerender({ entry: entryBase, enabled: false });
    rerender({ entry: entryBase, enabled: true });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(onCacheKey).not.toHaveBeenCalledWith(null);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("respuesta en vuelo de params VIEJOS llega después del cambio → se entrega con stale=true", async () => {
    // La race que el panel adversarial demostró con un test fallido: el
    // POST de /generate-preview no se cancela al cambiar params; su
    // respuesta llegaba después del onCacheKey(null) y re-envenenaba el
    // key. Ahora el hook la marca stale para que el consumer no la
    // escriba en la review actual (solo backfill de jobs ya aprobados).
    const onCacheKey = vi.fn();
    let resolveOldFetch;
    global.fetch = vi.fn()
      .mockImplementationOnce(() => new Promise((res) => { resolveOldFetch = res; }))
      .mockResolvedValue({
        ok: true,
        json: async () => ({ bg_cache_key: "key-v2", cached: true }),
      });

    const { rerender } = renderHook(
      ({ entry }) =>
        useBackgroundPreview(entry, {
          api: "http://test",
          authHeaders: () => ({}),
          onCacheKey,
          debounceMs: 100,
        }),
      { initialProps: { entry: entryBase } },
    );

    // El primer preview dispara el POST pero la respuesta queda en vuelo.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);

    // El operador cambia un param ANTES de que vuelva la respuesta.
    rerender({ entry: { ...entryBase, effect: "light" } });
    expect(onCacheKey).toHaveBeenCalledWith(null);
    onCacheKey.mockClear();

    // Llega la respuesta VIEJA: tiene que venir marcada stale=true.
    await act(async () => {
      resolveOldFetch({ ok: true, json: async () => ({ bg_cache_key: "key-v1", cached: true }) });
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(onCacheKey).toHaveBeenCalledWith("key-v1", { stale: true });

    // Y el preview de los params nuevos entrega su key como fresco.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(onCacheKey).toHaveBeenCalledWith("key-v2", { stale: false });
  });
});
