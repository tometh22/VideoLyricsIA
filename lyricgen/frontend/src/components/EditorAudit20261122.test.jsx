import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import LyricsEditor from './LyricsEditor';
import { segmentsStore } from '../state/segmentsStore';
import { resolveLegacyDraft } from '../lib/reviewRecovery';
vi.mock('../i18n',()=>({useI18n:()=>({t:()=>undefined})}));
vi.mock('./OnboardingTour',()=>({EditorTour:()=>null}));
vi.mock('./ToastProvider',()=>({useToast:()=>({toast:vi.fn(),dismiss:vi.fn()})}));
afterEach(()=>{cleanup();segmentsStore._clearAll();localStorage.clear();vi.restoreAllMocks();});
const rows=[{segment_id:'one',start:0,end:2,text:'Una frase'}];
it('persists deletion of the final legacy row',async()=>{
 const persist=vi.fn().mockResolvedValue({ok:true,revision:1});
 render(<LyricsEditor segments={rows} transcribeJobId='audit-empty' filename='synthetic.wav'
 requireLineReview disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} onPersistSegments={persist}/>);
 fireEvent.click(screen.getByRole('button',{name:'Eliminar línea 1',exact:true}));
 expect(segmentsStore.get('audit-empty')).toHaveLength(0);
 await waitFor(()=>expect(persist).toHaveBeenCalledWith('audit-empty',[],expect.anything()));
});
it('restores a deliberately empty draft after reload',()=>{
 expect(resolveLegacyDraft({draft:{segments:[],base_revision:4},currentSegments:rows,currentRevision:4}))
 .toMatchObject({action:'restore',segments:[]});
});
it('Space pauses audio after selecting a campaign sidebar row',()=>{
 const {container}=render(<LyricsEditor segments={rows} transcribeJobId='audit-space' filename='synthetic.wav'
 requireLineReview disableAutosave disableBeforeUnload audioUrl='https://example.test/synthetic.wav'
 onApprove={vi.fn()} onBack={vi.fn()}/>);
 const audio=container.querySelector('audio');let paused=true;
 Object.defineProperty(audio,'duration',{configurable:true,value:10});
 Object.defineProperty(audio,'readyState',{configurable:true,value:4});
 Object.defineProperty(audio,'paused',{configurable:true,get:()=>paused});
 vi.spyOn(audio,'play').mockImplementation(()=>{paused=false;fireEvent.play(audio);return Promise.resolve();});
 const pause=vi.spyOn(audio,'pause').mockImplementation(()=>{paused=true;fireEvent.pause(audio);});
 fireEvent.loadedMetadata(audio);
 const row=screen.getByTestId('review-line-1');row.focus();fireEvent.click(row);
 expect(paused).toBe(false);
 fireEvent.keyDown(row,{key:' ',code:'Space'});
 expect(pause).toHaveBeenCalled();
 expect(paused).toBe(true);
});

it('does not persist an empty lifecycle sentinel after releasing a legacy editor store',async()=>{
 vi.useFakeTimers();
 try {
  const persist=vi.fn().mockResolvedValue({ok:true,revision:1});
  render(<LyricsEditor segments={rows} transcribeJobId='audit-evict' filename='synthetic.wav'
   disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} onPersistSegments={persist}/>);
  fireEvent.change(screen.getByDisplayValue('Una frase'),{target:{value:'Pending edit'}});
  const {act}=await import('@testing-library/react');
  act(()=>segmentsStore.evict('audit-evict'));
  await act(async()=>{await vi.advanceTimersByTimeAsync(3100);});
  expect(persist).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
