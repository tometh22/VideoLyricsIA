import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

for (const width of [1440, 390]) {
  test(`admin grants and revokes existing user campaign access at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installEditorHarness(page, { role: "admin" });
    await page.addLocatorHandler(page.getByRole("dialog", { name: "Nuevo editor de letras" }), async (dialog) => dialog.getByRole("button", { name: "Cancelar" }).click());
    let granted = false;
    let writes = 0;
    await page.route("**/*", async (route) => {
      const path = new URL(route.request().url()).pathname;
      const json = (data) => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
      if (path === "/admin/users") return json({ total: 1, users: [{ id: 79, username: "danniivanna04@gmail.com", email: "danniivanna04@gmail.com", role: "user", plan: "free", tenant_id: "reviewers", is_active: true }] });
      if (path.startsWith("/admin/users/79/campaign-access")) {
        if (route.request().method() === "PUT") {
          const body = route.request().postDataJSON();
          expect(body.expected_granted).toBe(granted);
          granted = body.enabled; writes += 1;
        }
        return json({ total: 1, user_active: true, campaigns_enabled: true, items: [{ id: "6637d82e93dd", name: "UMG Septiembre 2026 — HitsDeSiempre — Lyrics", tenant_id: "tomas@epical.digital", kind: "lyric_video", status: "active", inherited: null, granted }] });
      }
      if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
      if (path === "/team/access") return json({ can_manage: true, can_grant: true, tenant_id: "e2e-team", tenants: ["e2e-team"] });
      if (path === "/admin/stats") return json({});
      if (path === "/admin/change-requests") return json({ total: 0, items: [] });
      return route.fallback();
    });
    await page.goto("/admin?section=gestion");
    await page.getByRole("button", { name: "Acceso a campañas", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Acceso a campañas" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Dar acceso", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Quitar acceso", exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: "Cerrar", exact: true }).click();
    await page.reload();
    await page.getByRole("button", { name: "Acceso a campañas", exact: true }).click();
    await dialog.getByRole("button", { name: "Quitar acceso", exact: true }).click();
    expect(writes).toBe(1);
    await dialog.getByRole("button", { name: "Confirmar revocación", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Dar acceso", exact: true })).toBeVisible();
    expect(writes).toBe(2);
    const box = await dialog.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    if (process.env.TEAM_SCREENSHOTS) await page.screenshot({ path: `${process.env.TEAM_SCREENSHOTS}/campaign-access-${width}.png` });
  });
}
