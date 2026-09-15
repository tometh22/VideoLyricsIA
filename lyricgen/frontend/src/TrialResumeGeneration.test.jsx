import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { I18nProvider } from "./i18n";
import { AlertProvider } from "./components/AlertProvider";
import * as wizardPersistence from "./wizardPersistence";

// Keep the real route, resume adapter, UploadZone controls, approval handler,
// generation builder and multipart request. Only unrelated chrome/media and
// the already-reviewed lyrics editor are stubbed; no submission logic is copied.
vi.mock("./components/Sidebar", () => ({ default: () => null }));
vi.mock("./components/WhatsNew/WhatsNewModal", () => ({ default: () => null }));
vi.mock("./components/WhatsNew/WhatsNewBell", () => ({ default: () => null }));
vi.mock("./components/HelpCenter/HelpButton", () => ({ default: () => null }));
vi.mock("./components/HelpCenter/HelpTip", () => ({ default: () => null }));
vi.mock("./components/ServiceStatusBanner", () => ({ default: () => null }));
vi.mock("./components/GiftCreditsBanner", () => ({ default: () => null }));
vi.mock("./components/BatchProgress", () => ({ default: () => <div>Generación enviada</div> }));
vi.mock("./components/OnboardingTour", () => ({ UploadTour: () => null, MotionStudioCoach: () => null, EditorTour: () => null }));
vi.mock("./components/WizardLivePreview", () => ({ default: () => null }));
vi.mock("./components/TitleCardPreview", () => ({ default: () => null, AUTO_INTRO_THRESHOLD_S: 5 }));
vi.mock("./components/LyricsEditor", () => ({
  default: ({ segments, onApprove }) => <button onClick={() => onApprove(segments)}>Aprobar letra revisada</button>,
}));
vi.mock("./lib/telemetryTrack", () => ({ track: vi.fn() }));
vi.mock("./r2Upload", () => ({ uploadFileToR2: vi.fn(async () => ({ jobId: "resume-trial" })) }));
vi.mock("./lib/fetchSse", () => ({ fetchSse: vi.fn(() => new Promise(() => {})), SseUnauthorizedError: class extends Error {} }));
vi.mock("./hooks/useBackgroundPreview", () => ({
  shouldEnableBackgroundPreview: () => false,
  useBackgroundPreview: () => ({ status: "idle", bgCacheKey: null }),
}));

const user = { id: 15, username: "trial-qa", role: "admin", plan: "unlimited", features: { scenes: true } };
const prompt = "Un barco rojo cruza el río y llega al faro";
let submitted;

function openReview(renderParams, { fresh = false } = {}) {
  let accepted = false;
  const job = {
    job_id: "resume-trial", status: "transcribed", filename: "song.wav",
    artist: "QA", song_title: "Río", language: "es", duration: 56,
    segments_revision: 0,
    segments: [{ id: "line-1", start: 0, end: 3, text: "Todo está tranquilo" }],
    ...(renderParams ? { render_params: renderParams } : {}),
  };
  vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
    const path = new URL(String(input), "https://trial.test").pathname;
    let body = {};
    if (path === "/auth/me") body = user;
    else if (path === "/usage") body = { plan: "unlimited" };
    else if (path === "/jobs") body = accepted
      ? [{ ...job, status: "queued" }, { job_id: "other-render", status: "processing" }]
      : [job];
    else if (path === "/status/resume-trial") body = { ...job, status: accepted ? "queued" : job.status };
    else if (path === "/transcribe-uploaded") body = { job_id: job.job_id, segments: job.segments, segments_revision: 0 };
    else if (path === "/generate" && options.method === "POST") {
      submitted = options.body;
      accepted = true;
      body = { job_id: job.job_id, status: "queued" };
    } else if (path.endsWith("/audio-url")) body = { url: "https://trial.test/audio.wav" };
    else if (path.includes("waveform")) body = { peaks: [], duration: 56 };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return render(<MemoryRouter initialEntries={[fresh ? "/new" : "/review/resume-trial"]}><I18nProvider><AlertProvider><App /></AlertProvider></I18nProvider></MemoryRouter>);
}

async function goStep(step) {
  await waitFor(() => expect(document.querySelector(`[data-wizard-step="${step}"]`)).not.toBeNull());
  fireEvent.click(document.querySelector(`[data-wizard-step="${step}"]`));
}

beforeEach(() => {
  submitted = null;
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("genly_token", `test.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }))}.test`);
  localStorage.setItem("genly_user", JSON.stringify(user));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

describe("trial review URL → wizard controls → real /generate request", () => {
  it.each(["resume-trial", "different-job"])("restores only matching saved creative settings (%s), never stale lyrics", async (savedJobId) => {
    wizardPersistence.save({
      currentReview: { transcribeJobId: savedJobId, artist: "Saved artist", textCase: "original",
        segments: [{ id: "stale", start: 0, end: 3, text: "Stale local lyrics" }] },
      wizardStage: "review", enableScenes: true, bgSelectMode: "auto",
    });
    openReview();
    await goStep(6);
    fireEvent.click(await screen.findByRole("button", { name: "Aprobar letra revisada" }));
    await waitFor(() => expect(submitted).not.toBeNull());
    const matches = savedJobId === "resume-trial";
    expect(submitted.get("enable_scenes")).toBe(matches ? "true" : null);
    expect(submitted.get("text_case")).toBe(matches ? "original" : "upper");
    expect(submitted.get("artist")).toBe(matches ? "Saved artist" : "QA");
    expect([...submitted.values()].join(" ")).not.toContain("Stale local lyrics");
  });

  it("preserves Multi-scene and typography when fresh transcription receives its canonical review URL", async () => {
    openReview(undefined, { fresh: true });
    await waitFor(() => expect(document.querySelector('input[type="file"]')).not.toBeNull());
    fireEvent.change(document.querySelector('input[type="file"]'), {
      target: { files: [new File(["qa"], "QA-song.mp3", { type: "audio/mpeg" })] },
    });
    fireEvent.change(await screen.findByPlaceholderText("Ej: Viejas Locas"), { target: { value: "Genly QA" } });
    await goStep(2);
    fireEvent.click(screen.getByRole("button", { name: /Multi-escena/ }));
    await goStep(4);
    fireEvent.click(document.querySelector('[data-text-case="original"]'));
    await goStep(5);
    fireEvent.click(screen.getByRole("button", { name: "Revisar letra antes de generar" }));
    const approve = await screen.findByRole("button", { name: "Aprobar letra revisada" });
    // Flush route canonicalization + any resume request before submitting.
    await waitFor(() => expect(screen.getByText("/review/resume-trial")).toBeInTheDocument());
    fireEvent.click(approve);
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted.get("enable_scenes")).toBe("true");
    expect(submitted.get("text_case")).toBe("original");
    expect(submitted.get("artist")).toBe("Genly QA");
  });

  it("uses the untouched literal default and refreshes active renders immediately after acceptance", async () => {
    openReview(); // No stored defaults or render_params: live QA reproduction.
    await goStep(2);
    fireEvent.click(screen.getByRole("button", { name: /Mi prompt/i }));
    const enhance = screen.getByRole("checkbox", { name: /Mejorar mi prompt/i });
    expect(enhance).not.toBeChecked();
    fireEvent.change(screen.getByPlaceholderText(/mansión surreal/i), { target: { value: prompt } });
    await goStep(3);
    fireEvent.click(screen.getByTestId("movement-picker-toggle"));
    fireEvent.click(document.querySelector('[data-movement="estatico"]'));
    await goStep(4);
    fireEvent.click(document.querySelector('[data-text-case="original"]'));
    await goStep(6);
    fireEvent.click(await screen.findByRole("button", { name: "Aprobar letra revisada" }));
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted.get("job_id")).toBe("resume-trial");
    expect(submitted.get("background_hint")).toBe(prompt);
    expect(submitted.get("bg_verbatim")).toBe("true");
    expect(submitted.get("movement_style")).toBe("estatico");
    expect(submitted.get("text_case")).toBe("original");
    await waitFor(() => expect(screen.getByRole("status", { name: "2 renders activos" })).toBeInTheDocument());
  });

  it.each([true, false])("restores saved bg_verbatim=%s in the checkbox and submitted request", async (literal) => {
    openReview({ background_hint: prompt, bg_verbatim: literal });
    await goStep(2);
    expect(screen.getByPlaceholderText(/mansión surreal/i)).toHaveValue(prompt);
    expect(screen.getByRole("checkbox", { name: /Mejorar mi prompt/i }).checked).toBe(!literal);
    await goStep(6);
    fireEvent.click(await screen.findByRole("button", { name: "Aprobar letra revisada" }));
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted.get("background_hint")).toBe(prompt);
    expect(submitted.get("bg_verbatim")).toBe(literal ? "true" : null);
  });
});
