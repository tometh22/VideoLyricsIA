import { expect, test } from "@playwright/test";
import { createSyntheticWav } from "./editor-harness.js";

const API = process.env.REAL_EDITOR_API || "http://127.0.0.1:8000";
const JOB = "e2ereview001";
const CAMPAIGN = "e2erevcamp01";

test.describe("Complete reviewer candidate with real PostgreSQL persistence", () => {
  test.describe.configure({ retries: 0 });
  test.skip(process.env.REAL_REVIEWER_E2E !== "1", "requires disposable seeded real API");

  test("listens, compares, adopts, then approves once without generating media", async ({ browser, request }) => {
    const login = await request.post(`${API}/auth/login`, {
      data: { username: "reviewer_e2e_admin", password: "EditorE2E-test-123" },
    });
    expect(login.ok()).toBeTruthy();
    const token = (await login.json()).token;
    const headers = { Authorization: `Bearer ${token}` };
    const document = async () => {
      const response = await request.get(`${API}/editor/${JOB}`, { headers });
      expect(response.ok()).toBeTruthy();
      return response.json();
    };
    const before = await document();
    expect(before.reviewer_candidate.adoption_status).toBe("matching_existing_proposal");
    expect(before.segments[0].text).toBe("Canto así");
    const protectedLine = before.segments[1];
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addInitScript((authToken) => {
      localStorage.setItem("genly_token", authToken);
      localStorage.setItem("genly_lang", "es");
      localStorage.setItem("genly_tour_editor_timing_v2_done", "1");
    }, token);
    const page = await context.newPage();
    const forbidden = [];
    page.on("request", (req) => {
      if (req.method() === "POST" && /\/(generate|generate-preview|render)(?:\?|$|\/)/.test(new URL(req.url()).pathname)) forbidden.push(req.url());
    });
    // Only media transport is synthetic. Editor, proposal, approval and queue
    // requests all reach the real authenticated API and disposable PostgreSQL.
    await page.route(`**/jobs/${JOB}/source-audio-url`, route => route.fulfill({
      json: { url: "/reviewer-e2e/audio.wav" },
    }));
    await page.route("**/reviewer-e2e/audio.wav", route => route.fulfill({
      contentType: "audio/wav", body: createSyntheticWav({ durationSeconds: 10 }),
    }));
    await page.route(`**/jobs/${JOB}/waveform`, route => route.fulfill({ json: { peaks: [0.2, 0.6, 0.9, 0.3] } }));
    await page.route(`**/jobs/${JOB}/background-url`, route => route.fulfill({ json: { url: null } }));
    try {
      await page.goto(`/review/${JOB}`);
      // A campaign opens directly at lyrics (six-step wizard), unlike the
      // four-step post-render editor. Wait for the actual editor, not a tab's
      // incidental step number; keep every candidate/adoption assertion below.
      await expect(page.getByTestId("lyrics-editor")).toBeVisible();
      const announcement = page.getByRole("dialog");
      if (await announcement.isVisible()) await announcement.getByRole("button", { name: "Cancelar", exact: true }).click();
      await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
      const companion = page.getByRole("region", { name: "Revisión acústica completa" });
      await expect(companion).toBeVisible();
      await companion.getByText(/Ver letra y timing de toda la candidata/).click();
      await expect(companion.getByText("Canto aquí", { exact: true })).toBeVisible();
      await expect(companion.getByText(/Actual:.*Canto así/)).toBeVisible();
      await companion.getByRole("button", { name: "Escuchar línea 1" }).click();
      await expect.poll(() => page.locator("audio").evaluateAll(nodes => nodes.some(audio => !audio.paused && audio.currentTime >= 1))).toBe(true);
      await companion.getByText(/Dudas y límites/).click();
      await expect(companion.getByText("Se conserva tu edición humana")).toBeVisible();

      const applyResponse = page.waitForResponse(response => response.url().includes(`/editor/${JOB}/quality-proposals/`)
        && response.url().endsWith("/apply") && response.request().method() === "POST");
      await page.getByRole("button", { name: "Usar candidata completa para revisar", exact: true }).click();
      expect((await applyResponse).ok()).toBeTruthy();
      await expect(page.getByRole("textbox", { name: "Letra de la línea 1" })).toHaveValue("Canto aquí");
      const adopted = await document();
      expect(adopted.segments[0].text).toBe("Canto aquí");
      expect(adopted.segments[1]).toEqual(protectedLine);
      expect(adopted.revision).toBeGreaterThan(before.revision);
      const itemsBefore = await request.get(`${API}/batch/campaigns/${CAMPAIGN}/items`, { headers });
      expect(itemsBefore.ok()).toBeTruthy();
      expect((await itemsBefore.json()).items.find(item => item.job_id === JOB).phase).toBe("lyrics_ready");

      const approveResponse = page.waitForResponse(response => response.url().endsWith(`/batch/campaigns/${CAMPAIGN}/jobs/${JOB}/approve-lyrics`)
        && response.request().method() === "POST");
      await page.getByRole("button", { name: "Aprobar letra y timing", exact: true }).click();
      const approvedResponse = await approveResponse;
      expect(approvedResponse.ok()).toBeTruthy();
      expect((await approvedResponse.json()).status).toBe("lyrics_approved");
      await expect(page).toHaveURL(new RegExp(`/admin/cola\\?approved=${JOB}`));
      const after = await document();
      expect(after.segments[0].text).toBe("Canto aquí");
      expect(after.segments[1]).toEqual(protectedLine);
      const itemsAfter = await request.get(`${API}/batch/campaigns/${CAMPAIGN}/items`, { headers });
      expect(itemsAfter.ok()).toBeTruthy();
      expect((await itemsAfter.json()).items.find(item => item.job_id === JOB).phase).toBe("lyrics_approved");
      expect(forbidden).toEqual([]);
    } finally {
      await context.close();
    }
  });
});
