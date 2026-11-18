import { test, expect } from '@playwright/test';
import { installEditorHarness } from './editor-harness';
test('click plays and playback follows without changing the edit selection', async ({ page }) => {
 const jobId='follow-layout';
 const segments=Array.from({length:36},(_,i)=>({segment_id:`row-${i}`,start:i*.1,end:i*.1+.08,text:`Línea ${i+1} para escuchar y corregir`,review:i%4===0}));
 const api=await installEditorHarness(page,{jobId,role:'admin',editorV2:true,segments});
 await page.route(`**/status/${jobId}`,route=>route.fulfill({json:{job_id:jobId,status:'transcribed_pending',campaign_id:'follow-test',campaign_item_id:'one',filename:'audio.wav',segments_revision:0,segments_json:segments}}));
 await page.addLocatorHandler(page.getByRole('dialog',{name:'Nuevo editor de letras'}),async dialog=>dialog.getByRole('button',{name:'Cancelar'}).click());
 await page.goto(`/review/${jobId}`);
 await expect(page.getByTestId('campaign-line-workspace')).toBeVisible();
 await expect.poll(()=>page.locator('audio').evaluate(a=>a.readyState)).toBeGreaterThan(0);
 await page.getByTestId('review-line-1').click();
 await expect.poll(()=>page.locator('audio').evaluate(a=>a.paused)).toBe(false);
 const input=page.getByLabel('Letra de esta línea',{exact:true});
 await input.focus();
 await page.locator('audio').evaluate(a=>{a.playbackRate=.25; a.currentTime=2.1;});
 await expect(page.locator('[data-playing="true"]')).toContainText(/Línea (2[2-9]|3[0-6])/);
 await expect(input).toHaveValue(segments[0].text);
 await expect(input).toBeFocused();
 await expect.poll(()=>page.getByTestId('review-line-list').evaluate(el=>el.scrollTop)).toBeGreaterThan(0);
 await page.locator('audio').evaluate(a=>a.pause());
 await expect(page.locator('[data-playing="true"]')).toHaveCount(0);
 expect(api.saves).toHaveLength(0);
 await page.getByTestId('review-line-list').evaluate(el=>{el.scrollTop=0;});
 for(const width of [1366,390]){
  await page.setViewportSize({width,height:844});
  await page.getByTestId('campaign-line-workspace').evaluate(el=>window.scrollTo(0,el.getBoundingClientRect().top+window.scrollY-160));
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  const bar=page.locator('[data-tour="editor-playbar"]');
  await expect(bar).toBeInViewport();
  expect((await bar.boundingBox()).y).toBeGreaterThanOrEqual(64);
  await page.screenshot({path:`/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/editor-layout-${width}.png`});
 }
});
