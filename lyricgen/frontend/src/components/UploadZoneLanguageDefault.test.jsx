import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UploadZone from "./UploadZone";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));
vi.mock("./OnboardingTour", () => ({
  UploadTour: () => null,
  EditorTour: () => null,
}));
vi.mock("./WizardLivePreview", () => ({ default: () => null }));
vi.mock("./TitleCardPreview", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("./Listbox", () => ({ default: () => null }));
vi.mock("../lib/telemetryTrack", () => ({ track: () => {} }));

function Harness() {
  const [files, setFiles] = useState([]);
  return (
    <>
      <output data-testid="language-value">
        {files.length ? JSON.stringify(files[0].language) : "missing"}
      </output>
      <UploadZone
        files={files}
        onFiles={setFiles}
        user={{ features: {} }}
        allHaveArtist={false}
        onStartReview={() => {}}
        onGenerateDirect={() => {}}
        onUploadAdvance={() => {}}
      />
    </>
  );
}

afterEach(() => cleanup());

describe("default transcription language", () => {
  it("uploads a new track in Auto instead of silently forcing Spanish", async () => {
    render(<Harness />);
    const input = document.querySelector('input[type="file"]');
    expect(input).toBeTruthy();

    fireEvent.change(input, {
      target: {
        files: [
          new File(["audio"], "Sisters (Live)_Divididos.wav", {
            type: "audio/wav",
          }),
        ],
      },
    });

    await waitFor(() => {
      expect(screen.getByTestId("language-value").textContent).toBe('""');
    });
  });
});
