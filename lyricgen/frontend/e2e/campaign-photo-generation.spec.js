import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { installCampaignApi, song, expectNoHorizontalOverflow } from "./campaign-harness";

for (const width of [1440, 390]) {
  test(`ready songs can save and generate a photo 50/50 split at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1000 });
    await installEditorHarness(page, { jobId: "song-1", role: "admin" });
    const api = await installCampaignApi(page, { songs: [
      ...Array.from({ length: 10 }, (_, i) => song(i + 1, "ready", { settings: { font: "poppins-bold" } })),
      song(11, "lyrics"), song(12, "delivered"),
    ] });
    await page.goto("/campaigns/workflow?view=ready");
    await page.getByRole("checkbox", { name: width < 768 ? "Seleccionar todas (10)" : "Seleccionar 10 canciones" }).check();
    await page.getByRole("region", { name: "Acciones sobre la selección" }).getByRole("button", { name: "Generar 10 videos" }).click();
    await page.getByRole("dialog", { name: "Confirmar generación" }).getByRole("button", { name: "Cambiar fondo" }).click();
    const assignment = page.getByRole("dialog", { name: "Elegir fondo" });
    await assignment.getByRole("button", { name: /Foto fija/ }).click();
    await assignment.getByRole("button", { name: "Mitad y mitad" }).click();
    await expect(assignment.getByLabel("Efecto de foto del grupo 1")).not.toBeVisible();
    await expect(assignment.getByLabel("Motivo del cambio")).not.toBeVisible();
    await expect(assignment.getByRole("button", { name: "Mitad y mitad" })).toHaveAttribute("aria-pressed", "true");
    await expectNoHorizontalOverflow(page, expect);
    await expect(assignment.getByRole("button", { name: "Bokeh", exact: true })).toHaveAttribute("aria-pressed", "false");
    await expect(assignment.getByRole("button", { name: "Mitad y mitad" })).toHaveAttribute("aria-pressed", "true");
    await page.screenshot({ path: `test-results/photo-preset-${width}.png`, animations: "disabled" });
    await assignment.getByRole("button", { name: "Continuar" }).click();
    await expect(assignment.getByText("5 · Bokeh")).toBeVisible();
    await expect(assignment.getByText("5 · Chispas")).toBeVisible();
    expect(api.lastPreview.reason).toBe("Configuración de estilos y reparto desde la campaña");
    expect(api.generations).toHaveLength(0);
    await page.screenshot({ path: `test-results/photo-confirm-${width}.png`, animations: "disabled" });
    await expectNoHorizontalOverflow(page, expect);
    await assignment.getByRole("button", { name: "Generar 10 videos" }).click();
    await expect(page.getByText(/10 trabajos enviados/)).toBeVisible();
    expect(api.generations).toHaveLength(10);
    expect(api.generations.filter(body => body.includes('name="effect"\r\n\r\nbokeh'))).toHaveLength(5);
    expect(api.generations.filter(body => body.includes('name="effect"\r\n\r\nembers'))).toHaveLength(5);
    expect(api.generations.every(body => body.includes('name="movement_style"\r\n\r\nfoto-parallax') && body.includes('name="match_lyrics"\r\n\r\ntrue') && body.includes('name="font"\r\n\r\npoppins-bold') && body.includes('name="campaign_creative_revision"\r\n\r\n1'))).toBe(true);
    expect(api.songs.find(song => song.ordinal === 11).stage).toBe("lyrics");
    expect(api.songs.find(song => song.ordinal === 12).stage).toBe("delivered");
  });
}
