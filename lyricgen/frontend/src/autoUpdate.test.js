import { describe, it, expect, vi, afterEach } from "vitest";
import { extractAssetRefs, buildChanged, initAutoUpdate } from "./autoUpdate";

const htmlWith = (entry) =>
  `<!doctype html><html><head>` +
  `<script type="module" crossorigin src="/assets/${entry}"></script>` +
  `<link rel="stylesheet" href="/assets/index-aaa111.css">` +
  `</head><body><div id="root"></div></body></html>`;

describe("extractAssetRefs", () => {
  it("pulls the hashed js/css refs, deduped and sorted", () => {
    expect(extractAssetRefs(htmlWith("index-abc123.js"))).toEqual([
      "/assets/index-aaa111.css",
      "/assets/index-abc123.js",
    ]);
  });
  it("returns [] for non-string input", () => {
    expect(extractAssetRefs(null)).toEqual([]);
    expect(extractAssetRefs(undefined)).toEqual([]);
  });
});

describe("buildChanged", () => {
  it("false when the asset sets are identical (same build)", () => {
    const a = extractAssetRefs(htmlWith("index-abc123.js"));
    const b = extractAssetRefs(htmlWith("index-abc123.js"));
    expect(buildChanged(a, b)).toBe(false);
  });
  it("true when the entry hash changed (new deploy)", () => {
    const boot = extractAssetRefs(htmlWith("index-abc123.js"));
    const latest = extractAssetRefs(htmlWith("index-def456.js"));
    expect(buildChanged(boot, latest)).toBe(true);
  });
  it("false on empty/unparseable either side (never reload blindly)", () => {
    const a = extractAssetRefs(htmlWith("index-abc123.js"));
    expect(buildChanged(a, [])).toBe(false);
    expect(buildChanged([], a)).toBe(false);
    expect(buildChanged(a, null)).toBe(false);
  });
});

describe("initAutoUpdate", () => {
  afterEach(() => vi.restoreAllMocks());

  it("is a no-op in dev (never polls)", () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, text: async () => "" });
    initAutoUpdate({ env: { PROD: false } });
    expect(f).not.toHaveBeenCalled();
  });

  it("starts polling in prod (captures the boot build)", () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, text: async () => htmlWith("index-abc123.js") });
    initAutoUpdate({ env: { PROD: true } });
    expect(f).toHaveBeenCalledWith("/", { cache: "no-store" });
  });
});
