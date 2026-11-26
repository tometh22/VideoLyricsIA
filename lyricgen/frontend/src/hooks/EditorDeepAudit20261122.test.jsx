import { useState } from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { mergeThreeWay } from '../editorMerge';
import { useEditorDocument } from './useEditorDocument';
import { useEditorAutosave } from './useEditorAutosave';
afterEach(()=>{cleanup();vi.restoreAllMocks();});
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,clone:()=>({json:async()=>body})});
async function persistedRebase(base, local, remote) {
 let stored=remote;const patches=[];
 const request=vi.fn(async(path,options={})=>{
  if(path.endsWith('/lock/heartbeat'))return reply({acquired:true});
  if(options.method==='DELETE')return reply({released:true});
  if(!options.method)return reply({job_id:'synthetic-deep-audit',revision:1,segments:base});
  const body=JSON.parse(options.body);patches.push(body);
  if(body.base_revision===1)return reply({server_revision:2,server_segments:remote},409);
  stored=body.segments;return reply({revision:3,applied:true});
 });
 const {result}=renderHook(()=>{
  const editor=useEditorDocument({jobId:'synthetic-deep-audit',enabled:true,request});
  const [segments,setSegments]=useState(local);
  const autosave=useEditorAutosave({enabled:!editor.loading,dirty:false,segments,save:editor.save,reconcile:editor.reconcile,onMerged:setSegments});
  return {editor,autosave};
 });
 await waitFor(()=>expect(result.current.editor.loading).toBe(false));
 await act(async()=>{await result.current.autosave.flush('autosave');});
 return {stored,patches};
}
it('preserves every row when a real id collides with a duplicate occurrence suffix',()=>{
 const rows=[{segment_id:'16',text:'Primera',start:1,end:2},{segment_id:'16',text:'Segunda',start:3,end:4},{segment_id:'16#1',text:'Tercera',start:5,end:6}];
 const result=mergeThreeWay(rows,rows,rows);
 expect(result.merged.map(x=>x.text)).toEqual(['Primera','Segunda','Tercera']);
});
it('preserves the corrected surviving occurrence after deleting the first duplicated id',async()=>{
 const base=[{segment_id:'16',text:'Primera',start:1,end:2},{segment_id:'16',text:'Repetida',start:5,end:6}];
 const {stored,patches}=await persistedRebase(base,[base[1]],[base[0],{...base[1],text:'Repetida corregida'}]);
 expect(patches).toHaveLength(2);expect(patches[1].base_revision).toBe(2);
 expect(stored.map(x=>x.text)).toEqual(['Repetida corregida']);
});
it('merges a local text correction with a remote timing correction on the same row',async()=>{
 const base=[{segment_id:'one',text:'Una frase',start:1,end:3}];
 const {stored,patches}=await persistedRebase(base,[{...base[0],text:'Una frase corregida'}],[{...base[0],end:4}]);
 expect(patches).toHaveLength(2);expect(patches[1].base_revision).toBe(2);
 expect(stored[0]).toMatchObject({text:'Una frase corregida',start:1,end:4});
});

it('reconciles further typing on an inserted line after its save commits but the response is lost',async()=>{
 const base=[{segment_id:'existing',text:'Existing line',start:1,end:2}];
 const inserted={segment_id:'n-synthetic-insertion-1',text:'Initial insertion',start:3,end:4};
 let stored=base,revision=1;const patches=[];
 const request=vi.fn(async(path,options={})=>{
  if(path.endsWith('/lock/heartbeat'))return reply({acquired:true});
  if(options.method==='DELETE')return reply({released:true});
  if(!options.method)return reply({job_id:'synthetic-lost-insertion',revision,segments:stored});
  const body=JSON.parse(options.body);patches.push(body);
  expect(body.base_revision).toBe(revision);
  stored=body.segments;revision++;
  if(patches.length===1)throw new TypeError('Synthetic response lost after commit');
  return reply({revision,applied:true});
 });
 const {result}=renderHook(()=>useEditorDocument({jobId:'synthetic-lost-insertion',enabled:true,request}));
 await waitFor(()=>expect(result.current.loading).toBe(false));
 let save,recovery;
 await act(async()=>{save=await result.current.save([...base,inserted]);});
 expect(save.ok).toBe(false);
 await act(async()=>{recovery=await result.current.reconcile([...base,{...inserted,text:'Further typing after insertion'}]);});
 expect(recovery.mergedSegments).toHaveLength(2);
 await act(async()=>{expect((await result.current.save(recovery.mergedSegments)).ok).toBe(true);});
 expect(patches.map(body=>body.base_revision)).toEqual([1,2]);
 expect(stored.filter(row=>row.segment_id===inserted.segment_id)).toEqual([expect.objectContaining({...inserted,text:'Further typing after insertion'})]);
 expect(stored[0]).toMatchObject(base[0]);
});
