import { expect } from "@playwright/test";

export const EDITOR_JOB_ID = "e2e-lyrics-editor";

export const DEFAULT_SEGMENTS = [
  { _id: "line-a", start: "0.40", end: "1.00", text: "Primera línea" },
  { _id: "line-b", start: "1.20", end: "1.80", text: "Segunda línea" },
  { _id: "line-c", start: "2.00", end: "2.60", text: "Tercera línea" },
  { _id: "line-d", start: "2.80", end: "3.40", text: "Cuarta línea" },
  { _id: "line-e", start: "3.55", end: "3.90", text: "Quinta línea" },
];

export function createSyntheticWav({ durationSeconds = 4, sampleRate = 8_000 } = {}) {
  const sampleCount = Math.floor(durationSeconds * sampleRate);
  const dataSize = sampleCount * 2;
  const wav = Buffer.alloc(44 + dataSize);

  wav.write("RIFF", 0);
  wav.writeUInt32LE(36 + dataSize, 4);
  wav.write("WAVE", 8);
  wav.write("fmt ", 12);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20); // PCM
  wav.writeUInt16LE(1, 22); // mono
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(dataSize, 40);

  for (let i = 0; i < sampleCount; i += 1) {
    // A quiet deterministic tone makes Chromium decode the fixture as real
    // audio without relying on a checked-in binary asset.
    const sample = Math.round(Math.sin((2 * Math.PI * 220 * i) / sampleRate) * 2_000);
    wav.writeInt16LE(sample, 44 + i * 2);
  }
  return wav;
}

function authToken() {
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 86_400 })).toString("base64url");
  return `e2e.${payload}.token`;
}

function jsonResponse(body, status = 200) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}

/**
 * Install a self-contained browser harness for the real Vite application.
 *
 * `audio: "unavailable"` exercises a definitive missing-audio state; `temporary`
 * simulates DB backpressure (503 + Retry-After) before the signed URL recovers.
 * `empty: true` exercises the no-lyrics bootstrap state.
 * Every save request is recorded and returned through `harness.saves` so
 * tests can assert the actual wire payload, including finite timings.
 */
export async function installEditorHarness(page, options = {}) {
  const jobId = options.jobId || EDITOR_JOB_ID;
  const segments = options.segments === undefined ? DEFAULT_SEGMENTS : options.segments;
  const empty = options.empty === true;
  const audio = ["unavailable", "temporary"].includes(options.audio) ? options.audio : "available";
  const editorV2 = options.editorV2 === true;
  const transcriptionQuality = options.transcriptionQuality || null;
  const saves = [];
  const approvals = [];
  let durableRevision = 0;
  let durableSegments = JSON.parse(JSON.stringify(empty ? [] : segments));
  const durableOriginal = JSON.parse(JSON.stringify(durableSegments));
  const versions = [];
  const heartbeats = [];
  let sourceAudioRequests = 0;
  const audioBytes = createSyntheticWav();

  await page.addInitScript(({ token }) => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem("genly_token", token);
    localStorage.setItem("genly_lang", "es");
    localStorage.setItem("genly_user", JSON.stringify({
      id: "e2e-user",
      email: "e2e@example.test",
      name: "E2E Operator",
      role: "user",
    }));
  }, { token: authToken() });

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/e2e/audio.wav") {
      await route.fulfill({
        status: 200,
        contentType: "audio/wav",
        headers: {
          "Accept-Ranges": "bytes",
          "Content-Length": String(audioBytes.length),
        },
        body: audioBytes,
      });
      return;
    }

    if (request.method() === "GET" && path === "/jobs") {
      await route.fulfill(jsonResponse([]));
      return;
    }

    if (request.method() === "GET" && path === "/usage") {
      await route.fulfill(jsonResponse({ used: 0, limit: 100, percent: 0, plan: "free" }));
      return;
    }

    if (request.method() === "GET" && path === "/auth/me") {
      await route.fulfill(jsonResponse({ id: "e2e-user", email: "e2e@example.test", role: "user", tenant_id: "e2e-team", features: { editor_v2: editorV2 } }));
      return;
    }

    if (editorV2 && request.method() === "GET" && path === `/editor/${jobId}`) {
      await route.fulfill(jsonResponse({
        job_id: jobId,
        revision: durableRevision,
        segments: durableSegments,
        original_segments: durableOriginal,
        updated_by: null,
        updated_at: new Date().toISOString(),
        lock: { active: false, user: null, expires_at: null },
      }));
      return;
    }

    if (editorV2 && request.method() === "PATCH" && path === `/editor/${jobId}`) {
      const body = request.postDataJSON();
      if (body.base_revision !== durableRevision) {
        await route.fulfill(jsonResponse({ detail: {
          detail: "editor_revision_conflict",
          server_revision: durableRevision,
          server_segments: durableSegments,
          updated_by: { id: "teammate", username: "Teammate" },
          updated_at: new Date().toISOString(),
        } }, 409));
        return;
      }
      const changed = JSON.stringify(body.segments) !== JSON.stringify(durableSegments);
      if (changed) durableRevision += 1;
      durableSegments = body.segments;
      const versionId = body.checkpoint === "draft" ? null : `version-${durableRevision}`;
      if (versionId && !versions.some((version) => version.id === versionId)) {
        versions.unshift({ id: versionId, revision: durableRevision, reason: body.checkpoint, is_approved: false, created_at: new Date().toISOString() });
      }
      saves.push(body);
      await route.fulfill(jsonResponse({ applied: changed, revision: durableRevision, version_id: versionId, saved_at: new Date().toISOString() }));
      return;
    }

    if (editorV2 && request.method() === "POST" && path.endsWith("/lock/heartbeat")) {
      heartbeats.push({ at: Date.now() });
      await route.fulfill(jsonResponse({ acquired: true, user: { id: "e2e-user", username: "E2E Operator" }, expires_at: new Date(Date.now() + 60_000).toISOString() }));
      return;
    }
    if (editorV2 && request.method() === "DELETE" && path.endsWith("/lock")) {
      await route.fulfill(jsonResponse({ released: true }));
      return;
    }
    if (editorV2 && request.method() === "GET" && path === `/editor/${jobId}/versions`) {
      await route.fulfill(jsonResponse({ versions }));
      return;
    }
    if (request.method() === "POST" && path === "/analytics/events") {
      await route.fulfill(jsonResponse({ accepted: 1, rejected: 0 }));
      return;
    }

    if (request.method() === "GET" && path.startsWith("/media-token/")) {
      await route.fulfill(jsonResponse({ token: "e2e-media-token" }));
      return;
    }

    if (request.method() === "GET" && path === `/status/${jobId}`) {
      await route.fulfill(jsonResponse({
        job_id: jobId,
        status: "pending_review",
        filename: "e2e-song.mp3",
        artist: "E2E Artist",
        song_title: "E2E Song",
        segments_json: empty ? [] : segments,
        segments_revision: 0,
        transcription_quality: transcriptionQuality,
        render_params: {},
      }));
      return;
    }

    if (request.method() === "GET" && path === `/jobs/${jobId}/source-audio-url`) {
      sourceAudioRequests += 1;
      if (audio === "unavailable") {
        await route.fulfill(jsonResponse({ detail: "synthetic audio unavailable" }, 404));
      } else if (audio === "temporary" && sourceAudioRequests <= 2) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          headers: { "Retry-After": "1" },
          body: JSON.stringify({ detail: "temporary DB pressure" }),
        });
      } else {
        await route.fulfill(jsonResponse({ url: "/e2e/audio.wav" }));
      }
      return;
    }

    if (request.method() === "GET" && path === `/jobs/${jobId}/waveform`) {
      await route.fulfill(jsonResponse({ peaks: Array.from({ length: 64 }, (_, i) => 0.2 + ((i * 17) % 50) / 100) }));
      return;
    }

    if (request.method() === "GET" && path === `/jobs/${jobId}/background-url`) {
      await route.fulfill(jsonResponse({ url: null }));
      return;
    }

    if (request.method() === "POST" && path === `/jobs/${jobId}/save-segments`) {
      const body = request.postDataJSON();
      saves.push(body);
      await route.fulfill(jsonResponse({ applied: true, revision: saves.length }));
      return;
    }

    if (request.method() === "POST" && path === `/edit/${jobId}`) {
      approvals.push(request.postDataJSON());
      await route.fulfill(jsonResponse({
        job_id: jobId,
        approved_editor_version_id: approvals.at(-1)?.editor_version_id || null,
      }));
      return;
    }

    // Keep the auth refresh path deterministic if the app decides to refresh
    // the synthetic token in a future change.
    if (request.method() === "POST" && path === "/auth/refresh") {
      await route.fulfill(jsonResponse({ token: authToken() }));
      return;
    }

    await route.continue();
  });

  return {
    jobId,
    saves,
    approvals,
    heartbeats,
    get sourceAudioRequests() { return sourceAudioRequests; },
    async open() {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.goto(`/videos/${jobId}/edit-lyrics`);
      if (empty) {
        await expect(page.getByText("Este video no tiene letras guardadas")).toBeVisible();
        return;
      }
      // Fresh test storage can show the product's first-visit announcement.
      // It is unrelated to editor behavior, so dismiss it as a user would.
      const welcomeDialog = page.getByRole("dialog");
      if (await welcomeDialog.isVisible().catch(() => false)) {
        await welcomeDialog.getByRole("button", { name: "Cancelar" }).click();
      }
      // EditLyricsRoute boots the real wizard, whose stepper starts on the
      // first setup step. Move to the Lyrics step before asserting the editor
      // contract; this mirrors the operator's explicit navigation.
      await expect(page.getByRole("button", { name: /4 Lyrics/ })).toBeVisible();
      await page.getByRole("button", { name: /4 Lyrics/ }).click();
      await expect(page.getByTestId("editor-mode-explainer")).toBeVisible();
      if (audio !== "unavailable") {
        await page.waitForFunction(() => {
          const element = document.querySelector("audio");
          return Boolean(element && element.readyState >= 1 && element.duration > 0);
        });
      }
    },
  };
}

export async function openAdvanced(page, { expectTimeline = true } = {}) {
  await page.getByRole("tab", { name: "Ajustar tiempos" }).click();
  await expect(page.getByRole("tab", { name: "Ajustar tiempos" })).toHaveAttribute("aria-selected", "true");
  if (expectTimeline) {
    await page.getByRole("tab", { name: "Timeline avanzada" }).click();
    await expect(page.getByRole("tab", { name: "Timeline avanzada" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("timeline-lane")).toBeVisible();
  }
}

export async function selectionCount(page, count) {
  await expect.poll(async () => Number(await page.getByTestId("timeline-primary-actions").getAttribute("data-selected-count"))).toBe(count);
}

export async function selectionAtLeast(page, count) {
  await expect.poll(async () => Number(await page.getByTestId("timeline-primary-actions").getAttribute("data-selected-count"))).toBeGreaterThanOrEqual(count);
}

export async function drag(page, from, to, steps = 8) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps });
  await page.mouse.up();
}

export function modifierForCurrentPlatform() {
  return process.platform === "darwin" ? "Meta" : "Control";
}
