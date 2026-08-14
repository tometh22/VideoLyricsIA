// Cubre las garantías que antes vivían en EditRequestPanel y se movieron
// al helper compartido: unchanged-guard (que evita el doble-click de
// "re-renderizar igual"), preservación de layout (pos/scale/rot/locked)
// en el wire shape, y mapeo de errores del backend.

import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  submitLyricsEdit,
  normalizeSegmentsForEdit,
  segmentsUnchanged,
  layoutChanged,
  translateBackendError,
} from "./lyricsEditSubmit";

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("normalizeSegmentsForEdit", () => {
  it("strips _id and preserves locked/pos/scale/rot when present", () => {
    const out = normalizeSegmentsForEdit([
      { _id: 7, start: 1.5, end: 2.5, text: "uno", locked: true, pos: { x: 0.5, y: 0.3 } },
      { _id: 8, start: 3, end: 4, text: "dos", scale: 1.25, rot: 0.1 },
    ]);
    expect(out).toEqual([
      { start: 1.5, end: 2.5, text: "uno", locked: true, pos: { x: 0.5, y: 0.3 } },
      { start: 3, end: 4, text: "dos", scale: 1.25, rot: 0.1 },
    ]);
  });

  it("omits scale when it equals 1 and rot when 0 (default values)", () => {
    const out = normalizeSegmentsForEdit([
      { start: 0, end: 1, text: "x", scale: 1, rot: 0 },
    ]);
    expect(out[0]).toEqual({ start: 0, end: 1, text: "x" });
  });
});

describe("segmentsUnchanged", () => {
  it("considers segments equal when text + timings within 1ms match", () => {
    const a = [{ start: 1.0001, end: 2.0001, text: "hola" }];
    const b = [{ start: 1, end: 2, text: "hola" }];
    expect(segmentsUnchanged(a, b)).toBe(true);
  });

  it("detects text edits", () => {
    const a = [{ start: 0, end: 1, text: "hola" }];
    const b = [{ start: 0, end: 1, text: "chau" }];
    expect(segmentsUnchanged(a, b)).toBe(false);
  });

  it("detects timing edits past the 1ms tolerance", () => {
    const a = [{ start: 0, end: 1, text: "x" }];
    const b = [{ start: 0, end: 1.05, text: "x" }];
    expect(segmentsUnchanged(a, b)).toBe(false);
  });
});

describe("layoutChanged", () => {
  it("detects pos.x change", () => {
    const a = [{ pos: { x: 0.5, y: 0.5 } }];
    const b = [{ pos: { x: 0.6, y: 0.5 } }];
    expect(layoutChanged(a, b)).toBe(true);
  });

  it("returns false when only segment counts differ (caller treats as text change)", () => {
    const a = [{ pos: { x: 0.5, y: 0.5 } }];
    const b = [{ pos: { x: 0.5, y: 0.5 } }, { pos: { x: 0.5, y: 0.5 } }];
    expect(layoutChanged(a, b)).toBe(false);
  });
});

describe("translateBackendError", () => {
  it("maps the structured edit_in_progress conflict", () => {
    const out = translateBackendError(
      { code: "edit_in_progress", message: "An edit is already being rendered." },
      () => null,
    );
    expect(out).toMatch(/re-renderizando/);
  });

  it("maps both flat and nested editor revision conflicts", () => {
    const flat = translateBackendError(
      { code: "stale_revision", detail: "editor_revision_conflict" },
      () => null,
    );
    const nested = translateBackendError(
      { detail: { detail: "editor_revision_conflict", server_revision: 4 } },
      () => null,
    );
    expect(flat).toMatch(/La letra cambió/);
    expect(nested).toMatch(/La letra cambió/);
  });

  it("maps the 'no cached background' error to friendly Spanish copy", () => {
    const out = translateBackendError("No cached background available for job", () => null);
    expect(out).toMatch(/fondo cacheado/);
  });

  it("flattens Pydantic v2 array of errors into a single string", () => {
    const out = translateBackendError([
      { type: "value_error", loc: ["body", "x"], msg: "x debe ser positivo" },
      { type: "missing", loc: ["body", "y"], msg: "campo requerido" },
    ], () => null);
    expect(out).toBe("x debe ser positivo; campo requerido");
  });

  it("returns null when raw is null", () => {
    expect(translateBackendError(null, () => null)).toBeNull();
  });
});

describe("submitLyricsEdit", () => {
  it("returns {unchanged} when nothing changed against baseline (no double-click trap)", async () => {
    const segs = [{ start: 0, end: 1, text: "uno" }];
    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: segs,
      baselineSegments: segs,
      t: () => null,
    });
    expect(result.unchanged).toBe(true);
    expect(result.ok).toBe(false);
  });

  it("force=true bypasses unchanged guard and posts to /edit/:id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 202, json: async () => ({ status: "queued" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const segs = [{ start: 0, end: 1, text: "uno" }];
    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: segs,
      baselineSegments: segs,
      force: true,
      t: () => null,
    });
    expect(result.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toContain("/edit/job-A");
    const body = JSON.parse(opts.body);
    expect(body.edit_type).toBe("lyrics");
    expect(body.segments).toEqual([{ start: 0, end: 1, text: "uno" }]);
  });

  it("includes typography fields only when provided", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 202, json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);

    await submitLyricsEdit({
      jobId: "job-X",
      segments: [{ start: 0, end: 1, text: "x" }],
      baselineSegments: [{ start: 0, end: 1, text: "y" }], // text differs → real change
      font: "Bebas",
      lyricsAnimation: "karaoke",
      t: () => null,
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.font).toBe("Bebas");
    expect(body.lyrics_animation).toBe("karaoke");
    expect(body.text_case).toBeUndefined();
    expect(body.text_contrast).toBeUndefined();
    expect(body.line_transition).toBeUndefined();
  });

  it("returns {error} with friendly translation when backend rejects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 400, json: async () => ({ detail: "No cached background available" }),
    }));
    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: [{ start: 0, end: 1, text: "x" }],
      baselineSegments: [{ start: 0, end: 1, text: "y" }],
      t: () => null,
    });
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/fondo cacheado/);
  });

  it("retries with allow_youtube_drift on 409 if operator confirms", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: false, status: 409,
        json: async () => ({ detail: { code: "youtube_already_published", youtube_url: "https://yt/x" } }),
      })
      .mockResolvedValueOnce({
        ok: true, status: 202, json: async () => ({ status: "queued" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: [{ start: 0, end: 1, text: "x" }],
      baselineSegments: [{ start: 0, end: 1, text: "y" }],
      confirmYoutubeDrift: () => true,
      t: () => null,
    });
    expect(result.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondBody = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(secondBody.allow_youtube_drift).toBe(true);
  });

  it("returns {cancelled} when operator rejects the 409 confirm", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({
      ok: false, status: 409,
      json: async () => ({ detail: { code: "youtube_already_published" } }),
    }));
    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: [{ start: 0, end: 1, text: "x" }],
      baselineSegments: [{ start: 0, end: 1, text: "y" }],
      confirmYoutubeDrift: () => false,
      t: () => null,
    });
    expect(result.cancelled).toBe(true);
    expect(result.ok).toBe(false);
  });

  it("rejects empty segment lists without hitting the network", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const result = await submitLyricsEdit({
      jobId: "job-A",
      segments: [],
      baselineSegments: [],
      t: () => null,
    });
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/vac[ií]as/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
