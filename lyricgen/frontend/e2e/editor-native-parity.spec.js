import { test, expect } from '@playwright/test';
import { installEditorHarness, EDITOR_JOB_ID } from './editor-harness';
test('individual wizard uses the same native preview across editor modes',async({page})=>{
 await page.addLocatorHandler(page.getByRole('dialog',{name:'Nuevo editor de letras'}),async d=>d.getByRole('button',{name:'Cancelar',exact:true}).click());
 await installEditorHarness(page,{editorV2:true,segments:[{segment_id:'a',start:0,end:2,text:'Texto propio de prueba'},{segment_id:'b',start:3,end:5,text:'Segunda frase'}]});
 const bodies=[];await page.route(`**/editor/${EDITOR_JOB_ID}/text-preview`,r=>{bodies.push(r.request().postDataJSON());return r.fallback();});
 await page.goto(`/videos/${EDITOR_JOB_ID}/edit-lyrics`);await page.getByRole('button',{name:/4 Lyrics/}).click();
 const preview=page.getByTestId('editor-native-preview-slot').getByTestId('native-lyric-preview');
 await expect(preview.getByRole('img')).toHaveAttribute('aria-busy','false',{timeout:20000});await expect(preview.locator('canvas')).toBeVisible();
 await expect(page.getByTestId('native-lyric-preview')).toHaveCount(1);
 await page.getByRole('tab',{name:'Ajustar tiempos',exact:true}).click();await page.getByTestId('timeline-segment-body').first().dblclick();
 const field=page.getByRole('textbox',{name:'Letra de esta línea 1',exact:true});await field.fill('Texto cambiado sin salir de la línea');
 await expect.poll(()=>bodies.some(b=>b.segments.some(s=>s.text==='Texto cambiado sin salir de la línea'))).toBe(true);
 await expect(field).toBeFocused();await expect(preview.getByRole('img')).toHaveAttribute('aria-busy','false');
 await field.press('Enter');await page.getByRole('tab',{name:'Revisar letra',exact:true}).click();await expect(page.getByTestId('native-lyric-preview')).toHaveCount(1);
 await page.screenshot({path:test.info().outputPath('individual-native-preview.png')});
});
test('does not expose the raw bootstrap snapshot outside the hydration overlay',async({page})=>{
 await page.addLocatorHandler(page.getByRole('dialog',{name:'Nuevo editor de letras'}),async d=>d.getByRole('button',{name:'Cancelar',exact:true}).click());
 await installEditorHarness(page,{editorV2:true});let release;const gate=new Promise(resolve=>{release=resolve;});let requests=0;
 await page.route(`**/editor/${EDITOR_JOB_ID}`,async route=>{if(route.request().method()==='GET')await gate;return route.fallback();});
 await page.route(`**/editor/${EDITOR_JOB_ID}/text-preview`,route=>{requests++;return route.fallback();});
 try{
 await page.goto(`/videos/${EDITOR_JOB_ID}/edit-lyrics`);await page.getByRole('button',{name:/4 Lyrics/}).click();
 const slot=page.getByTestId('editor-native-preview-slot');await expect(slot.getByRole('status')).toContainText('Preparando vista previa');expect(requests).toBe(0);
 release();await expect(slot.getByRole('img')).toHaveAttribute('aria-busy','false',{timeout:20000});
 }finally{release();}
});
