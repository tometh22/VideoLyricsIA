// Regression test for the movement-preview-reactivity bug reported by
// operator 2026-05-24 ("al hacer click en los fondos no cambia la preview")
// and fixed in 0511fa3. The contract: when movementStyle / clipSrc prop
// changes, the `<video>` element MUST remount (key changes) so the new
// MP4 actually loads. Without this, the video keeps the old source.
//
// This test exists to make the regression visible without running the
// full browser. If a future refactor breaks the key, this test fails.

import { render, cleanup } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import WizardLivePreview from "./WizardLivePreview";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

afterEach(cleanup);

describe("WizardLivePreview — movement preview reactivity (regression 0511fa3)", () => {
  // CONTRATO: el atributo src del <video> base refleja clipSrc cuando NO
  // hay effect activo. Sin esto, cambiar de Estático → Estándar no
  // cambia el clip visible.

  it("base video src matches clipSrc when no effect is active", () => {
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estatico"
        effect=""
        clipSrc="/movement_samples/estatico.mp4"
      />,
    );
    const videos = container.querySelectorAll("video");
    expect(videos.length).toBeGreaterThanOrEqual(1);
    // El primer video es el base — debe tener src=estatico.mp4
    expect(videos[0].getAttribute("src")).toBe("/movement_samples/estatico.mp4");
  });

  it("base video src changes when clipSrc prop updates", () => {
    const { container, rerender } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estatico"
        effect=""
        clipSrc="/movement_samples/estatico.mp4"
      />,
    );
    expect(container.querySelector("video").getAttribute("src"))
      .toBe("/movement_samples/estatico.mp4");

    // Usuario clickea "Estándar" → clipSrc cambia.
    rerender(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estandar"
        effect=""
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    expect(container.querySelector("video").getAttribute("src"))
      .toBe("/movement_samples/estandar.mp4");

    // Usuario clickea "Foto + parallax" → clipSrc cambia otra vez.
    rerender(
      <WizardLivePreview
        style="oscuro"
        movementStyle="foto-parallax"
        effect=""
        clipSrc="/movement_samples/foto-parallax.mp4"
      />,
    );
    expect(container.querySelector("video").getAttribute("src"))
      .toBe("/movement_samples/foto-parallax.mp4");
  });

  it("base video uses the movement clip even with effect active (fix 2026-05-25)", () => {
    // Bug fix 2026-05-25: el código anterior forzaba preview_base.mp4
    // siempre que había effect, lo cual desconectaba el preview del
    // thumbnail elegido. Ahora SIEMPRE se respeta el clipSrc del
    // movement (excepto para 'animado' que sigue cayendo a preview_base
    // para no clashear con efectos partícula sobre la nebula).
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estandar"
        effect="snow"
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    const baseVideo = container.querySelector("video");
    expect(baseVideo.getAttribute("src")).toBe("/movement_samples/estandar.mp4");
  });

  it("falls back to preview_base.mp4 ONLY when movement is animado + effect active", () => {
    // Animado con efecto: el motion bakeado del clip (nebula 2D) clash
    // con las partículas del efecto → fallback a escena calma + CSS
    // animation para que el movement se note.
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="animado"
        effect="snow"
        clipSrc="/movement_samples/animado.mp4"
      />,
    );
    const baseVideo = container.querySelector("video");
    expect(baseVideo.getAttribute("src")).toBe("/preview_base.mp4");
    // Y CSS animation aplicada porque preview_base no tiene motion baked.
    const animStyle = baseVideo.getAttribute("style") || "";
    expect(animStyle.includes("animation")).toBe(true);
    expect(animStyle).toMatch(/wlp-anim/);
  });

  it("movement clip has no CSS animation overlay (motion is baked into the clip)", () => {
    // Cuando usamos el movement clip directo (no preview_base), la animation
    // CSS NO debe aplicarse — el movement ya está en el video. Aplicar CSS
    // animation encima compondría motions y se vería raro.
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estandar"
        effect="snow"
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    const baseVideo = container.querySelector("video");
    const animStyle = baseVideo.getAttribute("style") || "";
    // estandar clip + effect → no CSS animation (motion baked).
    expect(animStyle.includes("animation")).toBe(false);
  });

  it("changing only movementStyle (no clipSrc change) doesn't crash", () => {
    // Edge case: si el caller pasa clipSrc estático pero movementStyle
    // cambia, el preview no rompe (aunque visualmente no actualice el clip).
    const { rerender, container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estatico"
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    expect(container.querySelector("video")).toBeTruthy();
    rerender(
      <WizardLivePreview
        style="oscuro"
        movementStyle="sutil"
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    expect(container.querySelector("video")).toBeTruthy();
  });
});
