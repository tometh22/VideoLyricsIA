import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const puts = [];
vi.mock("../r2Upload", () => ({
  putToR2WithProgress: vi.fn(async (url, blob, contentType, onProgress) => {
    puts.push({ url, size: blob.size, contentType });
    if (url.includes("broken")) throw Object.assign(new Error("R2 PUT failed: 403"), { status: 403 });
    onProgress?.(blob.size, blob.size);
    return { etag: url.includes("noetag") ? null : `"etag-${url.split("/").pop()}"` };
  }),
  withRetry: async (fn) => fn(0),
}));

const {
  guessFromFilename, inspectAudioFiles, metadataIssue, parseAudioFilename, parseMetadataCsv, uploadCampaignAudios,
} = await import("./campaignUpload");

const file = (name, size = 10, content = name) => {
  const value = new File([new Uint8Array(size).fill(content.length % 250)], name, { type: "audio/wav" });
  return value;
};

describe("filename contract (same as batch_manifest.parse_audio_filename)", () => {
  it("parses glued and underscored technical codes", () => {
    expect(parseAudioFilename("Que Pasó_Bersuit VergarabatARF149800014.wav")).toMatchObject({ title: "Que Pasó", artist: "Bersuit Vergarabat", technical_code: "ARF149800014" });
    expect(parseAudioFilename("Eso Es Real (Live)_Los Pericos_ARF040000028.wav")).toMatchObject({ title: "Eso Es Real (Live)", artist: "Los Pericos", technical_code: "ARF040000028" });
    expect(parseAudioFilename("folder/Tema_Artistaarum12.mp3")).toMatchObject({ filename: "Tema_Artistaarum12.mp3", technical_code: "ARUM12" });
  });
  it("rejects names without code or separator", () => {
    expect(() => parseAudioFilename("Tema sin codigo.wav")).toThrow(/technical/);
    expect(() => parseAudioFilename("TemaARF12.wav")).toThrow(/separator/);
    expect(guessFromFilename("Charly García - Promesas sobre el bidet.wav")).toEqual({ artist: "Charly García", title: "Promesas sobre el bidet" });
    expect(guessFromFilename("solo_titulo.wav")).toEqual({ artist: "", title: "solo titulo" });
  });
  it("reads a metadata sheet with semicolons, quotes and BOM", () => {
    const rows = parseMetadataCsv("﻿Archivo;Título;Artista;Código\n\"tema, uno.wav\";\"Tema, Uno\";Artista;arf1\n\notro.wav;Otro;;ARF2");
    expect(rows.get("tema, uno.wav")).toEqual({ title: "Tema, Uno", artist: "Artista", technical_code: "ARF1" });
    expect(rows.get("otro.wav")).toEqual({ title: "Otro", artist: "", technical_code: "ARF2" });
    expect(parseMetadataCsv("titulo,artista\nA,B").size).toBe(0);
  });
  it("classifies only blocking or incomplete rows", () => {
    const ok = { size_bytes: 10, duration_seconds: 100, title: "a", artist: "b", technical_code: "ARF1" };
    expect(metadataIssue(ok)).toBeNull();
    expect(metadataIssue({ ...ok, technical_code: " " })).toBe("missing_metadata");
    expect(metadataIssue({ ...ok, size_bytes: 600 * 1024 * 1024 })).toBe("invalid_size");
    expect(metadataIssue({ ...ok, duration_seconds: 4000 })).toBe("invalid_duration");
  });
});

describe("inspectAudioFiles", () => {
  it("merges name and sheet metadata, ignores non-audio and dedupes identical content", async () => {
    const progress = [];
    const entries = await inspectAudioFiles([
      file("Uno_ArtistaARF1.wav"), file("portada.jpg"), file("Dos sin codigo.wav"), file("copia.wav"),
    ], {
      csvRows: parseMetadataCsv("filename,title,artist,code\nDos sin codigo.wav,Dos,Otra,ARF2"),
      hash: async (value) => (value.name === "copia.wav" ? "a".repeat(64) : value.name === "Uno_ArtistaARF1.wav" ? "a".repeat(64) : "b".repeat(64)),
      duration: async () => 180,
      onProgress: (value) => progress.push(value),
    });
    expect(entries).toHaveLength(2);
    expect(entries[0]).toMatchObject({ title: "Uno", artist: "Artista", technical_code: "ARF1", duration_seconds: 180, client_id: "a".repeat(64) });
    expect(entries[1]).toMatchObject({ title: "Dos", artist: "Otra", technical_code: "ARF2" });
    expect(progress.at(-1)).toMatchObject({ index: 3, total: 3 });
  });
});

describe("uploadCampaignAudios", () => {
  let calls;
  beforeEach(() => {
    calls = [];
    puts.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url: String(url), headers: options.headers, body });
      const ok = (value) => ({ ok: true, status: 200, json: async () => value });
      if (String(url).endsWith("/manifest")) {
        return ok({ items: body.items.map((item) => ({ client_id: item.client_id, item_id: `item-${item.client_id.slice(0, 6)}`, duplicate: item.client_id.startsWith("dup") })), registered_count: body.items.length });
      }
      if (String(url).includes("/ticket")) {
        const id = String(url).split("/uploads/")[1].split("/")[0];
        if (id.startsWith("item-multi")) return ok({ complete: false, use_multipart: true, part_size: 4, content_type: "audio/wav", uploaded_parts: [{ part_number: 1, etag: "done-1" }], parts: [1, 2, 3].map((n) => ({ part_number: n, url: `https://r2/${id}/${n}` })) });
        if (id.startsWith("item-noeta")) return ok({ complete: false, use_multipart: true, part_size: 100, parts: [{ part_number: 1, url: "https://r2/noetag/1" }] });
        if (id.startsWith("item-broke")) return ok({ complete: false, use_multipart: false, upload_url: "https://r2/broken", content_type: "audio/wav" });
        if (id.startsWith("item-alrea")) return ok({ complete: true });
        return ok({ complete: false, use_multipart: false, upload_url: `https://r2/${id}`, content_type: "audio/wav" });
      }
      if (String(url).includes("/complete")) return ok({ ok: true });
      throw new Error(`unexpected ${url}`);
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  const entry = (id, extra = {}) => ({ file: file(`${id}.wav`, extra.size || 10), filename: `${id}.wav`, title: "T", artist: "A", technical_code: "ARF1", size_bytes: extra.size || 10, duration_seconds: 120, sha256: id.padEnd(64, "0"), client_id: id.padEnd(64, "0"), ...extra });

  it("registers with the campaign token, uploads, resumes multipart parts and reports errors per file", async () => {
    const updates = [];
    const result = await uploadCampaignAudios("camp1", [
      entry("simple"), entry("multi", { size: 12 }), entry("broken"), entry("already"), entry("dup"), entry("huge", { size: 600 * 1024 * 1024 }),
    ], { openSession: async () => "upload-token", onProgress: (value) => updates.push(value), concurrency: 2 });
    const manifest = calls.find((call) => call.url.endsWith("/batch/campaigns/camp1/manifest"));
    expect(manifest.headers["X-Batch-Upload-Token"]).toBe("upload-token");
    expect(manifest.body.items).toHaveLength(5);
    expect(manifest.body.items.map((item) => item.filename)).not.toContain("huge.wav");
    expect(result).toMatchObject({ phase: "done", uploaded: 4, failed: 1, duplicates: 1, skipped: 1 });
    expect(result.errors).toEqual([{ filename: "broken.wav", message: "R2 PUT failed: 403" }]);
    // Part 1 already landed; only parts 2 and 3 are sent again.
    expect(puts.filter((put) => put.url.includes("item-multi")).map((put) => put.url.split("/").pop())).toEqual(["2", "3"]);
    const complete = calls.find((call) => call.url.includes("item-multi") && call.url.endsWith("/complete"));
    expect(complete.body.parts).toEqual([{ part_number: 1, etag: "done-1" }, { part_number: 2, etag: "etag-2" }, { part_number: 3, etag: "etag-3" }]);
    expect(updates.at(-1).bytesDone).toBe(updates.at(-1).bytesTotal - 10);
  });

  it("splits manifests in chunks of 100 and refuses to complete a multipart upload without ETag", async () => {
    const many = Array.from({ length: 150 }, (_, index) => entry(`s${String(index).padStart(3, "0")}`));
    const result = await uploadCampaignAudios("camp1", [...many, entry("noetag")], { openSession: async () => "t", concurrency: 4 });
    expect(calls.filter((call) => call.url.endsWith("/manifest")).map((call) => call.body.items.length)).toEqual([100, 51]);
    expect(result.uploaded).toBe(150);
    expect(result.errors[0].message).toMatch(/ETag/);
  });
});
