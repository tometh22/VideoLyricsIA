import { test, expect } from '@playwright/test';
import path from 'node:path';
import { installEditorHarness } from './editor-harness';
import { expectNoHorizontalOverflow, installCampaignApi } from './campaign-harness';

for (const width of [1440, 390]) {
  test(`find delivered campaigns and client corrections at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installEditorHarness(page, { jobId: 'luces', role: 'admin' });
    await installCampaignApi(page, { songs: [] });
    await page.route('**/batch/campaigns', route => route.fulfill({ json: { can_view_accounts: true, features: { change_requests_inbox: true }, items: [
      { id: 'religiosas', name: 'Canciones Religiosas', status: 'completed', kind: 'lyric_video', pipeline: { portal_status_available: true, counts: { delivered: 15 } } },
      { id: 'agosto', name: 'UMG Agosto — Correcciones', status: 'completed', kind: 'lyric_video', pipeline: { portal_status_available: true, counts: { delivered: 300 }, flags: { change_requests_open: 1, oldest_change_request_at: '2026-11-01T12:00:00Z' } } },
    ] } }));
    await page.route('**/batch/campaigns/agosto/change-requests?*', route => route.fulfill({ json: { available: true, counts: { open: 1, resolved: 0 }, next_cursor: null, items: [
      { id: 7, song: 'Luces', artist: 'Babasónicos', comment: 'Corregir la letra del segundo verso.', submitted_at: '2026-11-01T12:00:00Z', song_id: 'luces' },
    ] } }));
    await page.goto('/campaigns');
    await expect(page.getByRole('button', { name: 'Canciones Religiosas', exact: true })).toBeVisible();
    await page.getByRole('button', { name: /Ver canciones con pedidos/ }).click();
    await expect(page.getByText('Luces', { exact: false })).toBeVisible();
    await expect(page.getByText('Corregir la letra del segundo verso.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Abrir y resolver' })).toHaveAttribute('href', '/admin?section=cambios&change_request_id=7');
    await page.reload();
    await expect(page.getByText('Corregir la letra del segundo verso.')).toBeVisible();
    await expectNoHorizontalOverflow(page, expect);
    await page.screenshot({ path: path.resolve(`../../.context/campaign-change-requests-${width}.png`), fullPage: true, animations: 'disabled' });
  });
}
