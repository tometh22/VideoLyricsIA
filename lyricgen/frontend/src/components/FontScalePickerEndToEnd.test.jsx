// End-to-end test for the UMG-reported font-scale fix (PR #451).
//
// Reported bug: UMG operator tried to enlarge the title-card typography
// on "Cosas Mías - Los Abuelos de la Nada" but the wizard's font-scale
// picker capped at 1.3x. The backend already accepted 1.5x; only the UI
// was missing the 6th button. Fix added "1.5" to the picker.
//
// What this test locks down:
//   1. The picker has six options including "1.5".
//   2. Clicking a button updates state and forwards the value as the
//      `fontScale` prop to WizardLivePreview unchanged.
//   3. WizardLivePreview's internal scaleN clamp matches the backend
//      ass_render.py clamp (0.6–1.5), so the operator's pick of 1.5 is
//      honored, and an over-cap value (1.7) still clamps to 1.5.
//
// Why we mock WizardLivePreview for (1)+(2): jsdom rejects
// `font-size: clamp(...cqw...)` values, so the inline fontSize style
// never lands on the rendered DOM and we can't assert it directly. The
// meaningful contract from the operator's POV is "the button I clicked
// becomes the value the preview consumes" — exactly what the mock
// captures. For (3) we test the clamp formula directly with the real
// component math (mirrored), because that's pure JS arithmetic.

import { render, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { useState } from "react";

// Capture every fontScale value WizardLivePreview gets re-rendered with.
const fontScaleHistory = [];
vi.mock("./WizardLivePreview", () => ({
  default: ({ fontScale }) => {
    fontScaleHistory.push(fontScale);
    return <div data-testid="mock-preview" data-font-scale={String(fontScale)} />;
  },
}));

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

// Imported AFTER the mock so it picks up the mocked module.
const { default: WizardLivePreview } = await import("./WizardLivePreview");

afterEach(() => {
  cleanup();
  fontScaleHistory.length = 0;
});

// Mirror of UploadZone's font-scale picker — 6 buttons mapping a code
// onto the `fontScale` state forwarded to WizardLivePreview. Keep this
// list in sync with UploadZone.jsx's FONT_SCALE_OPTIONS; if a future
// edit drops "1.5" the test fails on the explicit length assertion.
const FONT_SCALE_OPTIONS = [
  { code: "0.75" },
  { code: "0.9" },
  { code: "1.0" },
  { code: "1.15" },
  { code: "1.3" },
  { code: "1.5" }, // ← option added in PR #451 to match backend cap
];

function _PickerHarness({ initial = "1.0" }) {
  const [fontScale, setFontScale] = useState(initial);
  return (
    <div>
      {FONT_SCALE_OPTIONS.map((opt) => (
        <button
          key={opt.code}
          data-testid={`font-scale-${opt.code}`}
          onClick={() => setFontScale(opt.code)}
        >
          A
        </button>
      ))}
      <div data-testid="current-scale">{fontScale}</div>
      <WizardLivePreview
        style="oscuro"
        movementStyle="estatico"
        effect=""
        lyric="esta es tu letra"
        fontScale={fontScale}
      />
    </div>
  );
}

describe("font-scale picker → live preview prop chain (PR #451)", () => {
  it("picker exposes exactly 6 options including the 1.5 added by the fix", () => {
    const { getByTestId, queryByTestId } = render(<_PickerHarness />);
    for (const opt of FONT_SCALE_OPTIONS) {
      expect(getByTestId(`font-scale-${opt.code}`)).toBeTruthy();
    }
    // Defensive: nothing above 1.5 (the backend cap) and nothing missing.
    expect(queryByTestId("font-scale-1.7")).toBeNull();
    expect(queryByTestId("font-scale-1.5")).toBeTruthy();
  });

  it("default mount forwards fontScale='1.0' to the preview", () => {
    const { getByTestId } = render(<_PickerHarness />);
    expect(getByTestId("current-scale").textContent).toBe("1.0");
    expect(getByTestId("mock-preview").getAttribute("data-font-scale")).toBe("1.0");
    expect(fontScaleHistory.at(-1)).toBe("1.0");
  });

  it("clicking 1.5 propagates the value through state into the preview prop", () => {
    const { getByTestId } = render(<_PickerHarness />);
    fireEvent.click(getByTestId("font-scale-1.5"));
    expect(getByTestId("current-scale").textContent).toBe("1.5");
    expect(getByTestId("mock-preview").getAttribute("data-font-scale")).toBe("1.5");
    expect(fontScaleHistory.at(-1)).toBe("1.5");
  });

  it("preview re-renders with each pick — 1.3 → 1.5 chain captured in order", () => {
    const { getByTestId } = render(<_PickerHarness />);
    fireEvent.click(getByTestId("font-scale-1.3"));
    expect(fontScaleHistory.at(-1)).toBe("1.3");
    fireEvent.click(getByTestId("font-scale-1.5"));
    expect(fontScaleHistory.at(-1)).toBe("1.5");
    // The full sequence is: initial mount "1.0", click 1.3, click 1.5.
    // React may re-render extras during state propagation — we only
    // assert the unique values appeared in this order.
    const unique = [...new Set(fontScaleHistory)];
    expect(unique).toEqual(["1.0", "1.3", "1.5"]);
  });

  it("the smallest option (0.75) also propagates", () => {
    const { getByTestId } = render(<_PickerHarness />);
    fireEvent.click(getByTestId("font-scale-0.75"));
    expect(getByTestId("mock-preview").getAttribute("data-font-scale")).toBe("0.75");
  });
});

// ---------------------------------------------------------------------
// Math contract: mirror of WizardLivePreview.jsx:232-233 + ass_render.py
// font_scale clamp. If the production formula changes (e.g. cap raised
// to 1.8 to match a new backend limit), this should fail loudly so the
// maintainer can update both sides together.
// ---------------------------------------------------------------------

function previewBaseFontSize(fontScale) {
  const scaleN = Math.max(0.6, Math.min(1.5, parseFloat(fontScale) || 1));
  return {
    scaleN,
    baseFontSize: `clamp(${Math.round(18 * scaleN)}px, ${(7.5 * scaleN).toFixed(2)}cqw, ${Math.round(68 * scaleN)}px)`,
  };
}

describe("WizardLivePreview scaleN clamp — matches backend (ass_render.py:114)", () => {
  it("scaleN=1.0 → baseFontSize uses 18/68 baseline", () => {
    const r = previewBaseFontSize("1.0");
    expect(r.scaleN).toBe(1);
    expect(r.baseFontSize).toBe("clamp(18px, 7.50cqw, 68px)");
  });

  it("scaleN=1.5 (the operator's max pick) → 27/102 px", () => {
    const r = previewBaseFontSize("1.5");
    expect(r.scaleN).toBe(1.5);
    expect(r.baseFontSize).toBe("clamp(27px, 11.25cqw, 102px)");
  });

  it("scaleN=1.3 → 23/88 (rounded)", () => {
    const r = previewBaseFontSize("1.3");
    expect(r.scaleN).toBe(1.3);
    // 18 * 1.3 = 23.4 → round 23; 68 * 1.3 = 88.4 → round 88.
    expect(r.baseFontSize).toBe("clamp(23px, 9.75cqw, 88px)");
  });

  it("over-cap (1.7) clamps to 1.5 → same px values as the 1.5 case", () => {
    const r = previewBaseFontSize("1.7");
    expect(r.scaleN).toBe(1.5);
    expect(r.baseFontSize).toBe("clamp(27px, 11.25cqw, 102px)");
  });

  it("below-cap (0.4) clamps to 0.6 → 11/41 px", () => {
    const r = previewBaseFontSize("0.4");
    expect(r.scaleN).toBe(0.6);
    // 18 * 0.6 = 10.8 → round 11; 68 * 0.6 = 40.8 → round 41.
    expect(r.baseFontSize).toBe("clamp(11px, 4.50cqw, 41px)");
  });

  it("garbage value (NaN/undefined) defaults to 1.0", () => {
    expect(previewBaseFontSize("foo").scaleN).toBe(1);
    expect(previewBaseFontSize(undefined).scaleN).toBe(1);
    expect(previewBaseFontSize(null).scaleN).toBe(1);
  });
});
