export function wavBuffer(durationSeconds = 8) {
  const sampleRate = 8000;
  const samples = Math.floor(sampleRate * durationSeconds);
  const bytes = new ArrayBuffer(44 + samples);
  const view = new DataView(bytes);
  const write = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  write(0, "RIFF");
  view.setUint32(4, 36 + samples, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate, true);
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  write(36, "data");
  view.setUint32(40, samples, true);
  for (let index = 44; index < bytes.byteLength; index += 1) view.setUint8(index, 128);
  return Buffer.from(bytes);
}

export async function installEditorHarness(page, { conflictOnce = false } = {}) {
  const segments = [
    { start: 0.5, end: 1.6, text: "Primera línea" },
    { start: 2.2, end: 3.4, text: "Segunda línea" },
    { start: 4.0, end: 5.2, text: "Tercera línea" },
    { start: 5.8, end: 7.1, text: "Cuarta línea" },
  ];
  let revision = 0;
  let serverSegments = segments;
  let conflictSent = false;
  const versions = [{ id: "seed", revision: 0, reason: "autosave", segments, created_by: { id: 1, username: "tester" }, created_at: new Date().toISOString() }];
  const saves = [];

  await page.addInitScript(() => {
    localStorage.setItem("genly_token", "e2e-token");
    localStorage.setItem("genly_user", JSON.stringify({ id: 1, username: "tester", role: "user", tenant_id: "e2e" }));
    localStorage.setItem("genly_tour_editor_done", "1");
  });

  await page.route("**/jobs", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/backgrounds", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/upload-url", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "e2e-job", key: "e2e/song.wav", upload_url: "http://127.0.0.1:4173/e2e-upload", use_multipart: false }) }));
  await page.route("http://127.0.0.1:4173/e2e-upload", (route) => route.fulfill({ status: 200, headers: { ETag: "e2e" }, body: "" }));
  await page.route("**/transcribe-uploaded", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "e2e-job", segments, reference_lyrics: "Primera línea\nSegunda línea\nTercera línea\nCuarta línea" }) }));
  await page.route("**/editor/e2e-job/lock**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ acquired: true, user: { id: 1, username: "tester" }, expires_at: new Date(Date.now() + 60000).toISOString() }) }));
  await page.route("**/editor/e2e-job/versions", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ versions }) }));
  await page.route("**/editor/e2e-job/restore", async (route) => {
    const payload = route.request().postDataJSON();
    const version = versions.find((item) => item.id === payload.version_id) || versions[0];
    revision += 1;
    versions.unshift({ ...version, id: `version-${revision}`, revision });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "e2e-job", revision, version_id: `version-${revision}`, segments: version.segments }) });
  });
  await page.route("**/editor/e2e-job", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "e2e-job", revision, segments: serverSegments, original_segments: segments, updated_by: { id: 1, username: "tester" }, lock: { active: false }, updated_at: new Date().toISOString() }) });
    }
    const payload = route.request().postDataJSON();
    if (conflictOnce && !conflictSent) {
      conflictSent = true;
      return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { detail: "editor_revision_conflict", server_revision: revision + 1, server_segments: segments.map((item, index) => index === 0 ? { ...item, text: "Versión del equipo" } : item), updated_by: { id: 2, username: "equipo" }, updated_at: new Date().toISOString() } }) });
    }
    saves.push(payload);
    revision += 1;
    serverSegments = payload.segments;
    versions.unshift({ id: `version-${revision}`, revision, reason: payload.checkpoint || "autosave", segments: payload.segments, created_by: { id: 1, username: "tester" }, created_at: new Date().toISOString() });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "e2e-job", revision, version_id: `version-${revision}`, saved_at: new Date().toISOString() }) });
  });
  await page.route("**/analytics/events", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ accepted: 1 }) }));

  return { segments, saves };
}
