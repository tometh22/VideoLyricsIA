import { expect, test } from "@playwright/test";
import { createSyntheticWav } from "./editor-harness.js";

const ENABLED = process.env.REAL_EDITOR_E2E === "1";
const API = process.env.REAL_EDITOR_API || "http://127.0.0.1:8000";
const JOB_ID = "e2ecollab001";
const PASSWORD = "EditorE2E-test-123";

async function login(request, username) {
  const response = await request.post(`${API}/auth/login`, { data: { username, password: PASSWORD } });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).token;
}

async function openEditor(browser, token) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(({ authToken }) => {
    // Initialize a fresh context once. Running clear() on every navigation
    // makes reload unlike a real browser and destroys drafts/one-time receipts.
    if (localStorage.getItem("genly_token") !== authToken) {
      localStorage.clear();
      sessionStorage.clear();
      localStorage.setItem("genly_token", authToken);
      localStorage.setItem("genly_lang", "es");
      localStorage.setItem("genly_tour_editor_timing_v2_done", "1");
    }
  }, { authToken: token });
  const page = await context.newPage();
  const wav = createSyntheticWav({ durationSeconds: 6 });
  await page.route(`**/jobs/${JOB_ID}/source-audio-url`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ url: "/real-e2e/audio.wav" }) }));
  await page.route("**/real-e2e/audio.wav", (route) => route.fulfill({ status: 200, contentType: "audio/wav", body: wav }));
  await page.route(`**/jobs/${JOB_ID}/waveform`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ peaks: [0.2, 0.5, 0.8, 0.4] }) }));
  await page.route(`**/jobs/${JOB_ID}/background-url`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ url: null }) }));
  await page.goto(`/videos/${JOB_ID}/edit-lyrics`);
  await expect(page.getByRole("button", { name: /4 Lyrics/ })).toBeVisible();
  // A fresh browser profile can legitimately receive the one-time product
  // announcement before the editor. Dismiss it through the same accessible
  // control a real user sees so it cannot intercept the workflow below.
  const announcement = page.getByRole("dialog");
  if (await announcement.isVisible()) {
    await announcement.getByRole("button", { name: "Cancelar", exact: true }).click();
  }
  await page.getByRole("button", { name: /4 Lyrics/ }).click();
  await expect(page.getByTestId("editor-mode-explainer")).toBeVisible();
  return { context, page };
}

async function revision(request, token) {
  const response = await request.get(`${API}/editor/${JOB_ID}`, { headers: { Authorization: `Bearer ${token}` } });
  expect(response.ok()).toBeTruthy();
  return await response.json();
}

const firstLyricsInput = (page) => page.getByRole("textbox", { name: "Letra de la línea 1" });
const secondLyricsInput = (page) => page.getByRole("textbox", { name: "Letra de la línea 2" });

test.describe("Editor 2.0 real collaboration", () => {
  // This scenario intentionally mutates one seeded document. Retrying against
  // the same database would start from a later revision and produce false data.
  test.describe.configure({ retries: 0 });
  test.skip(!ENABLED, "requires the real API/PostgreSQL CI job");

  test("merges both users silently, reloads and preserves history", async ({ browser, request }) => {
    const tokenA = await login(request, "editor_e2e_a");
    const tokenB = await login(request, "editor_e2e_b");
    const a = await openEditor(browser, tokenA);

    await firstLyricsInput(a.page).fill("Edición de A");
    await expect.poll(async () => (await revision(request, tokenA)).revision).toBeGreaterThan(0);
    let latestRevision = (await revision(request, tokenA)).revision;

    const b = await openEditor(browser, tokenB);
    await expect(firstLyricsInput(b.page)).toHaveValue("Edición de A");
    await secondLyricsInput(b.page).fill("Edición de B");
    await expect.poll(async () => (await revision(request, tokenB)).revision).toBeGreaterThan(latestRevision);
    latestRevision = (await revision(request, tokenB)).revision;

    await firstLyricsInput(a.page).fill("A queda local");
    await expect.poll(async () => (await revision(request, tokenA)).revision).toBeGreaterThan(latestRevision);
    latestRevision = (await revision(request, tokenA)).revision;
    await expect(firstLyricsInput(a.page)).toHaveValue("A queda local");
    await expect(secondLyricsInput(a.page)).toHaveValue("Edición de B");
    await expect(a.page.getByRole("dialog", { name: /Hay una versión más nueva/ })).toHaveCount(0);

    await a.context.setOffline(true);
    await firstLyricsInput(a.page).fill("A segunda versión local");
    await secondLyricsInput(b.page).fill("B vuelve a guardar");
    await expect.poll(async () => (await revision(request, tokenB)).revision).toBeGreaterThan(latestRevision);
    latestRevision = (await revision(request, tokenB)).revision;
    await a.context.setOffline(false);
    await expect.poll(async () => (await revision(request, tokenA)).revision).toBeGreaterThan(latestRevision);
    latestRevision = (await revision(request, tokenA)).revision;
    await expect(firstLyricsInput(a.page)).toHaveValue("A segunda versión local");
    await expect(secondLyricsInput(a.page)).toHaveValue("B vuelve a guardar");
    await expect(a.page.getByRole("dialog", { name: /Hay una versión más nueva/ })).toHaveCount(0);

    await a.page.reload();
    await expect(a.page.getByRole("button", { name: /4 Lyrics/ })).toBeVisible();
    await a.page.getByRole("button", { name: /4 Lyrics/ }).click();
    await expect(firstLyricsInput(a.page)).toHaveValue("A segunda versión local");
    await expect(secondLyricsInput(a.page)).toHaveValue("B vuelve a guardar");
    await a.page.getByRole("tab", { name: "Ajustar tiempos" }).click();
    await a.page.getByTestId("editor-overflow-btn").click();
    await a.page.getByRole("menuitem", { name: /Historial de versiones/ }).click();
    await expect(a.page.getByRole("dialog", { name: "Historial de versiones" })).toBeVisible();
    // Draft autosaves advance the durable CAS revision but intentionally do
    // not create a visible history checkpoint. The immutable migration
    // checkpoint must remain available after the silent merge flow.
    await expect(a.page.getByText("Revisión 0")).toBeVisible();

    await a.context.close();
    await b.context.close();
  });
});
