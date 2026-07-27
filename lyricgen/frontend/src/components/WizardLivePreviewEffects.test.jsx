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
      // The production loop remains present only as the auxiliary mask/light.
      expect(container.querySelector(`video[src="/fx_raw/${effect}.mp4"]`)).toBeTruthy();
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
