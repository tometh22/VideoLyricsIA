// useLookVariant: catálogo con respaldo, /resolve con debounce y descarte de
// respuestas viejas, /detect-genre una vez por canción.
import { renderHook, act, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLookVariant, RESOLVE_DEBOUNCE_MS } from "./useLookVariant";
import { STATIC_LOOK_CATALOG, _resetLookVariantCaches } from "../lib/lyricLookVariants";

const json = (data, ok = true) => Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve(data) });

const CATALOG = {
  families: [{ id: "heavy", label: "Rock / metal" }],
  genres: [{ id: "metal", family: "heavy", label: "Metal" }],
  fonts: [{ id: "metal-mania", label: "Metal Mania", css_family: "Metal Mania", weight: 400, families: ["heavy"] }],
  recommended: { heavy: ["cinetico"] },
};

function resolvedFor(body, extra = {}) {
  return {
    look: body.lyric_look, genre: body.lyric_genre || "metal", family: "heavy",
    genre_source: body.lyric_genre ? "operator" : "detected",
    variant: body.lyric_variant === "" ? 2 : body.lyric_variant,
    description: `v${body.lyric_variant}`,
    fonts: { main: { id: "rubik-dirt", css_family: "Rubik Dirt", weight: 400 }, key: { id: "metal-mania", css_family: "Metal Mania", weight: 400 } },
    colors: { ink: "#F2EEE6", alt: "#CFC9BE", accent: "#D0101E" },
    overridden: {}, background: "source", warnings: [],
    ...extra,
  };
}

let calls;
function mockFetch({ catalogOk = true, resolveDelay = () => 0, detect = { genre: "metal", family: "heavy", source: "ai" } } = {}) {
  calls = { catalog: 0, resolve: [], detect: [] };
  globalThis.fetch = vi.fn((url, init = {}) => {
    if (url.endsWith("/lyric-looks/catalog")) {
      calls.catalog += 1;
      return catalogOk ? json(CATALOG) : Promise.reject(new TypeError("network"));
    }
    if (url.endsWith("/lyric-looks/detect-genre")) {
      calls.detect.push(JSON.parse(init.body));
      return json(detect);
    }
    if (url.endsWith("/lyric-looks/resolve")) {
      const body = JSON.parse(init.body);
      calls.resolve.push(body);
      const delay = resolveDelay(body);
      return new Promise((res) => setTimeout(() => res({ ok: true, status: 200, json: () => Promise.resolve(resolvedFor(body)) }), delay));
    }
    return json({}, false);
  });
}

const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
};
const advance = async (ms) => {
  await act(async () => { vi.advanceTimersByTime(ms); });
  await flush();
};

const realFetch = globalThis.fetch;
beforeEach(() => {
  _resetLookVariantCaches();
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  globalThis.fetch = realFetch;
});

describe("useLookVariant — catálogo", () => {
  it("se pide una vez por sesión y se comparte entre montajes", async () => {
    mockFetch();
    const a = renderHook(() => useLookVariant({ look: "" }));
    await flush();
    expect(a.result.current.catalog.fonts[0].id).toBe("metal-mania");
    renderHook(() => useLookVariant({ look: "" }));
    await flush();
    expect(calls.catalog).toBe(1);
  });

  it("si falla, la UI sigue con el catálogo estático", async () => {
    mockFetch({ catalogOk: false });
    const { result } = renderHook(() => useLookVariant({ look: "cinetico" }));
    await flush();
    expect(result.current.catalog).toBe(STATIC_LOOK_CATALOG);
  });
});

describe("useLookVariant — /resolve", () => {
  it("sin look no resuelve nada", async () => {
    mockFetch();
    const { result } = renderHook(() => useLookVariant({ look: "" }));
    await advance(RESOLVE_DEBOUNCE_MS * 2);
    expect(calls.resolve).toHaveLength(0);
    expect(result.current.resolved).toBeNull();
  });

  it("debounce: cambios seguidos disparan UN pedido, con los últimos valores", async () => {
    mockFetch();
    const { result, rerender } = renderHook((p) => useLookVariant(p), {
      initialProps: { look: "cinetico", genre: "metal", jobId: "job-1", artist: "A", songTitle: "T" },
    });
    rerender({ look: "cinetico", genre: "metal", variant: 1, jobId: "job-1", artist: "A", songTitle: "T" });
    rerender({ look: "cinetico", genre: "metal", variant: 2, jobId: "job-1", artist: "A", songTitle: "T" });
    await advance(RESOLVE_DEBOUNCE_MS - 10);
    expect(calls.resolve).toHaveLength(0);
    await advance(20);
    expect(calls.resolve).toHaveLength(1);
    expect(calls.resolve[0]).toMatchObject({ lyric_look: "cinetico", lyric_genre: "metal", lyric_variant: 2, job_id: "job-1" });
    await advance(1);
    expect(result.current.resolved.variant).toBe(2);
    expect(result.current.resolved._request).toEqual({ artist: "A", songTitle: "T", jobId: "job-1" });
  });

  it("una respuesta vieja que llega tarde no pisa a la más nueva", async () => {
    // El pedido de variant=1 tarda 1 s; el de variant=2, 10 ms.
    mockFetch({ resolveDelay: (b) => (b.lyric_variant === 1 ? 1000 : 10) });
    const { result, rerender } = renderHook((p) => useLookVariant(p), {
      initialProps: { look: "cinetico", genre: "metal", variant: 1 },
    });
    await advance(RESOLVE_DEBOUNCE_MS + 1); // sale el pedido de variant=1
    rerender({ look: "cinetico", genre: "metal", variant: 2 });
    await advance(RESOLVE_DEBOUNCE_MS + 20); // sale y vuelve el de variant=2
    expect(result.current.resolved.variant).toBe(2);
    await advance(1500); // vuelve (tarde) el de variant=1
    expect(result.current.resolved.variant).toBe(2);
  });

  it("si /resolve falla conserva el último resultado bueno", async () => {
    mockFetch();
    const { result, rerender } = renderHook((p) => useLookVariant(p), {
      initialProps: { look: "cinetico", genre: "metal", variant: 1 },
    });
    await advance(RESOLVE_DEBOUNCE_MS + 5);
    expect(result.current.resolved.variant).toBe(1);
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) }));
    rerender({ look: "cinetico", genre: "metal", variant: 2 });
    await advance(RESOLVE_DEBOUNCE_MS + 5);
    expect(result.current.resolved.variant).toBe(1);
    expect(result.current.error).toBe(true);
  });

  it("un resultado de otro look no se expone", async () => {
    mockFetch();
    const { result, rerender } = renderHook((p) => useLookVariant(p), {
      initialProps: { look: "cinetico", genre: "metal" },
    });
    await advance(RESOLVE_DEBOUNCE_MS + 5);
    expect(result.current.resolved.look).toBe("cinetico");
    rerender({ look: "neon", genre: "metal" });
    expect(result.current.resolved).toBeNull();
  });
});

describe("useLookVariant — /detect-genre", () => {
  it("con look y género automático detecta UNA vez por canción (cache artista|título)", async () => {
    mockFetch();
    const props = { look: "cinetico", genre: "", artist: "Almafuerte", songTitle: "Toro y Pampa", lyricsText: "letra" };
    const a = renderHook(() => useLookVariant(props));
    await flush();
    expect(calls.detect).toHaveLength(1);
    expect(calls.detect[0]).toMatchObject({ artist: "Almafuerte", song_title: "Toro y Pampa", lyrics_text: "letra" });
    await flush();
    expect(a.result.current.detected).toMatchObject({ genre: "metal", family: "heavy" });
    // Otro montaje (o el editor) para la misma canción usa la cache.
    const b = renderHook(() => useLookVariant({ ...props, songTitle: "toro y pampa " }));
    await flush();
    expect(calls.detect).toHaveLength(1);
    expect(b.result.current.detected.genre).toBe("metal");
  });

  it("no detecta sin look ni con género elegido por el operador", async () => {
    mockFetch();
    renderHook(() => useLookVariant({ look: "", artist: "A", songTitle: "T" }));
    renderHook(() => useLookVariant({ look: "cinetico", genre: "pop", artist: "A", songTitle: "T" }));
    await flush();
    expect(calls.detect).toHaveLength(0);
  });

  it("al terminar la detección se vuelve a resolver (el backend toma el género detectado)", async () => {
    mockFetch();
    renderHook(() => useLookVariant({ look: "cinetico", artist: "A", songTitle: "T" }));
    await advance(RESOLVE_DEBOUNCE_MS + 5);
    await advance(RESOLVE_DEBOUNCE_MS + 5);
    expect(calls.resolve.length).toBeGreaterThanOrEqual(1);
    // Nunca se manda el detectado como si lo hubiera elegido el operador.
    expect(calls.resolve.every((b) => b.lyric_genre === "")).toBe(true);
  });
});
