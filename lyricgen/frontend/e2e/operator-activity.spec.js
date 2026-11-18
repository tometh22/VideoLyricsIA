import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import path from "node:path";
import { mkdir } from "node:fs/promises";

test("personal production is visible from campaigns and home, with yesterday and separate edits", async ({ page }) => {
  await mkdir(path.resolve("../../.context"), { recursive: true });
  await installEditorHarness(page, { role: "admin" });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.route("**/service-status/summary", (route) => route.fulfill({ json: { status: "operational", incidents: [] } }));
  const campaigns = [
    { id: "rock", name: "UMG Septiembre — Cancionero Rock", count: 18 },
    { id: "religiosas", name: "UMG Octubre — Canciones Religiosas", count: 15 },
    { id: "chile", name: "UMG Chile — Noviembre", count: 3 },
  ];
  const songs = campaigns.flatMap((campaign) => Array.from({ length: campaign.count }, (_, i) => ({ job_id: `${campaign.id}-${i}`, campaign_id: campaign.id, artist: "Artista de prueba", title: `Canción ${i + 1}` })));
  const dates = [];
  await page.route("**/batch/activity/me?*", async (route) => {
    dates.push(new URL(route.request().url()).searchParams.get("day"));
    await route.fulfill({ json: { generated: 36, requested: 36, edited_songs: 11, edit_requests: 12, approved_now: 27, pending_review_now: 9, campaigns, songs } });
  });
  await page.route("**/batch/campaigns", (route) => route.fulfill({ json: { items: campaigns.map((c) => ({ ...c, kind: "lyric_video", status: "active", pipeline: { counts: { qc: c.count } } })) } }));
  await page.goto("/campaigns");
  const card = page.getByRole("region", { name: "Mi producción en campañas" });
  await expect(card).toBeHidden();
  await expect(page.getByRole("region", { name: "Lista de campañas" })).toBeVisible();
  await page.screenshot({ path: "test-results/campaign-home-overview.png", fullPage: true });
  await page.getByText("Mi actividad de producción", { exact: true }).click();
  await expect(card.getByText("Canciones convertidas a video", { exact: true })).toBeVisible();
  await card.getByRole("button", { name: "Ayer", exact: true }).click();
  await expect(card.getByRole("button", { name: "Ayer", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(card.getByText(/12 solicitudes/)).toBeVisible();
  await card.getByText("Ver las 36 canciones").click();
  await expect(card.getByRole("link", { name: "Artista de prueba · Canción 1", exact: true }).first()).toBeVisible();
  expect(new Set(dates).size).toBe(2);
  await card.screenshot({ path: path.resolve("../../.context/operator-activity-desktop.png"), animations: "disabled" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => (await card.boundingBox()).width).toBeGreaterThan(300);
  await expect(card.getByRole("button", { name: "Ayer", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await card.screenshot({ path: path.resolve("../../.context/operator-activity-mobile.png"), animations: "disabled" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Mi producción en campañas" })).toBeVisible();
});
