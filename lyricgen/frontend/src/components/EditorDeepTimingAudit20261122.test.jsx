import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import LyricsEditor from './LyricsEditor';
import { segmentsStore } from '../state/segmentsStore';
import { canonicalizeEditorSegments } from '../lib/segmentTiming';
import { mergeThreeWay } from '../editorMerge';
vi.mock('../i18n',()=>({useI18n:()=>({t:()=>undefined})}));
vi.mock('./OnboardingTour',()=>({EditorTour:()=>null}));
vi.mock('./ToastProvider',()=>({useToast:()=>({toast:vi.fn(),dismiss:vi.fn()})}));
afterEach(()=>{cleanup();segmentsStore._clearAll();localStorage.clear();vi.restoreAllMocks();});
it('keeps the final timestamp edit inside the actual audio with positive duration',async()=>{
 const persist=vi.fn().mockResolvedValue({ok:true,revision:1});
 const {container}=render(<LyricsEditor segments={[{segment_id:'one',start:1,end:2,text:'Frase'}]} transcribeJobId='deep-end'
 filename='synthetic.wav' onPersistSegments={persist} disableBeforeUnload audioUrl='https://example.test/synthetic.wav' onApprove={vi.fn()} onBack={vi.fn()}/>);
 const audio=container.querySelector('audio');Object.defineProperty(audio,'duration',{configurable:true,value:5});fireEvent.loadedMetadata(audio);
 fireEvent.doubleClick(screen.getByRole('button',{name:/Doble click para editar el tiempo de la línea 1/}));
 const input=screen.getByLabelText('Tiempo de inicio de la línea 1');fireEvent.change(input,{target:{value:'5'}});fireEvent.keyDown(input,{key:'Enter'});
 await waitFor(()=>expect(persist).toHaveBeenCalled(),{timeout:4500});
 const line=persist.mock.calls.at(-1)[1][0];
 expect(line.end).toBeGreaterThan(line.start);
 expect(line.end).toBeLessThanOrEqual(5);
});
it('does not move an independently inserted remote line during normalizing a rebase',()=>{
 const base=[{segment_id:'a',start:1,end:2,text:'Primera'},{segment_id:'b',start:3,end:4,text:'Segunda'}];
 const inserted={segment_id:'new',start:0,end:3.5,text:'Texto recuperado',review:true,timing_validation:{status:'unvalidated',reason:'estimated_placement'}};
 const merged=mergeThreeWay(base,base,[inserted,...base]).merged;
 const canonical=canonicalizeEditorSegments(merged);
 expect(canonical.find(x=>x.segment_id==='new')).toMatchObject({start:0,end:3.5});
 expect(canonical.every(x=>x.end<=4)).toBe(true);
});
it('does not autosave a moved remote insertion after a durable conflict',async()=>{
 const base=[{segment_id:'a',start:1,end:2,text:'Primera'},{segment_id:'b',start:3,end:4,text:'Segunda'}];
 const inserted={segment_id:'new',start:0,end:3.5,text:'Texto recuperado',review:true,timing_validation:{status:'unvalidated',reason:'estimated_placement'}};
 let server=[inserted,...base];let revision=2;const patches=[];
 const reply=(body,status=200)=>({ok:status>=200&&status<300,status,clone:()=>({json:async()=>body})});
 const request=vi.fn(async(path,options={})=>{
  if(path.endsWith('/lock/heartbeat'))return reply({acquired:true});
  if(path.endsWith('/lock'))return reply({});
  if(path.endsWith('/text-preview'))return reply({},503);
  if(path==='/analytics/events')return reply({accepted:1,rejected:0});
  if(!options.method)return reply({job_id:'deep-rebase-time',revision:1,segments:base,original_segments:base,lock:{active:false}});
  if(path!=='/editor/deep-rebase-time' || options.method!=='PATCH')return reply({});
  const body=JSON.parse(options.body);patches.push(body);
  if(body.base_revision===1)return reply({server_revision:2,server_segments:server},409);
  server=body.segments;revision++;return reply({revision,applied:true});
 });
 render(<LyricsEditor segments={base} transcribeJobId='deep-rebase-time' filename='synthetic.wav'
 requireLineReview disableBeforeUnload user={{id:42,tenant_id:'synthetic',features:{editor_v2:true}}}
 editorRequest={request} onPersistSegments={vi.fn()} onApprove={vi.fn()} onBack={vi.fn()}/>);
 await waitFor(()=>expect(screen.getByTestId('lyrics-editor')).toHaveAttribute('aria-busy','false'));
 fireEvent.change(screen.getByLabelText('Letra de esta línea'),{target:{value:'Primera corregida'}});
 fireEvent.blur(screen.getByLabelText('Letra de esta línea'));
 await waitFor(()=>expect(patches.length).toBeGreaterThanOrEqual(2),{timeout:3500});
 expect(server.find(row=>row.segment_id==='new')).toMatchObject({start:0,end:3.5});
});
