/**
 * Tests del forwarding de console-tags a Sentry (observability 2026-06-10).
 *
 * Lección del bug del editor de sync: los warn de diagnóstico
 * ("[drag-persist] …") murieron en la consola de la operadora. Ahora los
 * warn TAGUEADOS suben como eventos agrupados; lo no-tagueado se
 * descarta; el throttle por tag protege la cuota ante loops.
 */
import { describe, expect, it } from "vitest";
import { consoleTagOf, shouldForwardConsoleEvent } from "./observability";

describe("consoleTagOf", () => {
  it("extrae el tag de los warn de diagnóstico reales", () => {
    expect(consoleTagOf("[drag-persist] prop-sync RESEEDING")).toBe("drag-persist");
    expect(consoleTagOf("[stale-bundle] vite:preloadError detected")).toBe("stale-bundle");
    expect(consoleTagOf("[OBS] Sentry initialized")).toBe("obs");
  });

  it("descarta lo no tagueado (ruido de libs/React)", () => {
    expect(consoleTagOf("Warning: Each child in a list should have a key")).toBe(null);
    expect(consoleTagOf("Failed prop type")).toBe(null);
    expect(consoleTagOf("")).toBe(null);
    expect(consoleTagOf(undefined)).toBe(null);
  });
});

describe("shouldForwardConsoleEvent", () => {
  it("forwardea la primera ocurrencia de un tag y trottlea la ráfaga", () => {
    const t0 = 1_000_000;
    const first = shouldForwardConsoleEvent("[test-ratelimit-a] ocurrencia 1", t0);
    expect(first).toEqual({ forward: true, tag: "test-ratelimit-a" });
    // El loop del editor habría emitido miles de estos en segundos:
    for (let i = 1; i <= 50; i++) {
      const burst = shouldForwardConsoleEvent("[test-ratelimit-a] ráfaga", t0 + i * 100);
      expect(burst.forward).toBe(false);
    }
    // Pasada la ventana de 60s, vuelve a pasar uno.
    const later = shouldForwardConsoleEvent("[test-ratelimit-a] post-ventana", t0 + 61_000);
    expect(later.forward).toBe(true);
  });

  it("tags distintos trottlean independiente", () => {
    const t0 = 2_000_000;
    expect(shouldForwardConsoleEvent("[test-tag-b] x", t0).forward).toBe(true);
    expect(shouldForwardConsoleEvent("[test-tag-c] y", t0).forward).toBe(true);
    expect(shouldForwardConsoleEvent("[test-tag-b] x2", t0 + 1).forward).toBe(false);
    expect(shouldForwardConsoleEvent("[test-tag-c] y2", t0 + 1).forward).toBe(false);
  });

  it("mensajes sin tag nunca se forwardean", () => {
    expect(shouldForwardConsoleEvent("warning suelto de una lib", 3_000_000))
      .toEqual({ forward: false, tag: null });
  });
});
