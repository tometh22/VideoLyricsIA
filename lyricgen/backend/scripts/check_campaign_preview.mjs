// Local isolated browser QA; no product sessions, writes or approvals.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { chromium } = await import(process.env.REVIEWER_PLAYWRIGHT_MODULE);
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1440,height:960}});
const errors=[];page.on('pageerror',e=>errors.push(String(e)));
try {
  await page.goto('http://127.0.0.1:8768/campaign-first10-v1/index.html');
  assert.equal(await page.locator('#song option').count(),10);
  await page.selectOption('#song','1');
  assert.equal(await page.locator('#lines tr.changed').count(),1);
  assert.match(await page.locator('[data-line="6"]').innerText(),/vivo fuertes/);
  assert.doesNotMatch(await page.locator('[data-line="6"]').innerText(),/creciendo vivo/);
  await page.waitForFunction(()=>document.querySelector('audio').readyState>=1);
  const duration=await page.locator('audio').evaluate(a=>a.duration);
  assert(duration>230);
  await page.locator('[data-line="6"] button').click();
  await page.locator('audio').evaluate(async a=>{a.currentTime=43.7;await a.play();});
  await page.waitForFunction(()=>document.querySelector('audio').currentTime>44.1);
  assert.equal(await page.locator('audio').evaluate(a=>a.paused),false);
  await page.locator('audio').evaluate(a=>a.pause());
  assert.match(await page.locator('#summary').innerText(),/Cobertura de escucha completa/);
  await page.locator('#doubt').click();
  await page.locator('audio').evaluate(a=>a.pause());
  await page.screenshot({path:process.env.REVIEWER_QA_SCREENSHOT});
  assert.deepEqual(errors,[]);
  const report={candidates:10,new_text_repairs_shown:1,full_audio_duration:duration,
    continued_after_line_end:true,neighbor_word_not_duplicated:true,page_errors:errors,
    scope:'readonly_local_preview_not_staging_adoption',human_checked:false};
  await fs.writeFile(process.env.REVIEWER_QA_REPORT,JSON.stringify(report,null,2),{flag:'wx',mode:0o600});
  console.log(JSON.stringify(report));
} finally {await browser.close();}
