import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import path from "node:path";

test("account and campaign delivery plan: preview, assign, then generate only ready songs", async ({ page }) => {
  await installEditorHarness(page, { jobId: "planjob1", role: "admin" });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.setViewportSize({ width: 1440, height: 1050 });
  const zero = { veo: 0, photo_effect: 0, unknown: 0 };
  let assigned = false, generated = false, currentPortal = "argentina";
  const campaign = { id: "plan1", name: "UMG Argentina · Septiembre", kind: "lyric_video", status: "active", destination_portal: "argentina", registered_count: 4 };
  const rows = () => ["El día es claro", "Canción del sur", "Vuelvo a casa", "Luz de octubre"].map((title, i) => ({
    item_id: `item${i}`, campaign_id: "plan1", campaign_name: campaign.name, title, artist: "Artista de prueba", job_id: `planjob${i + 1}`,
    status: i === 0 ? "lyrics_approved" : "transcribed_pending", type: assigned ? "photo_effect" : "unknown", effect: assigned ? "bokeh" : "",
    stage: "pending", ready: assigned && i === 0 && !generated, editable: !generated, pinned: false, revision: assigned ? 1 : 0,
    settings: assigned ? { movement_style: "foto-parallax", effect: "bokeh" } : {}, assignment: assigned ? { requirement: "photo_effect", revision: 1 } : {},
  }));
  const report = () => ({
    scope: `portal:${currentPortal}`, name: currentPortal === "argentina" ? "Universal Argentina" : "Universal Chile", portal_id: currentPortal,
    period: "", campaign_period: "2026-09", fingerprint: assigned ? "v2" : "v1", target: { veo: 50, photo_effect: 50 },
    buckets: { delivered: { veo: 261, photo_effect: 95, unknown: 0 }, generated: { veo: 2, photo_effect: 2, unknown: 0 },
      in_progress: zero, pending: { veo: 0, photo_effect: assigned ? 4 : 0, unknown: assigned ? 0 : 4 } },
    projected: { veo: 263, photo_effect: assigned ? 101 : 97, unknown: assigned ? 0 : 4 },
    suggestion: { veo: 0, photo_effect: 4, total: 4, projected_veo: 263, projected_photo_effect: 101, remaining_gap: 162, odd_total: false },
    excluded_art_tracks: 254, warnings: [], blockers: [], unmapped_campaigns: [], items: rows(), can_manage: true, can_view_accounts: true,
    campaigns: [{ id: "plan1", name: campaign.name, portal_id: "argentina", pending: 4, ready: assigned ? 1 : 0, writable: true }],
  });
  await page.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
    const json = data => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (pathname === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (pathname === "/batch/campaigns") return json({ items: [campaign], can_view_accounts: true });
    if (pathname === "/batch/campaigns/plan1") return json(campaign);
    if (pathname === "/batch/delivery-plan") { currentPortal = url.searchParams.get("portal_id") || "argentina"; return json(report()); }
    if (pathname === "/batch/delivery-plan/preview") return json({ preview_id: "reviewed", suggestion: report().suggestion, changes: rows().map(row => ({ ...row, after_type: "photo_effect" })) });
    if (pathname === "/batch/delivery-plan/apply") { assigned = true; return json({ ok: true }); }
    if (pathname === "/status/planjob1") return json({ status: "lyrics_approved", segments_json: [], segments_revision: 1 });
    if (pathname === "/generate") { generated = true; return json({ ok: true }); }
    return route.fallback();
  });
  await page.goto("/campaigns?view=accounts&account=argentina");
  await expect(page.getByRole("heading", { name: "Plan de entrega", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Generar seleccionadas (0)" })).toBeDisabled();
  await page.getByRole("button", { name: "Actualizar saldo" }).click();
  await expect(page.getByRole("button", { name: "Actualizar saldo" })).toBeEnabled();
  await page.screenshot({ path: path.resolve("../../.context/delivery-plan-account-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Revisar reparto", exact: true }).click();
  const proposal = page.getByRole("dialog", { name: "Revisar reparto de fondos" });
  await expect(proposal).toBeVisible(); expect(assigned).toBe(false); expect(generated).toBe(false);
  await proposal.getByRole("button", { name: "Aplicar asignaciones" }).click();
  await expect(page.getByText(/Reparto guardado/)).toBeVisible();
  expect(generated).toBe(false);
  await page.getByRole("button", { name: "Seleccionar listas (1)" }).click();
  await page.getByRole("button", { name: "Generar seleccionadas (1)" }).click();
  const generation = page.getByRole("dialog", { name: "Confirmar generación" });
  await expect(generation).toContainText("0 Veo + 1 foto con efecto");
  await generation.getByRole("button", { name: "Confirmar generación", exact: true }).click();
  await expect(page.getByText(/1 de 1 enviados a generación/)).toBeVisible(); expect(generated).toBe(true);
  await page.goto("/campaigns/plan1?view=plan");
  await expect(page.getByRole("link", { name: "Ver saldo de la cuenta" })).toBeVisible();
  await page.screenshot({ path: path.resolve("../../.context/delivery-plan-campaign-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Plan de entrega", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Actualizar saldo" }).click();
  await expect(page.getByRole("button", { name: "Actualizar saldo" })).toBeEnabled();
  await page.screenshot({ path: path.resolve("../../.context/delivery-plan-mobile.png"), fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});
