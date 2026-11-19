import { test, expect } from '@playwright/test';
import { installEditorHarness, EDITOR_JOB_ID } from './editor-harness.js';
const longText='Una frase larga que debe verse completa en la barra lateral sin ocultar ninguna de sus palabras al revisar la canción';
const segments=[{segment_id:'a',start:.1,end:.8,text:longText},{segment_id:'b',start:1.1,end:1.8,text:'Estribillo repetido'},{segment_id:'c',start:3,end:3.8,text:'Estribillo repetido'}];
async function open(page){await page.addLocatorHandler(page.getByRole('dialog',{name:'Nuevo editor de letras'}),async dialog=>dialog.getByRole('button',{name:'Cancelar',exact:true}).click());const h=await installEditorHarness(page,{editorV2:true,segments});await page.setViewportSize({width:1440,height:1000});await page.goto(`/videos/${EDITOR_JOB_ID}/edit-lyrics`);await page.getByRole('button',{name:/4 Lyrics/}).click();await expect(page.getByTestId('editor-mode-explainer')).toBeVisible();await page.waitForFunction(()=>{const a=document.querySelector('audio');return a&&a.readyState>=1&&a.duration>0;});await page.getByRole('tab',{name:'Ajustar tiempos',exact:true}).click();await expect(page.getByTestId('timeline-segment')).toHaveCount(3);return h;}
for(const surface of ['rail','block'])test(`double-click ${surface}: type, Enter flush, stable viewport, Space playback and delete`,async({page})=>{
 const h=await open(page);
 const labels=page.getByTestId('timeline-label-row');
 const first=labels.first();
 const dimensions=await first.evaluate(node=>{const text=node.querySelector('span.whitespace-pre-wrap');return {height:node.getBoundingClientRect().height,full: text.scrollHeight<=text.clientHeight+1};});
 expect(dimensions.full).toBe(true);expect(dimensions.height).toBeGreaterThan(48);
 const target=surface==='rail'?labels.nth(1).getByRole('button').first():page.getByTestId('timeline-segment-body').nth(1);
 await target.dblclick();
 const input=page.getByRole('textbox',{name:'Letra de esta línea 2',exact:true});
 await expect(input).toBeFocused();
 const before=await page.evaluate(()=>window.scrollY);
 await input.pressSequentially(' corregido con espacios',{delay:30});
 await expect(input).toHaveValue('Estribillo repetido corregido con espacios');
 expect(Math.abs((await page.evaluate(()=>window.scrollY))-before)).toBeLessThan(2);
 await input.press('Enter');await expect(input).toHaveCount(0);
 await expect.poll(()=>h.saves.some(save=>save.segments.some(row=>row.text==='Estribillo repetido corregido con espacios'))).toBe(true);
 await expect(page.getByRole('region',{name:'Sugerencia de texto para repeticiones'})).toBeVisible();
 await page.keyboard.press('Space');await expect.poll(()=>page.locator('audio').evaluate(a=>a.paused)).toBe(false);
 await page.keyboard.press('Space');await expect.poll(()=>page.locator('audio').evaluate(a=>a.paused)).toBe(true);
 // Separate delete controls on both surfaces.
 if(surface==='rail')await labels.nth(1).getByTestId('timeline-delete-line').click();
 else {await page.getByTestId('timeline-segment-body').nth(1).click();await page.getByRole('button',{name:'Eliminar línea 2 del editor',exact:true}).click();}
 await expect(page.getByTestId('timeline-segment')).toHaveCount(2);
 await expect.poll(()=>h.saves.some(save=>save.segments.length===2)).toBe(true);
});
test('inserts at the chosen exact time from the sidebar and persists new text',async({page})=>{
 const h=await open(page);
 await page.getByRole('button',{name:'Agregar línea desde la barra lateral'}).click();
 await page.getByLabel('Inicio de la línea nueva en segundos').fill('2.2');
 await page.getByRole('button',{name:'Insertar y escribir'}).click();
 await expect(page.getByTestId('timeline-segment')).toHaveCount(4);
 const input=page.getByRole('textbox',{name:'Letra de esta línea 3',exact:true});
 await expect(input).toBeFocused();await input.fill('La línea que faltaba');await input.press('Enter');
 await expect.poll(()=>h.saves.some(save=>save.segments.some(row=>row.text==='La línea que faltaba'&&Math.abs(Number(row.start)-2.2)<.001&&Number(row.end)<=2.95))).toBe(true);
});
test('right-click uses the pointer timestamp and long sidebar rows align with blocks',async({page})=>{
 await open(page);
 const row=page.getByTestId('timeline-label-row').nth(1);const block=page.getByTestId('timeline-segment').nth(1);
 await expect.poll(async()=>Math.abs((await row.boundingBox()).y+7-(await block.boundingBox()).y)).toBeLessThan(2);
 const lane=page.getByTestId('timeline-lane');const box=await lane.boundingBox();
 const px=Number(await lane.getAttribute('data-px-per-sec'));
 await lane.click({button:'right',position:{x:2.2*px,y:10}});
 expect(Number(await page.getByLabel('Inicio de la línea nueva en segundos').inputValue())).toBeCloseTo(2.2,1);
 await page.getByLabel('Studio de tiempos').evaluate(node=>window.scrollTo({top:node.getBoundingClientRect().top+window.scrollY-90,behavior:'instant'}));
 await page.screenshot({path:'/Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/editor-controls-sidebar.png'});
});
test('offers a timing correction for later repetitions without applying it automatically',async({page})=>{
 const h=await open(page);
 const edge=page.getByTestId('timeline-edge-end').nth(1);await edge.evaluate(node=>node.scrollIntoView({block:'center',behavior:'instant'}));
 const box=await edge.boundingBox();const lane=page.getByTestId('timeline-lane');const px=Number(await lane.getAttribute('data-px-per-sec'));
 await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+px*.15,box.y+box.height/2,{steps:5});await page.mouse.up();
 const panel=page.getByRole('region',{name:'Sugerencia de duración para repeticiones'});await expect(panel).toBeVisible();
 await expect(panel.getByRole('button',{name:'Aplicar duración a las marcadas'})).toBeDisabled();
 expect(h.saves.every(save=>Number(save.segments.find(row=>row.segment_id==='c').end)===3.8)).toBe(true);
 await panel.getByRole('checkbox').check();await panel.getByRole('button',{name:'Aplicar duración a las marcadas'}).click();
 await expect.poll(()=>h.saves.some(save=>{const source=save.segments.find(row=>row.segment_id==='b'),target=save.segments.find(row=>row.segment_id==='c');return Number(target.start)===3&&Math.abs((target.end-target.start)-(source.end-source.start))<.001;})).toBe(true);
});
