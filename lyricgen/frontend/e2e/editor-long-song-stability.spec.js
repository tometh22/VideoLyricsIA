import {test,expect} from '@playwright/test';
import {installEditorHarness,EDITOR_JOB_ID,createSyntheticWav} from './editor-harness.js';
for(const width of [1366,390])for(const surface of ['rail','block'])test(`35 complete lyric rows and stationary ${surface} editing at ${width}px`,async({page},testInfo)=>{
 const segments=Array.from({length:35},(_,i)=>({segment_id:`synthetic-long-${i}`,start:i*4+.5,end:i*4+3.5,text:i===6?'Una línea sintética bastante larga para verificar que se muestren todas las palabras sin puntos suspensivos ni recortes':`Línea sintética ${i+1} para revisar en esta prueba`}));
 const h=await installEditorHarness(page,{editorV2:true,segments});
 const wav=createSyntheticWav({durationSeconds:145});
 await page.route('**/e2e/audio.wav',r=>{
  const range=/bytes=(\d+)-(\d*)/.exec(r.request().headers().range||'');
  if(!range)return r.fulfill({contentType:'audio/wav',headers:{'Accept-Ranges':'bytes'},body:wav});
  const start=Number(range[1]),end=range[2]?Math.min(Number(range[2]),wav.length-1):wav.length-1;
  return r.fulfill({status:206,contentType:'audio/wav',headers:{'Accept-Ranges':'bytes','Content-Range':`bytes ${start}-${end}/${wav.length}`},body:wav.subarray(start,end+1)});
 });
 await page.addLocatorHandler(page.getByRole('dialog',{name:'Nuevo editor de letras'}),async d=>d.getByRole('button',{name:'Cancelar',exact:true}).click());
 await page.setViewportSize({width,height:844});await page.goto(`/videos/${EDITOR_JOB_ID}/edit-lyrics`);
 await page.getByRole('button',{name:/4 Lyrics/}).click();await expect(page.getByTestId('editor-mode-explainer')).toBeVisible();
 await page.getByRole('tab',{name:'Ajustar tiempos',exact:true}).click();await expect(page.getByTestId('timeline-label-row')).toHaveCount(35);
 const rail=page.getByTestId('timeline-label-row').nth(6);
 const text=rail.locator('span.whitespace-pre-wrap');await expect(text).toHaveText(segments[6].text);
 expect(await text.evaluate(n=>getComputedStyle(n).textOverflow!=='ellipsis'&&n.scrollHeight<=n.clientHeight+1)).toBe(true);
 if(surface==='rail')await rail.getByRole('button').first().dblclick();
 else {await rail.getByRole('button').first().click();await page.getByTestId('timeline-segment-body').nth(6).dblclick();}
 const input=page.getByRole('textbox',{name:'Letra de esta línea 7',exact:true});await expect(input).toBeFocused();
 await page.waitForFunction(()=>{const a=document.querySelector('audio');return a?.readyState>=2&&a.duration>140;});
 await page.locator('audio').evaluate(async a=>{a.currentTime=28.3;await a.play();});
 await expect.poll(()=>page.locator('audio').evaluate(a=>a.currentTime)).toBeGreaterThanOrEqual(28.3);
 // Set the caret without issuing an OS-dependent document-navigation key.
 await input.evaluate(n=>n.setSelectionRange(n.value.length,n.value.length));
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 const y=await page.evaluate(()=>scrollY),left=await page.getByTestId('timeline-scroll').evaluate(n=>n.scrollLeft);
 const fieldTop=(await input.boundingBox()).y;
 await input.pressSequentially(' corregida mientras avanza el audio',{delay:35});
 await expect(input).toBeFocused();expect(Math.abs((await input.boundingBox()).y-fieldTop)).toBeLessThan(2);expect(Math.abs(await page.evaluate(()=>scrollY)-y)).toBeLessThan(2);
 expect(Math.abs(await page.getByTestId('timeline-scroll').evaluate(n=>n.scrollLeft)-left)).toBeLessThan(2);
 await input.press('Enter');await expect(input).toHaveCount(0);
 await expect.poll(()=>h.saves.some(s=>s.segments.some(row=>row.segment_id==='synthetic-long-6'&&row.text.endsWith('corregida mientras avanza el audio')))).toBe(true);
 await page.locator('audio').evaluate(a=>a.pause());
 await page.screenshot({path:testInfo.outputPath(`long-editor-${surface}-${width}.png`)});
});
