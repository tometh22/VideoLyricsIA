import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

const campaign = { id: "campaign-1", name: "Campaña de prueba", status: "active", registered_count: 3, counters: {}, default_render_params: {} };
const rows = [
  { item_id: "i1", job_id: "j1", title: "Lista para revisar", state: "ready" },
  { item_id: "i2", job_id: "j2", title: "Letra ya aprobada", state: "approved" },
  { item_id: "i3", job_id: "j3", title: "Falló procesamiento", state: "failed" },
];

for (const path of ["/admin/cola", "/campaigns/campaign-1"]) {
  test(`${path}: cards, tabs, browser history and approved return`, async ({ page }) => {
    await installEditorHarness(page, { role: "admin" });
    const mutations = [];
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (request.isNavigationRequest() && url.pathname === "/admin/cola") {
        return route.fulfill({ response: await route.fetch({ url: `${url.origin}/` }) });
      }
      if (url.pathname === "/service-status/summary") return json({ status: "operational", incidents: [] });
      if (url.pathname === "/auth/me") return json({ id: "e2e-user", email: "e2e@example.test", role: "admin", features: {} });
      if (url.pathname.startsWith("/batch/")) {
        if (request.method() !== "GET") { mutations.push(url.pathname); return json({}, 405); }
        if (url.pathname.endsWith("/access")) return json({ enabled: true });
        if (url.pathname === "/batch/campaigns") return json({ items: [campaign] });
        if (url.pathname.endsWith("/items")) return json({ items: [], pages: 1 });
        if (url.pathname.endsWith("/review-queue")) {
          const scope = url.searchParams.get("scope");
          const items = rows.filter((row) => scope === "all" || (scope === "approved" ? row.state === "approved" : row.state !== "approved"));
          return json({ items, pages: 1, total: items.length, scope: { key: scope, label: scope, total: items.length }, campaign_totals: { songs: 3, approved: 1 }, counters: { ready: 1 } });
        }
        return json(campaign);
      }
      // Exercise the error-state return as well: a missing detail must not
      // strand the reviewer or silently allocate another campaign job.
      if (url.pathname === "/status/j2") return json({}, 404);
      if (!["localhost", "127.0.0.1"].includes(url.hostname)) return route.abort();
      return route.fallback();
    });
    const announcement = page.getByRole("dialog");
    await page.addLocatorHandler(announcement, async () => {
      await announcement.getByRole("button", { name: "Cancelar" }).click();
    });
    await page.goto(path);
    await expect(page.getByText("Lista para revisar", { exact: true })).toBeVisible();
    await page.getByRole("navigation", { name: "Resumen de la campaña" }).getByRole("button", { name: /Aprobadas/ }).click();
    await expect(page.getByText("Letra ya aprobada", { exact: true })).toBeVisible();
    await expect(page.getByText("Lista para revisar", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("tab", { name: /Aprobadas/ })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: /Aprobadas/ }).press("ArrowRight");
    await expect(page.getByText("Lista para revisar", { exact: true })).toBeVisible();
    await expect(page.getByText("Fallida", { exact: true })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("tab", { name: /Aprobadas/ })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: "Ver canción", exact: true }).click();
    await expect(page).toHaveURL(/\/videos\/j2\?return_to=/);
    await page.getByRole("button", { name: "Volver", exact: true }).click();
    await expect(page.getByRole("tab", { name: /Aprobadas/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Letra ya aprobada", { exact: true })).toBeVisible();
    expect(mutations).toEqual([]);
  });
}
