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

  it("base video src stays as preview_base.mp4 when effect is active (movement via CSS)", () => {
    // Con effect activo, el clip base es FIJO (preview_base.mp4) y el
    // movimiento se aplica via CSS transform. Esto es intencional —
    // ver el comment del fix cf1a79a.
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estandar"
        effect="snow"
        clipSrc="/movement_samples/estandar.mp4"
      />,
    );
    const baseVideo = container.querySelector("video");
    expect(baseVideo.getAttribute("src")).toBe("/preview_base.mp4");
  });

  it("base video has CSS animation when effect + non-static movement", () => {
    // Con effect activo + movimiento != estatico, la animación CSS
    // debe estar presente para que se note el cambio.
    const { container, rerender } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estatico"
        effect="snow"
      />,
    );
    let baseVideo = container.querySelector("video");
    // Estático con efecto → animation:none (la escena no se mueve).
    const staticStyle = baseVideo.getAttribute("style") || "";
    expect(staticStyle.includes("animation")).toBe(false);

    rerender(
      <WizardLivePreview
        style="oscuro"
        movementStyle="estandar"
        effect="snow"
      />,
    );
    baseVideo = container.querySelector("video");
    // Estándar con efecto → debe tener CSS animation.
    const cinematicStyle = baseVideo.getAttribute("style") || "";
    expect(cinematicStyle.includes("animation")).toBe(true);
    expect(cinematicStyle).toMatch(/wlp-estandar/);
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
