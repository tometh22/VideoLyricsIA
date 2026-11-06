import { test, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { installEditorHarness } from "./editor-harness";
import { installCampaignApi, song, expectNoHorizontalOverflow } from "./campaign-harness";
import { LYRIC_LOOK_CODES } from "../src/lib/lyricLooks";
import { EFFECT_CODES } from "../src/lib/catalogCodes";

for (const width of [1440, 390]) for (const classic of [false, true]) {
  test(`50-song ${classic ? "classic" : "pack"} batch: review exact split and generate at ${width}px`, async ({ page }) => {
    await mkdir(path.resolve("../../.context"), { recursive: true });
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1050 });
    await installEditorHarness(page, { jobId: "song-1", role: "admin" });
    let frozen;
    const api = await installCampaignApi(page, { songs: Array.from({ length: 50 }, (_, i) => song(i + 1, "ready")),
      onRequest: async ({ path: routePath, request, state, json }) => {
        if (routePath === '/batch/campaigns/workflow/creative') return json({
          plan: state.plan, can_manage: true, portal_id: 'argentina', can_view_account_balance: true,
          mix_catalog: { looks: LYRIC_LOOK_CODES.map(id => ({ id, compatible: !['pop70', 'degrade', 'cinetico'].includes(id), reason: 'Puede reemplazar el fondo; no participa del reparto foto/video.' })),
            effects: EFFECT_CODES.filter(e => e && !['none', 'foto_viva'].includes(e)), editable_statuses: ['lyrics_approved', 'transcribed_pending'] },
          items: state.songs.map(s => ({ id: s.item_id, job_id: s.job_id, title: s.title, artist: s.artist, status: s.stage === 'ready' ? 'lyrics_approved' : 'queued', settings: s.settings || {}, assignment: s.assignment || {} })),
        });
        if (routePath === '/batch/campaigns/workflow/creative/mix-preview') {
          const body = request.postDataJSON(); state.mixRequest = body;
          const changes = state.songs.map((s, i) => ({ item_id: s.item_id, title: s.title, artist: s.artist, before: {}, after: {
            lyric_look: body.looks[i % body.looks.length], effect: i < 25 ? body.effects[i % body.effects.length] : '',
            movement_style: i < 25 ? 'foto-parallax' : 'estandar', match_lyrics: true, lyric_variant: '',
          } }));
          const count = (key, rows) => rows.reduce((out, c) => ({ ...out, [c.after[key]]: (out[c.after[key]] || 0) + 1 }), {});
          frozen = { preview_id: 'reviewed-mix', skipped: [], ready_item_ids: body.item_ids, changes,
            mix: { total: 50, photo_effect: 25, veo: 25, looks: count('lyric_look', changes), effects: count('effect', changes.slice(0, 25)) } };
          return json(frozen);
        }
        if (routePath === '/batch/campaigns/workflow/creative/apply') {
          expect(request.postDataJSON().preview_id).toBe('reviewed-mix');
          state.plan.revision++;
          for (const change of frozen.changes) {
            const s = state.songs.find(s => s.item_id === change.item_id);
            s.settings = change.after; s.assignment = { group_name: change.after.lyric_look, revision: state.plan.revision };
          }
          return json({ revision: state.plan.revision });
        }
        if (routePath === '/batch/delivery-plan') return json({ name: 'Universal Argentina', projected: { photo_effect: 10, veo: 40, unknown: 50 }, warnings: [],
          items: state.songs.map(s => ({ item_id: s.item_id, campaign_id: 'workflow', stage: 'pending', type: 'unknown' })),
        });
      },
    });
    await page.goto('/campaigns/workflow?view=ready');
    await page.getByRole('checkbox', { name: width < 768 ? 'Seleccionar todas (50)' : 'Seleccionar 50 canciones' }).check();
    await page.getByRole('region', { name: 'Acciones sobre la selección' }).getByRole('button', { name: 'Preparar lote (50)' }).click();
    const dialog = page.getByRole('dialog', { name: 'Preparar lote' });
    await expect(dialog.getByLabel('25 fotos y 25 videos')).toBeVisible();
    await dialog.getByRole('button', { name: 'Lluvia', exact: true }).click();
    await page.screenshot({ path: path.resolve(`../../.context/batch-mix-background-${classic ? "classic" : "pack"}-${width}.png`), animations: 'disabled' });
    await dialog.getByRole('button', { name: 'Elegir looks →' }).click();
    await expect(dialog.getByRole('button', { name: 'Look Cinético', exact: true })).toBeDisabled();
    if (classic) {
      for (const name of ['Pincel', 'Cósmico', 'Cine']) await dialog.getByRole('button', { name: `Look ${name}`, exact: true }).click();
      await expect(dialog.getByRole('button', { name: 'Revisar reparto de 50 canciones' })).toBeDisabled();
      await dialog.getByRole('button', { name: 'Look Clásico — letra simple', exact: true }).click();
      await expect(dialog.getByRole('button', { name: 'Revisar reparto de 50 canciones' })).toBeEnabled();
    }
    await page.screenshot({ path: path.resolve(`../../.context/batch-mix-design-${classic ? "classic" : "pack"}-${width}.png`), animations: 'disabled' });
    await dialog.getByRole('button', { name: 'Revisar reparto de 50 canciones' }).click();
    const review = dialog.getByRole('region', { name: 'Revisar lote' });
    await expect(review.getByRole('row', { name: 'Con este lote 35 65 0' })).toBeVisible();
    expect(api.generations).toHaveLength(0);
    expect(api.mixRequest.looks).toEqual(classic ? [''] : ['pincel', 'cosmico', 'cine']);
    expect(api.mixRequest.effects).toEqual(['bokeh', 'embers', 'rain']);
    await expectNoHorizontalOverflow(page, expect);
    await page.screenshot({ path: path.resolve(`../../.context/batch-mix-review-${classic ? "classic" : "pack"}-${width}.png`), animations: 'disabled' });
    await review.getByRole('button', { name: 'Generar 50 videos' }).click();
    await expect(page.getByText(/50 trabajos enviados/)).toBeVisible({ timeout: 20000 });
    expect(api.generations).toHaveLength(50);
    const field = (body, name) => body.match(new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]*)`))?.[1];
    const photos = api.generations.filter(body => field(body, 'movement_style') === 'foto-parallax');
    expect(photos).toHaveLength(25);
    expect(['bokeh', 'embers', 'rain'].map(effect => photos.filter(body => field(body, 'effect') === effect).length).sort()).toEqual([8, 8, 9]);
    expect((classic ? [''] : ['pincel', 'cosmico', 'cine']).map(look => api.generations.filter(body => field(body, 'lyric_look') === look).length).sort()).toEqual(classic ? [50] : [16, 17, 17]);
    expect(new Set(api.generations.map(body => field(body, 'job_id'))).size).toBe(50);
  });
}
