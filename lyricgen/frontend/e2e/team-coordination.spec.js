import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

for (const width of [1440, 390]) {
  test(`team assignment persists through reload and opens operator view at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1100 });
    await installEditorHarness(page, { role: "admin" });
    const members = [
      { id: 2, name: "Lucía", active: true },
      { id: 3, name: "Mateo", active: true },
      { id: 4, name: "Sofía", active: true },
    ];
    const tasks = Array.from({ length: 18 }, (_, i) => ({
      id: `item-${i}`,
      title:
        [
          "Volver a empezar",
          "Cerca del sol",
          "Todo cambia",
          "Un lugar",
          "Sin mirar atrás",
          "La distancia",
        ][i % 6] + (i > 5 ? ` · versión ${Math.floor(i / 6) + 1}` : ""),
      artist: "Banda del Sur",
      campaign_id: `campaign-${Math.floor(i / 6)}`,
      campaign: [
        "Lanzamientos · Noviembre",
        "Catálogo · Pop argentino",
        "Sesiones · Acústicos",
      ][Math.floor(i / 6)],
      assignee_id: i % 5 === 0 ? null : members[i % 3].id,
      priority: "normal",
      due_date: "2026-11-20",
      status: ["pending", "working", "review", "done", "blocked", "processing"][
        i % 6
      ],
      phase: "lyrics_ready",
      note: "",
      blocked_reason: "",
      version: 0,
      job_id: `job-${i}`,
    }));
    const count = (list) =>
      list.reduce(
        (acc, t) => ({ ...acc, [t.status]: (acc[t.status] || 0) + 1 }),
        {},
      );
    await page.route("**/*", async (route) => {
      const req = route.request(),
        url = new URL(req.url()),
        path = url.pathname;
      const json = (value) =>
        route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(value),
        });
      if (path === "/auth/me")
        return json({
          id: 2,
          username: "Agus",
          role: "admin",
          tenant_id: "e2e-team",
        });
      if (path === "/batch/campaigns/access") return json({ enabled: true });
      if (path === "/service-status/summary")
        return json({ status: "operational", incidents: [] });
      if (path === "/team/access")
        return json({
          can_manage: true,
          can_grant: true,
          tenant_id: "e2e-team",
          tenants: ["e2e-team"],
        });
      const scope =
        url.searchParams.get("mine") === "true"
          ? tasks.filter((t) => t.assignee_id === 2)
          : tasks;
      if (path === "/team/dashboard")
        return json({
          can_manage: true,
          counts: count(scope),
          unassigned: scope.filter((t) => !t.assignee_id).length,
          members: members.map((m) => ({
            ...m,
            counts: count(scope.filter((t) => t.assignee_id === m.id)),
          })),
          campaigns: [0, 1, 2].map((i) => ({
            id: `campaign-${i}`,
            name: tasks[i * 6].campaign,
            counts: count(
              scope.filter((t) => t.campaign_id === `campaign-${i}`),
            ),
          })),
          activity: [],
        });
      if (path === "/team/tasks") {
        const filtered = scope.filter(
          (t) =>
            !url.searchParams.get("campaign_id") ||
            t.campaign_id === url.searchParams.get("campaign_id"),
        );
        return json({ items: filtered, total: filtered.length });
      }
      if (path === "/team/assignments") {
        const body = req.postDataJSON();
        for (const item of body.items)
          Object.assign(
            tasks.find((t) => t.id === item.id),
            {
              assignee_id: body.assignee_id,
              priority: body.priority,
              due_date: body.due_date,
              version: item.version + 1,
            },
          );
        return json({ assigned: body.items.map((t) => t.id), skipped: [] });
      }
      return route.fallback();
    });
    const announcement = page.getByRole("dialog", {
      name: "Nuevo editor de letras",
    });
    await page.addLocatorHandler(announcement, () =>
      announcement.getByRole("button", { name: "Cancelar" }).click(),
    );
    await page.goto("/team");
    await expect(
      page.getByRole("heading", { name: "Cada entrega, en buenas manos." }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Volver a empezar", exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: `../../.context/team-implemented-${width}.png`,
    });
    await page
      .getByRole("checkbox", {
        name: "Seleccionar Volver a empezar",
        exact: true,
      })
      .check();
    await page.getByRole("button", { name: "Asignar selección" }).click();
    const dialog = page.getByRole("dialog", { name: "Asignar videos" });
    await dialog
      .getByRole("combobox", { name: "Responsable", exact: true })
      .selectOption("2");
    await dialog.getByRole("button", { name: "Confirmar asignación" }).click();
    await expect(page.getByText("1 videos actualizados.")).toBeVisible();
    await page.reload();
    const row = page
      .locator("tr")
      .filter({
        has: page.getByRole("button", {
          name: "Volver a empezar",
          exact: true,
        }),
      });
    await expect(row).toContainText("Lucía");
    await page
      .locator(".tp-view-tabs")
      .getByRole("link", { name: "Mis tareas", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Mis tareas", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Asignar campaña", exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "Volver a empezar", exact: true })
      .click();
    await expect(
      page
        .getByRole("dialog", { name: "Detalle de tarea" })
        .getByRole("link", { name: /Abrir revisión de letra/ }),
    ).toHaveAttribute("href", /\/review\/job-0/);
    await page.keyboard.press("Escape");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}
