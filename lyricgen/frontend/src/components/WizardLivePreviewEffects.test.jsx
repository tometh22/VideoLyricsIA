import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import WizardLivePreview, { PIXEL_TRANSFORM_EFFECTS } from "./WizardLivePreview";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

afterEach(cleanup);

const PHOTO = "/library/operator-selected-photo.jpg";

describe("WizardLivePreview — Motion Lab v2 photo contract", () => {
  it.each([...PIXEL_TRANSFORM_EFFECTS])(
    "%s marks the stage as a photo-derived transformation",
    (effect) => {
      const { getByTestId, container } = render(
        <WizardLivePreview
          effect={effect}
          movementStyle="estatico"
          clipSrc={PHOTO}
          clipIsVideo={false}
        />,
      );
      const stage = getByTestId("photo-effect-stage");
      expect(stage.dataset.photoTransform).toBe(effect);
      // Every derived layer must use the operator-selected photo. The generic
      // preview placeholder is forbidden for fixed-photo effects.
      const photos = [...stage.querySelectorAll("img")];
      expect(photos.length).toBeGreaterThan(0);
      expect(photos.every((image) => image.getAttribute("src") === PHOTO)).toBe(true);
      expect(container.querySelector('video[src="/preview_base.mp4"]')).toBeNull();
      // Most transforms use the production loop as an auxiliary light layer.
      // Foto viva and Tinta viva use their loops as compositor masks; showing
      // those MP4s directly would create a second, dishonest dark overlay.
      const rawLoop = container.querySelector(`video[src="/fx_raw/${effect}.mp4"]`);
      if (effect === "foto_viva" || effect === "ink_reveal") {
        expect(rawLoop).toBeNull();
      } else {
        expect(rawLoop).toBeTruthy();
      }
    },
  );

  it("glitch and echo visibly derive multiple layers from the selected photo", () => {
    for (const effect of ["rgb_glitch", "chromatic_pulse", "cutout_echo"]) {
      const { getByTestId, unmount } = render(
        <WizardLivePreview
          effect={effect}
          movementStyle="estatico"
          clipSrc={PHOTO}
          clipIsVideo={false}
        />,
      );
      expect(getByTestId("photo-effect-stage").querySelectorAll("img").length)
        .toBeGreaterThanOrEqual(3);
      unmount();
    }
  });

  it("Foto viva duplicates only a travelling subject window from the selected photo", () => {
    const { getByTestId, container } = render(
      <WizardLivePreview
        effect="foto_viva"
        movementStyle="foto-parallax"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    const stage = getByTestId("photo-effect-stage");
    expect(stage.querySelectorAll(`img[src="${PHOTO}"]`).length).toBeGreaterThanOrEqual(2);
    expect(container.querySelector(".wlp-living-window")).toBeTruthy();
    expect(container.querySelector(".wlp-living-subject")).toBeTruthy();
  });

  it("Kaleido fills the stage with four overflow-clipped panels", () => {
    const { getByTestId } = render(
      <WizardLivePreview
        effect="kaleido"
        movementStyle="estatico"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    const grid = getByTestId("kaleido-grid");
    expect(grid.className).toContain("inset-0");
    const panels = [...grid.querySelectorAll(".wlp-kaleido-panel")];
    expect(panels).toHaveLength(4);
    expect(panels.every((panel) => panel.className.includes("inset-0"))).toBe(true);
    expect(panels.every((panel) => panel.style.clipPath)).toBe(true);
    expect(grid.querySelectorAll(`img[src="${PHOTO}"]`)).toHaveLength(4);
  });

  it("Tinta viva applies the treatment through a CSS mask without a dark raw overlay", () => {
    const { container } = render(
      <WizardLivePreview
        effect="ink_reveal"
        movementStyle="estatico"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    expect(container.querySelector(".wlp-ink-reveal")).toBeTruthy();
    expect(container.querySelector('video[src="/fx_raw/ink_reveal.mp4"]')).toBeNull();
  });

  it("Pulso cromático derives restrained red/cyan contour layers", () => {
    const { container } = render(
      <WizardLivePreview
        effect="chromatic_pulse"
        movementStyle="estatico"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    expect(container.querySelector(".wlp-chromatic-edge-red")).toBeTruthy();
    expect(container.querySelector(".wlp-chromatic-edge-cyan")).toBeTruthy();
  });

  it("RGB Glitch keeps the base stable and reveals channel shifts only in bursts", () => {
    const { container } = render(
      <WizardLivePreview
        effect="rgb_glitch"
        movementStyle="estatico"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    expect(container.querySelector(".wlp-rgb-red")).toBeTruthy();
    expect(container.querySelector(".wlp-rgb-cyan")).toBeTruthy();
    const raw = container.querySelector('video[src="/fx_raw/rgb_glitch.mp4"]');
    expect(raw.style.animation).toContain("wlp-rgb-overlay");
    expect(Number(raw.style.opacity)).toBeLessThanOrEqual(0.18);
  });

  it("legacy particles remain honest overlay effects", () => {
    const { getByTestId } = render(
      <WizardLivePreview
        effect="snow"
        movementStyle="estatico"
        clipSrc={PHOTO}
        clipIsVideo={false}
      />,
    );
    expect(getByTestId("photo-effect-stage").dataset.photoTransform).toBe("overlay");
  });
});
