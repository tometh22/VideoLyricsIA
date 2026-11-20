import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { expectNoHorizontalOverflow, installCampaignApi, song } from "./campaign-harness";

// PUBLISH_REVIEW_SUMMARY=prompt: el envío por campaña muestra UNA vez, con el
// total por canción, las líneas que nadie escuchó y los avisos sin mirar, y
// "Publicar igual" manda los tokens de lo que se vio. Letras inventadas.
const summary = {
  job_id: "song-2", title: "Canción 2", artist: "Otro artista", listening_status: "measured",
  lines_total: 32, lines_unheard: 3, warnings_count: 2, needs_attention: true, token: "e".repeat(64),
  warnings_by_category: { official: 1, two_ears: 0, style: 1 },
  unheard: [
    { line_index: 5, start: 61.4, end: 64, text: "Vuelvo a la esquina de siempre" },
    { line_index: 6, start: 64.2, end: 67, text: "con la luna a contramano" },
    { line_index: 20, start: 150, end: 153, text: "y el tren que no se va" },
  ],
  warnings: [
    { id: "w-off", category: "official", title: "Se escucha distinto", count: 1,
      occurrences: [{ start: 88, before: "la ciudad se ve tan sola", after: "la ciudad se ve tan sorda" }] },
    { id: "w-dot", category: "style", title: "Quitar el punto final", count: 4,
      occurrences: [{ start: 12, before: "Lejos de casa.", after: "Lejos de casa" }] },
  ],
};

for (const width of [1440, 390]) {
  test(`campaign send shows one summary before publishing at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installEditorHarness(page, { jobId: "song-1", role: "admin" });
    const previews = [];
    const api = await installCampaignApi(page, {
      songs: [song(1, "approved"), song(2, "approved"), song(3, "delivered")],
      onRequest: async ({ path, url, json }) => {
        if (path === "/batch/campaigns/workflow/deliveries/publish-review") {
          previews.push(url.searchParams.get("job_ids"));
          return json({ mode: "prompt", available: true, summaries: [summary],
            totals: { songs: 2, songs_needing_attention: 1, lines_unheard: 3, lines_total: 64, warnings: 2 } });
        }
        return false;
      },
    });
    await page.goto("/campaigns/workflow");
    await page.getByRole("tab", { name: /^Aprobada/ }).click();
    await page.getByRole("checkbox", { name: width < 768 ? "Seleccionar todas (2)" : "Seleccionar 2 canciones" }).check();
    await page.getByRole("region", { name: "Acciones sobre la selección" }).getByRole("button", { name: /Enviar 2 al portal/ }).click();
    const send = page.getByRole("dialog", { name: "Enviar videos aprobados" });
    await send.getByLabel("Portal de destino").selectOption("chile");

    const review = send.getByRole("region", { name: "Resumen antes de publicar" });
    await expect(review).toContainText("3 líneas sin escuchar y 2 avisos sin mirar.");
    await expect(review).toContainText("3 de 32 líneas sin escuchar · 2 avisos sin mirar");
    await expect(review).toContainText("1:01");
    await expect(review).toContainText("Vuelvo a la esquina de siempre");
    await expect(review).toContainText("Letra oficial");
    await expect(review).toContainText("Estilo del portal");
    expect(previews.at(-1).split(",").sort()).toEqual(["song-1", "song-2"]);
    await expect(send.getByRole("button", { name: "Confirmar envío", exact: true })).toHaveCount(0);
    await page.screenshot({ path: `test-results/campaign-publish-review-${width}.png` });
    await expectNoHorizontalOverflow(page, expect);

    await send.getByRole("button", { name: "Publicar igual", exact: true }).click();
    await expect(page.getByText(/2 de 2 enviados/)).toBeVisible();
    expect(api.deliveries).toHaveLength(1);
    expect(api.deliveries[0].publish_review_tokens).toEqual({ "song-2": summary.token });
  });
}

test("campaign send without anything to show keeps the usual confirmation", async ({ page }) => {
  await installEditorHarness(page, { jobId: "song-1", role: "admin" });
  const api = await installCampaignApi(page, {
    songs: [song(1, "approved")],
    onRequest: async ({ path, json }) => (path.endsWith("/deliveries/publish-review")
      ? json({ mode: "observe", available: true, summaries: [], totals: {} }) : false),
  });
  await page.goto("/campaigns/workflow");
  await page.getByRole("tab", { name: /^Aprobada/ }).click();
  await page.getByRole("checkbox", { name: "Seleccionar Canción 1" }).check();
  await page.getByRole("region", { name: "Acciones sobre la selección" }).getByRole("button", { name: /Enviar 1 al portal/ }).click();
  const send = page.getByRole("dialog", { name: "Enviar videos aprobados" });
  await send.getByLabel("Portal de destino").selectOption("chile");
  await expect(send.getByRole("region", { name: "Resumen antes de publicar" })).toHaveCount(0);
  await send.getByRole("button", { name: "Confirmar envío", exact: true }).click();
  await expect(page.getByText(/1 de 1 enviado/)).toBeVisible();
  expect(api.deliveries[0].publish_review_tokens).toBeUndefined();
});
