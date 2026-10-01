// Local readonly QA. Does not use signed-in browser profiles or product APIs.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { chromium } = await import(process.env.REVIEWER_PLAYWRIGHT_MODULE);
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1440,height:960}});
const errors=[];page.on('pageerror',e=>errors.push(String(e)));
try {
  await page.goto('http://127.0.0.1:8768/bounded-delivery-v1/index.html');
  assert.equal(await page.locator('#song option').count(),6);
  assert.match(await page.locator('#song option').first().textContent(),/Magia Veneno/);
  await page.waitForFunction(()=>document.querySelector('audio').readyState>=1);
  const duration=await page.locator('audio').evaluate(a=>a.duration);
  assert(duration>150);
  await page.locator('[data-line="36"] button').click();
  await page.locator('audio').evaluate(async a=>{a.currentTime=150;await a.play();});
  await page.waitForFunction(()=>document.querySelector('audio').currentTime>150.3);
  assert.equal(await page.locator('audio').evaluate(a=>a.paused),false);
  await page.locator('audio').evaluate(a=>a.pause());
  await page.selectOption('#song','2');
  assert.equal(await page.locator('#lines tr.changed').count(),2);
  await page.locator('[data-line="12"] button').click();
  await page.screenshot({path:process.env.REVIEWER_QA_SCREENSHOT});
  assert.deepEqual(errors,[]);
  const result={options:6,bersuit_changes:2,full_audio_duration:duration,
    continued_after_line_end:true,page_errors:errors,live_documents_modified:false,
    scope:'readonly_preview_browser_QA_not_staging_adoption'};
  await fs.writeFile(process.env.REVIEWER_QA_REPORT,JSON.stringify(result,null,2),{flag:'wx',mode:0o600});
  console.log(JSON.stringify(result));
} finally {await browser.close();}
