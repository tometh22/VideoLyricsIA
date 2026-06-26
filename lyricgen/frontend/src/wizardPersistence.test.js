import { describe, it, expect, beforeEach } from "vitest";
import { save, load, clear } from "./wizardPersistence";

describe("wizardPersistence — enableScenes roundtrip (audit B1)", () => {
  beforeEach(() => clear());

  it("persiste enableScenes=true y lo restaura", () => {
    save({ files: [], enableScenes: true, style: "neon" });
    const snap = load();
    expect(snap).toBeTruthy();
    expect(snap.topLevel.enableScenes).toBe(true);
  });

  it("persiste enableScenes=false explícito", () => {
    save({ files: [], enableScenes: false });
    expect(load().topLevel.enableScenes).toBe(false);
  });

  it("default cuando no se pasa (undefined) → false, no se pierde la key", () => {
    save({ files: [] });
    const snap = load();
    // La key existe (no es undefined) → el guard != null del restore funciona.
    expect(snap.topLevel.enableScenes).toBe(false);
  });

  it("no rompe el resto del topLevel", () => {
    save({ files: [], enableScenes: true, style: "oscuro", customColors: "#fff,#000" });
    const tl = load().topLevel;
    expect(tl.style).toBe("oscuro");
    expect(tl.customColors).toBe("#fff,#000");
    expect(tl.enableScenes).toBe(true);
  });
});
