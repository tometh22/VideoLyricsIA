import { describe, it, expect } from "vitest";
import { CHANGELOG } from "./changelog";
import { translations } from "./i18n";

// Guard: toda clave i18n que una entrada del changelog referencia debe
// existir de verdad en los 3 idiomas. Sin esto, un typo en changelog.js
// no rompe nada visiblemente — t() devuelve la clave cruda como string
// (truthy), así que el fallback `|| "texto"` nunca dispara y el usuario
// ve "announce.motor2_hl2" en vez de la novedad real.
const LANGS = Object.keys(translations);

function keysOf(entry) {
  const keys = [entry.titleKey, entry.taglineKey, entry.bodyKey, entry.ctaKey];
  if (Array.isArray(entry.highlightKeys)) keys.push(...entry.highlightKeys);
  if (Array.isArray(entry.modalFeatures)) {
    for (const feature of entry.modalFeatures) keys.push(feature.titleKey, feature.bodyKey);
  }
  return keys.filter(Boolean);
}

describe("changelog i18n integrity", () => {
  it("cada entrada tiene id/date/titleKey", () => {
    for (const e of CHANGELOG) {
      expect(e.id, "falta id").toBeTruthy();
      expect(e.date, `${e.id}: falta date`).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(e.titleKey, `${e.id}: falta titleKey`).toBeTruthy();
    }
  });

  it("los ids son únicos (localStorage trackea por id)", () => {
    const ids = CHANGELOG.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("toda clave referenciada existe en los 3 idiomas", () => {
    const missing = [];
    for (const e of CHANGELOG) {
      for (const key of keysOf(e)) {
        for (const lang of LANGS) {
          if (!(key in translations[lang])) missing.push(`${e.id}: ${key} (${lang})`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("como mucho una entrada featured (el modal muestra la primera)", () => {
    const featured = CHANGELOG.filter((e) => e.featured);
    expect(featured.length).toBeLessThanOrEqual(1);
  });
});
