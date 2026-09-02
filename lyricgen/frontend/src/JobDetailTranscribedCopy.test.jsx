/**
 * Regression test for PR B (fix/jobdetail-transcribed-cta-copy, 2026-05-26).
 *
 * Bug reported in prod: the operator clicked into a job he believed was
 * "approved" (e.g. job 595fb90f4aa9) and the UI redirected him to /new
 * (= the "create video" wizard). Root cause: the job was actually in
 * `transcribed_pending` (leftover orphan from the dedup bug fixed in
 * PR #388), and the CTA in JobDetail said "Editar lyrics y generar" —
 * which the operator read as "fix lyrics on the finished video" rather
 * than "continue the wizard you abandoned".
 *
 * Fix: rephrase the copy in i18n (3 locales) + JobDetail fallback
 * strings to make it inequivocally clear that the video was NEVER
 * generated and the button continues the wizard.
 *
 * This test pins the contract: the copy strings MUST NOT include
 * language that suggests the video exists / is approved. If anyone
 * tries to revert the copy to the old wording, this fails.
 */
import { describe, it, expect } from "vitest";
// Import the runtime i18n bundle so we test the actual production strings.
import { translations } from "./i18n.jsx";

const FORBIDDEN_PATTERNS_TITLE = [
  /listo para editar/i,           // implies "ready/approved, just retouch"
  /draft ready to edit/i,
  /rascunho pronto para editar/i,
];

const FORBIDDEN_PATTERNS_CTA = [
  /^editar lyrics y generar$/i,   // exact old copy
  /^edit lyrics and generate$/i,
  /^editar letras e gerar$/i,
];

const REQUIRED_TITLE_PHRASES = {
  es: "todavía no se generó",
  en: "hasn't been generated",
  pt: "ainda não foi gerado",
};

const REQUIRED_CTA_PHRASES = {
  es: "continuar",
  en: "continue",
  pt: "continuar",
};

describe("transcribed_pending CTA copy — must not suggest 'video already exists'", () => {
  for (const locale of ["es", "en", "pt"]) {
    describe(`locale: ${locale}`, () => {
      const dict = translations[locale];

      it("transcribed_title does not use the old 'ready to edit' framing", () => {
        const title = dict["detail.transcribed_title"];
        expect(title, "title must be defined in i18n").toBeTruthy();
        for (const pattern of FORBIDDEN_PATTERNS_TITLE) {
          expect(title, `title must not match ${pattern}`).not.toMatch(pattern);
        }
      });

      it("transcribed_title makes explicit the video was not generated", () => {
        const title = dict["detail.transcribed_title"].toLowerCase();
        expect(title, `title must mention "${REQUIRED_TITLE_PHRASES[locale]}"`)
          .toContain(REQUIRED_TITLE_PHRASES[locale]);
      });

      it("transcribed_cta does not use the old 'editar lyrics y generar' framing", () => {
        const cta = dict["detail.transcribed_cta"];
        expect(cta, "cta must be defined in i18n").toBeTruthy();
        for (const pattern of FORBIDDEN_PATTERNS_CTA) {
          expect(cta, `cta must not match ${pattern}`).not.toMatch(pattern);
        }
      });

      it("transcribed_cta uses 'continue' verb to signal wizard-continuation", () => {
        const cta = dict["detail.transcribed_cta"].toLowerCase();
        expect(cta, `cta must contain "${REQUIRED_CTA_PHRASES[locale]}"`)
          .toContain(REQUIRED_CTA_PHRASES[locale]);
      });

      it("transcribed_subtitle exists and is non-trivial", () => {
        const subtitle = dict["detail.transcribed_subtitle"];
        expect(subtitle, "subtitle must be defined").toBeTruthy();
        expect(subtitle.length, "subtitle should be substantive (>30 chars)")
          .toBeGreaterThan(30);
      });
    });
  }
});
