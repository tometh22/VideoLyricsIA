/**
 * Contrato de lib/persistSegments::persistSegments (PR F).
 *
 * ANTES (hasta PR F): este archivo testeaba un MIRROR inline
 * (`makePersistSegmentsToBackend`) que todavía contenía el ECO post-200
 * (`setCurrentReview({...prev, segments})`) — comportamiento ELIMINADO en la
 * auditoría 2026-06-10. O sea, el test verde no protegía nada de producción:
 * afirmaba un contrato que el código real ya no implementa.
 *
 * AHORA: testeamos la función REAL extraída (authFetch inyectado), sin mirror.
 * El eco no existe: un 200 retorna { ok: true } y no escribe nada de vuelta
 * (la frescura vive en el segmentsStore desde PR E). Guardamos el contrato de
 * retorno del que depende toda la cadena saveStatus/banner + la cola (PR F).
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { persistSegments } from "./lib/persistSegments";

const API = "https://api.test";
const SEGMENTS = [
  { start: 0, end: 5, text: "linea 1" },
  { start: 5, end: 10, text: "linea 2" },
];

describe("persistSegments — contrato real (sin eco a currentReview)", () => {
  let authFetch;
  beforeEach(() => { authFetch = vi.fn(); });

  const persist = (jobId, segments, opts) =>
    persistSegments(authFetch, API, jobId, segments, opts);

  it("POSTea a /jobs/{id}/save-segments con base_revision y retorna la revisión", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    const result = await persist("abc123", SEGMENTS);
    expect(authFetch).toHaveBeenCalledTimes(1);
    const [url, init] = authFetch.mock.calls[0];
    expect(url).toBe(`${API}/jobs/abc123/save-segments`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toHaveProperty("segments");
    expect(JSON.parse(init.body)).toHaveProperty("base_revision", 0);
    expect(result).toEqual({ ok: true, applied: true, revision: 1 });
  });

  it("NO escribe nada de vuelta en éxito (el eco fue removido)", async () => {
    // Contrato clave: el retorno es SOLO { ok: true }. Nada de segments/echo.
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    const result = await persist("abc123", SEGMENTS);
    expect(result).toEqual({ ok: true, applied: true, revision: 1 });
    expect(result.segments).toBeUndefined();
  });

  it("pasa keepalive al fetch cuando opts.keepalive === true", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    await persist("abc123", SEGMENTS, { keepalive: true });
    expect(authFetch.mock.calls[0][1].keepalive).toBe(true);
  });

  it("keepalive es false por defecto", async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    await persist("abc123", SEGMENTS);
    expect(authFetch.mock.calls[0][1].keepalive).toBe(false);
  });

  it("404 (job reapeado) → { ok: false, reason: 'job-gone', status: 404 }", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 404 });
    expect(await persist("abc123", SEGMENTS)).toEqual({
      ok: false, reason: "job-gone", status: 404,
    });
  });

  it("401 → { ok: false, reason: 'http-401', status: 401 } (el copy lo mapea a sesión)", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 401 });
    expect(await persist("abc123", SEGMENTS)).toEqual({
      ok: false, reason: "http-401", status: 401,
    });
  });

  it("5xx → { ok: false, reason: 'http-500', status: 500 }", async () => {
    authFetch.mockResolvedValue({ ok: false, status: 500 });
    expect(await persist("abc123", SEGMENTS)).toEqual({
      ok: false, reason: "http-500", status: 500,
    });
  });

  it("400 captura el detail del body sin romper el contrato", async () => {
    authFetch.mockResolvedValue({
      ok: false, status: 400,
      clone: () => ({ json: async () => ({ detail: "segments[7] out of range" }) }),
    });
    expect(await persist("abc123", SEGMENTS)).toEqual({
      ok: false, reason: "http-400", status: 400,
    });
  });

  it("fetch que tira → { ok: false, reason: 'network' } con el error", async () => {
    authFetch.mockRejectedValue(new Error("ECONNRESET"));
    const r = await persist("abc123", SEGMENTS);
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("network");
    expect(r.error).toContain("ECONNRESET");
  });

  it("input inválido → { ok: false, reason: 'no-data' } sin tocar la red", async () => {
    expect(await persist("", SEGMENTS)).toEqual({ ok: false, reason: "no-data" });
    expect(await persist("abc", [])).toEqual({ ok: false, reason: "no-data" });
    expect(await persist("abc", null)).toEqual({ ok: false, reason: "no-data" });
    expect(await persist(null, SEGMENTS)).toEqual({ ok: false, reason: "no-data" });
    expect(authFetch).not.toHaveBeenCalled();
  });

  it("sanitiza al contrato ANTES del POST: un segmento fuera de rango degrada, no rechaza", async () => {
    // Un valor suelto (end < start) NO debe tirar todo el save — se clampea y
    // el POST igual sale (incidente 2026-06-26, Universal AR).
    authFetch.mockResolvedValue({ ok: true, status: 200 });
    const bad = [{ start: 5, end: 1, text: "invertido" }];
    const result = await persist("abc123", bad);
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ ok: true, applied: true, revision: 1 });
  });

  it("409 expone conflicto OCC y la revisión actual", async () => {
    authFetch.mockResolvedValue({
      ok: false,
      status: 409,
      clone: () => ({ json: async () => ({ current_revision: 4, updated_at: "2026-07-22T12:00:00Z" }) }),
    });
    expect(await persist("abc123", SEGMENTS, { baseRevision: 3 })).toEqual({
      ok: false,
      reason: "stale-revision",
      status: 409,
      currentRevision: 4,
      updatedAt: "2026-07-22T12:00:00Z",
    });
  });

  it("nunca obtiene una revisión fresca para sobrescribir un conflicto", async () => {
    authFetch.mockResolvedValue({
      ok: false,
      status: 409,
      clone: () => ({ json: async () => ({ current_revision: 7 }) }),
    });
    const result = await persist("abc123", SEGMENTS, { baseRevision: 3, resolveConflict: true });
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(authFetch.mock.calls[0][0]).toBe(`${API}/jobs/abc123/save-segments`);
    expect(JSON.parse(authFetch.mock.calls[0][1].body).base_revision).toBe(3);
    expect(result).toMatchObject({ ok: false, reason: "stale-revision", currentRevision: 7 });
  });
});
