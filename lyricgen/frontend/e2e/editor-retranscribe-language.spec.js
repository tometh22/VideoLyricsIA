import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

// Staging job 7d45ea03941b "Gran Ídolo De Asia": WhisperX heard English in a
// Spanish song. The campaign editor re-transcribes it with a chosen language.
// The API is a stateful mock at the browser boundary; nothing reaches staging.

const ENGLISH = [
  { segment_id: "one", start: 0.4, end: 1.6, text: "Great idol of Asia" },
  { segment_id: "two", start: 1.8, end: 3.2, text: "You never come back" },
];
const SPANISH = [
  { segment_id: "uno", start: 0.4, end: 1.6, text: "Gran ídolo de Asia" },
  { segment_id: "dos", start: 1.8, end: 3.2, text: "Nunca vas a volver" },
];

async function installRetranscribeApi(page, {
  jobId, initialStatus = "transcribed_pending", attempt = null, outcome = "applied", pollsUntilDone = 2,
  hold = false,
}) {
  const state = {
    hold, // while true the worker never finishes (lets a test observe progress)
    jobStatus: initialStatus,
    revision: 3,
    segments: ENGLISH,
    attempt, // null | { status, language, polls }
    posts: [],
    versions: [
      { id: "v3", revision: 3, reason: "manual", is_approved: false, created_at: "2026-11-18T12:03:00Z" },
      { id: "v2", revision: 2, reason: "manual", is_approved: false, created_at: "2026-11-18T12:02:00Z" },
      { id: "v0", revision: 0, reason: "transcription", is_approved: false, created_at: "2026-11-18T12:00:00Z" },
    ],
  };
  const finish = () => {
    if (outcome === "applied") {
      state.attempt.status = "applied";
      state.attempt.result_revision = 5;
      state.revision = 5;
      state.segments = SPANISH;
      state.jobStatus = "transcribed_pending";
      state.versions.unshift(
        { id: "v5", revision: 5, reason: "retranscription", is_approved: false, created_at: "2026-11-18T12:10:00Z" },
        { id: "v4", revision: 4, reason: "pre_retranscription", is_approved: false, created_at: "2026-11-18T12:05:00Z" },
      );
    } else {
      state.attempt.status = "failed";
      state.attempt.error = "No pudimos completar la transcripción. Código: RuntimeError.";
      state.jobStatus = "transcribed_pending";
    }
  };
  const editorPayload = () => ({
    job_id: jobId, revision: state.revision, segments: state.segments, original_segments: state.segments,
    latest_approved_version: null, updated_by: null, updated_at: new Date().toISOString(),
    lock: { active: false, user: null, expires_at: null }, job_status: state.jobStatus,
  });
  // Registered after the harness, so these handlers win.
  await page.route(`**/status/${jobId}`, (route) => route.fulfill({ json: {
    job_id: jobId, status: state.jobStatus, campaign_id: "retranscribe-campaign",
    campaign_item_id: "retranscribe-item", filename: "gran-idolo.wav", artist: "Fito Páez",
    song_title: "Gran Ídolo De Asia", segments_revision: state.revision, segments_json: state.segments,
    language_conflict: state.segments[0]?.text === ENGLISH[0].text, render_params: {},
  } }));
  await page.route(`**/editor/${jobId}`, (route) => {
    const request = route.request();
    if (request.method() === "GET") return route.fulfill({ json: editorPayload() });
    if (request.method() === "PATCH") {
      // The editor checkpoints before asking; keep one shared revision.
      const body = request.postDataJSON();
      if (body.base_revision !== state.revision) {
        return route.fulfill({ status: 409, json: { detail: { detail: "editor_revision_conflict",
          server_revision: state.revision, server_segments: state.segments } } });
      }
      const changed = JSON.stringify(body.segments) !== JSON.stringify(state.segments);
      if (changed) { state.revision += 1; state.segments = body.segments; }
      return route.fulfill({ json: { applied: changed, revision: state.revision,
        version_id: changed ? `v${state.revision}` : null, saved_at: new Date().toISOString() } });
    }
    return route.fallback();
  });
  await page.route(`**/editor/${jobId}/versions*`, (route) => route.fulfill({ json: { versions: state.versions } }));
  await page.route(`**/jobs/${jobId}/retranscribe-language`, async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      state.posts.push(body);
      if (body.base_revision !== state.revision) {
        return route.fulfill({ status: 409, json: { code: "stale_revision", message: "La versión guardada cambió." } });
      }
      state.attempt = { status: "queued", language: body.language, polls: 0 };
      state.jobStatus = "transcribing_queued";
      return route.fulfill({ status: 202, json: { ok: true, job_id: jobId, status: "queued", language: body.language } });
    }
    if (!state.attempt) return route.fulfill({ json: { status: "idle", job_status: state.jobStatus } });
    if (["queued", "running"].includes(state.attempt.status)) {
      state.attempt.polls += 1;
      state.attempt.status = "running";
      state.jobStatus = "transcribing";
      if (!state.hold && state.attempt.polls >= pollsUntilDone) finish();
    }
    return route.fulfill({ json: {
      status: state.attempt.status, language: state.attempt.language, job_status: state.jobStatus,
      progress: state.attempt.status === "running" ? 40 : 100, error: state.attempt.error || null,
      result_revision: state.attempt.result_revision || null,
    } });
  });
  return state;
}

// A cold Vite dev server compiles the editor on first visit; be generous.
const LOAD = { timeout: 40_000 };
test.describe.configure({ timeout: 120_000 });

async function openReview(page, jobId) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/review/${jobId}`);
  await page.getByTestId("editor-overflow-btn").waitFor(LOAD);
}

test("re-transcribes in Spanish: confirm, progress, reload with the new text, history checkpoint", async ({ page }) => {
  const jobId = "retranscribe01";
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true, initialRevision: 3, segments: ENGLISH });
  const api = await installRetranscribeApi(page, { jobId, pollsUntilDone: 2 });
  await openReview(page, jobId);

  const inline = page.getByTestId("retranscribe-inline");
  await expect(inline).toBeVisible(LOAD);
  await expect(inline.getByTestId("retranscribe-open")).toHaveText("Volver a transcribir en Español");
  await inline.getByTestId("retranscribe-open").click();

  const dialog = page.getByTestId("retranscribe-dialog");
  await expect(dialog).toContainText(
    "La versión actual queda guardada en el historial. Genly vuelve a escuchar la canción en Español y la letra nueva vuelve a revisión.",
  );
  await dialog.getByTestId("retranscribe-confirm").click();
  await expect(dialog).toContainText("Genly está escuchando la canción en Español");
  // One request, for Spanish, at the revision just checkpointed (the mock 409s otherwise).
  expect(api.posts).toHaveLength(1);
  expect(api.posts[0]).toMatchObject({ language: "es" });
  expect(api.posts[0].base_revision).toBeGreaterThanOrEqual(3);

  // Completion reloads the page; the editor now shows the Spanish lyrics.
  await expect(page.getByText("Gran ídolo de Asia").first()).toBeVisible(LOAD);
  await expect(page.getByTestId("retranscribe-dialog")).toHaveCount(0);
  await expect(page.getByTestId("retranscribe-inline")).toHaveCount(0); // no language alert anymore

  await page.getByTestId("editor-overflow-btn").click();
  await page.getByRole("menuitem", { name: /Historial de versiones/ }).click();
  await expect(page.getByText("Antes de volver a transcribir")).toBeVisible();
  await expect(page.getByText("Nueva transcripción (otro idioma)")).toBeVisible();
});

for (const status of ["transcribing_queued", "transcribing"]) {
  test(`reopening a song in ${status} resumes the progress`, async ({ page }) => {
    const jobId = `retranscribe-${status}`.slice(0, 24);
    await installEditorHarness(page, { jobId, role: "admin", editorV2: true, initialRevision: 3, segments: ENGLISH });
    const api = await installRetranscribeApi(page, {
      jobId, initialStatus: status, pollsUntilDone: 1, hold: true,
      attempt: { status: status === "transcribing" ? "running" : "queued", language: "es", polls: 0 },
    });
    await openReview(page, jobId);
    const dialog = page.getByTestId("retranscribe-dialog");
    await expect(dialog).toContainText("Genly está escuchando la canción en Español", LOAD);
    // Polling continues while it runs, and the editor cannot be used meanwhile.
    const pollsBefore = api.attempt.polls;
    await expect.poll(() => api.attempt.polls, LOAD).toBeGreaterThan(pollsBefore);
    await expect(dialog.getByTestId("retranscribe-confirm")).toHaveCount(0);
    api.hold = false; // the worker finishes
    await expect(page.getByText("Gran ídolo de Asia").first()).toBeVisible(LOAD);
    await expect(page.getByTestId("retranscribe-dialog")).toHaveCount(0);
  });
}

test("a failed re-transcription shows the error and keeps the previous text", async ({ page }) => {
  const jobId = "retranscribefail";
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true, initialRevision: 3, segments: ENGLISH });
  await installRetranscribeApi(page, { jobId, outcome: "failed", pollsUntilDone: 1 });
  await openReview(page, jobId);
  await page.getByTestId("retranscribe-open").click();
  const dialog = page.getByTestId("retranscribe-dialog");
  await dialog.getByTestId("retranscribe-confirm").click();
  await expect(dialog.getByRole("alert")).toContainText("La versión anterior sigue en el editor y en el historial", { timeout: 15_000 });
  await expect(dialog.getByRole("alert")).toContainText("No pudimos completar la transcripción.");
  await dialog.getByRole("button", { name: "Cerrar" }).click();
  await expect(page.getByTestId("retranscribe-dialog")).toHaveCount(0);
  await expect(page.getByText("Great idol of Asia").first()).toBeVisible();
});
