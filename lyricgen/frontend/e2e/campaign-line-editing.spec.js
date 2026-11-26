import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { installEditorHarness } from "./editor-harness";

const segments = [
  { segment_id: "first", start: 0, end: 1.2, text: "Primera frase" },
  { segment_id: "second", start: 1.2, end: 2.4, text: "Segunda frase" },
  { segment_id: "third", start: 2.4, end: 3.8, text: "Tercera frase" },
];
async function open(page, jobId) {
  const harness = await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "line-edits", campaign_item_id: "one",
    filename: "Prueba.wav", segments_revision: 0, segments_json: segments,
  } }));
  await page.route(`**/editor/${jobId}/text-preview`, route => route.fulfill({ status: 503, json: { detail: "Preview temporarily unavailable" } }));
  await page.goto(`/review/${jobId}`);
  await expect(page.getByTestId("campaign-line-workspace")).toBeVisible();
  return harness;
}

test("adds below the selection, drags, saves, deletes, undoes and reloads", async ({ page }) => {
  const api = await open(page, "line-editing-browser");
  await page.getByTestId("review-line-2").click();
  await page.locator("audio").evaluate(audio => audio.pause());
  await page.getByRole("button", { name: "Agregar línea", exact: true }).click();
  const input = page.getByLabel("Letra de esta línea", { exact: true });
  await expect(input).toBeFocused();
  await input.fill("Nueva frase");
  await expect(page.getByTestId("review-line-3")).toContainText("Nueva frase");
  await expect.poll(() => api.saves.at(-1)?.segments.map(row => row.text)).toEqual(["Primera frase", "Segunda frase", "Nueva frase", "Tercera frase"]);
  const target = page.getByTestId("review-line-1").locator("..");
  const box = await target.boundingBox();
  await page.getByRole("button", { name: "Mover línea 3", exact: true }).dragTo(target, { targetPosition: { x: 80, y: box.height - 4 } });
  await expect.poll(() => api.saves.at(-1)?.segments.map(row => row.text)).toEqual(["Primera frase", "Nueva frase", "Segunda frase", "Tercera frase"]);
  await page.getByRole("button", { name: "Eliminar línea 2", exact: true }).click();
  await expect.poll(() => api.saves.at(-1)?.segments.map(row => row.text)).toEqual(["Primera frase", "Segunda frase", "Tercera frase"]);
  await page.getByRole("button", { name: "Deshacer", exact: true }).click();
  await expect.poll(() => api.saves.at(-1)?.segments.map(row => row.text)).toEqual(["Primera frase", "Nueva frase", "Segunda frase", "Tercera frase"]);
  await page.reload();
  await expect(page.getByTestId("review-line-2")).toContainText("Nueva frase");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByTestId("campaign-line-workspace").screenshot({ path: path.resolve("../../.context/editor-line-improvements.png") });
});

test("native preview retains video line breaks at different panel sizes", async ({ page }) => {
  const track = JSON.parse(await readFile(new URL("./fixtures/native-preview.json", import.meta.url), "utf8"));
  await page.route("**/editor/proof/text-preview", route => route.fulfill({ json: track }));
  await page.route("**/editor/preview/fonts/*", async route => route.fulfill({ contentType: "font/ttf",
    body: await readFile(path.resolve("../backend/fonts", new URL(route.request().url()).pathname.split("/").at(-1))) }));
  // Exercise the actual WASM worker in isolation from application auth.
  await page.goto("/");
  await page.evaluate(async () => {
    const { default: React } = await import("/node_modules/.vite/deps/react.js");
    const { default: Client } = await import("/node_modules/.vite/deps/react-dom_client.js");
    const { I18nProvider } = await import("/src/i18n.jsx");
    const { default: Preview } = await import("/src/components/NativeLyricPreview.jsx");
    const root = document.createElement("div"); root.id = "preview-proof";
    root.style.cssText = "position:fixed;inset:10px auto auto 10px;width:768px;z-index:99999;background:black";
    document.body.append(root);
    Client.createRoot(root).render(React.createElement(I18nProvider, null, React.createElement(Preview, {
      segments: [{ start: 0, end: 3, text: "¡Si es bueno para mí, es malo para vos!" }],
      settings: { font: "montserrat-bold", font_scale: 1.5 }, jobId: "proof",
      request: (url, options) => fetch(url, options), currentTime: 1,
    })));
  });
  const canvas = page.locator("#preview-proof canvas");
  await expect(canvas).toBeVisible({ timeout: 20000 });
  const size = await canvas.evaluate(node => ({ width: node.width, height: node.height }));
  expect(size).toEqual({ width: 1920, height: 1080 });
  const whiteRows = async () => {
    const png = await page.getByTestId("native-lyric-preview").screenshot();
    return page.evaluate(async data => {
      const image = new Image(); image.src = data; await image.decode();
      const canvas = document.createElement("canvas"); canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext("2d"); context.drawImage(image, 0, 0);
      const { data: pixels } = context.getImageData(0, 0, image.width, image.height);
      const groups = [];
      for (let y = 0; y < image.height; y++) {
        let count = 0;
        for (let x = 0; x < image.width; x++) {
          const at = (y * image.width + x) * 4;
          if (Math.min(pixels[at], pixels[at + 1], pixels[at + 2]) > 200) count++;
        }
        if (count <= 1) continue;
        if (!groups.length || y - groups.at(-1).end > Math.max(2, image.height * .015)) groups.push({ start: y, end: y });
        else groups.at(-1).end = y;
      }
      return groups.map(group => ({ start: group.start / image.height, end: group.end / image.height }));
    }, `data:image/png;base64,${png.toString("base64")}`);
  };
  await expect.poll(async () => (await whiteRows()).length).toBe(2);
  const largeRows = await whiteRows();
  // Native ffmpeg/libass reference for the same ASS and fonts.
  expect(largeRows[0].start).toBeCloseTo(429 / 1080, 2);
  expect(largeRows[1].start).toBeCloseTo(573 / 1080, 2);
  await page.locator("#preview-proof").evaluate(node => { node.style.width = "384px"; });
  await expect(canvas).toHaveCSS("width", "1920px");
  expect(await canvas.evaluate(node => [node.width, node.height])).toEqual([1920, 1080]);
  await expect.poll(async () => (await whiteRows()).length).toBe(2);
  const smallRows = await whiteRows();
  expect(smallRows[0].start).toBeCloseTo(largeRows[0].start, 2);
  expect(smallRows[1].start).toBeCloseTo(largeRows[1].start, 2);
  await page.getByTestId("native-lyric-preview").screenshot({ path: path.resolve("../../.context/native-text-preview.png") });
});

test("moves and saves a line with touch without shifting its neighbours", async ({ page, context }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = await open(page, "touch-line-editing");
  const list = page.getByTestId("review-line-list");
  await list.scrollIntoViewIfNeeded();
  const from = await page.getByRole("button", { name: "Mover línea 1", exact: true }).boundingBox();
  const to = await page.getByTestId("review-line-3").boundingBox();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from.x + from.width / 2, y: from.y + from.height / 2 }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: to.x + 30, y: to.y + to.height - 5 }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await expect.poll(() => api.saves.at(-1)?.segments.map(row => row.text)).toEqual(["Segunda frase", "Tercera frase", "Primera frase"]);
  const saved = api.saves.at(-1).segments;
  for (const original of segments.slice(1)) expect(saved.find(row => row.segment_id === original.segment_id)).toMatchObject(original);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
