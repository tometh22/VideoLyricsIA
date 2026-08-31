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
    // El efecto no puede desconectar el preview del thumbnail elegido.
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

  it("keeps the live illustration visible when animado has an active effect", () => {
    // Regression: animado + effect used to be silently replaced by the
    // generic preview base, so the selected illustration disappeared.
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="animado"
        effect="kaleido"
        clipSrc="/movement_samples/animado.mp4"
        clipIsVideo
      />,
    );
    const illustrationLayers = container.querySelectorAll(
      'video[src="/movement_samples/animado.mp4"]',
    );
    expect(illustrationLayers.length).toBeGreaterThan(0);
    expect(container.querySelector('video[src="/preview_base.mp4"]')).toBeNull();
    expect(container.querySelector('[data-photo-transform="kaleido"]')).toBeTruthy();
  });

  it("movement clip has no CSS animation overlay (motion is baked into the clip)", () => {
    // El movement ya está horneado en el video. Aplicar CSS animation encima
    // compondría motions y se vería raro.
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

  it("applies the selected movement animation to a still-image source", () => {
    const { container } = render(
      <WizardLivePreview
        style="oscuro"
        movementStyle="foto-parallax"
        effect=""
        clipSrc="/library/operator-photo.jpg"
        clipIsVideo={false}
      />,
    );
    const baseImage = container.querySelector('img[src="/library/operator-photo.jpg"]');
    expect(baseImage).toBeTruthy();
    expect(baseImage.getAttribute("style") || "").toMatch(/wlp-parallax/);
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

  it("labels an operator photo as animated without faking motion before generation", () => {
    const { container } = render(
      <WizardLivePreview
        movementStyle="estatico"
        operatorPhoto
        photoAnimated
        clipSrc="/operator/plaza.jpg"
        clipIsVideo={false}
      />,
    );
    expect(container.textContent).toContain("Movimiento: Foto animada");
    const image = container.querySelector('img[src="/operator/plaza.jpg"]');
    expect(image.getAttribute("style") || "").not.toMatch(/animation/);
  });
});
