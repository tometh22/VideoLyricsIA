import { cleanup, fireEvent, render as rtlRender, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import LyricsTimeline from './LyricsTimeline';
import { I18nProvider } from '../i18n';
const render = ui => rtlRender(<I18nProvider>{ui}</I18nProvider>);
afterEach(cleanup);
const rows=[{_id:1,start:1,end:3,text:'Una frase muy larga que debe mostrarse entera en la barra lateral sin recortar ninguna de sus palabras'}];
it('edits from the sidebar, flushes on Enter and returns focus without scrolling',()=>{
 const start=vi.fn(),commit=vi.fn(),change=vi.fn(),play=vi.fn();
 render(<LyricsTimeline segments={rows} duration={20} onTextEditStart={start} onTextEditCommit={commit} onTextChange={change} onTogglePlayback={play}/>);
 const label=screen.getByTestId('timeline-label-row');
 expect(label.querySelector('.truncate')).toBeNull();
 fireEvent.doubleClick(label.querySelector('button'));
 const input=screen.getByRole('textbox');
 fireEvent.change(input,{target:{value:'Letra corregida'}});
 fireEvent.keyDown(input,{key:' ',code:'Space'});
 expect(play).not.toHaveBeenCalled();
 fireEvent.keyDown(input,{key:'Enter'});
 expect(commit).toHaveBeenCalledExactlyOnceWith(1,'Letra corregida');
 expect(change).toHaveBeenCalledExactlyOnceWith(1,'Letra corregida');
 expect(screen.queryByRole('textbox')).toBeNull();
 fireEvent.keyDown(screen.getByLabelText('Studio de tiempos'),{key:' ',code:'Space'});
 expect(play).toHaveBeenCalledTimes(1);
});
it('preserves native toolbar activation and ignores sustained Space presses',()=>{
 const play=vi.fn();render(<LyricsTimeline segments={rows} onTogglePlayback={play}/>);
 fireEvent.keyDown(screen.getByRole('button',{name:'Agregar línea en el cursor'}),{key:' ',code:'Space'});
 expect(play).not.toHaveBeenCalled();
 const segment=screen.getByTestId('timeline-segment');
 fireEvent.keyDown(segment,{key:' ',code:'Space'});
 fireEvent.keyDown(segment,{key:' ',code:'Space',repeat:true});
 expect(play).toHaveBeenCalledTimes(1);
});
it('chooses an exact insertion time before asking the parent to insert',()=>{
 const add=vi.fn(()=>2);render(<LyricsTimeline segments={rows} currentTime={7} duration={20} onAddAtTime={add}/>);
 fireEvent.click(screen.getByRole('button',{name:'Agregar línea en el cursor'}));
 fireEvent.change(screen.getByLabelText('Inicio de la línea nueva en segundos'),{target:{value:'8.25'}});
 fireEvent.click(screen.getByRole('button',{name:'Insertar y escribir'}));
 expect(add).toHaveBeenCalledExactlyOnceWith(8.25);
});
it('Escape closes a reconciled inline edit without restoring old remote text',()=>{
 const change=vi.fn();const view=render(<LyricsTimeline segments={rows} onTextChange={change}/>);
 fireEvent.doubleClick(screen.getByTestId('timeline-label-row').querySelector('button'));
 view.rerender(<I18nProvider><LyricsTimeline segments={[{...rows[0],text:'Corrección remota'}]} onTextChange={change}/></I18nProvider>);
 fireEvent.keyDown(screen.getByRole('textbox'),{key:'Escape'});
 expect(screen.queryByRole('textbox')).toBeNull();
 expect(change).not.toHaveBeenCalled();
});
it('resizes the sidebar by dragging without changing lyrics or timings',()=>{
 const change=vi.fn(),timing=vi.fn();render(<LyricsTimeline segments={rows} onTextChange={change} onTimingChange={timing}/>);
 const handle=screen.getByRole('separator',{name:'Ajustar ancho de la barra lateral'});
 expect(handle).toHaveAttribute('aria-valuenow','264');
 fireEvent.pointerDown(handle,{button:0,pointerId:7,clientX:264});
 fireEvent.pointerMove(handle,{pointerId:7,clientX:364});
 fireEvent.pointerUp(handle,{pointerId:7,clientX:364});
 expect(handle).toHaveAttribute('aria-valuenow','364');
 expect(screen.getByTestId('timeline-lane')).toHaveStyle({left:'364px'});
 expect(change).not.toHaveBeenCalled();expect(timing).not.toHaveBeenCalled();
 fireEvent.doubleClick(handle);expect(handle).toHaveAttribute('aria-valuenow','264');
});
it('supports keyboard bounds and cancels an interrupted sidebar drag',()=>{
 render(<LyricsTimeline segments={rows}/>);
 const handle=screen.getByRole('separator');
 fireEvent.keyDown(handle,{key:'Home'});expect(handle).toHaveAttribute('aria-valuenow','180');
 fireEvent.keyDown(handle,{key:'ArrowRight'});expect(handle).toHaveAttribute('aria-valuenow','200');
 fireEvent.pointerDown(handle,{button:0,pointerId:7,clientX:200});
 fireEvent.pointerMove(handle,{pointerId:7,clientX:500});
 fireEvent.pointerCancel(handle,{pointerId:7});expect(handle).toHaveAttribute('aria-valuenow','200');
 fireEvent.keyDown(handle,{key:'End'});expect(handle).toHaveAttribute('aria-valuenow',handle.getAttribute('aria-valuemax'));
});

it('removes the anchored editor when the parent blocks interaction without an extra commit',()=>{
 const change=vi.fn(),commit=vi.fn();
 const props={segments:rows,onTextChange:change,onTextEditCommit:commit};
 const view=render(<LyricsTimeline {...props}/>);
 fireEvent.doubleClick(screen.getByTestId('timeline-label-row').querySelector('button'));
 fireEvent.change(screen.getByRole('textbox'),{target:{value:'Última edición humana'}});
 view.rerender(<I18nProvider><LyricsTimeline {...props} interactionBlocked/></I18nProvider>);
 expect(screen.queryByRole('textbox')).toBeNull();
 expect(change).toHaveBeenCalledExactlyOnceWith(1,'Última edición humana');
 expect(commit).not.toHaveBeenCalled();
 fireEvent.doubleClick(screen.getByTestId('timeline-label-row').querySelector('button'));
 expect(screen.queryByRole('textbox')).toBeNull();
});

it('commits the anchored editor on Tab and returns keyboard focus to the workspace',()=>{
 const commit=vi.fn();render(<LyricsTimeline segments={rows} onTextEditCommit={commit}/>);
 fireEvent.doubleClick(screen.getByTestId('timeline-label-row').querySelector('button'));
 fireEvent.keyDown(screen.getByRole('textbox'),{key:'Tab'});
 expect(commit).toHaveBeenCalledExactlyOnceWith(1,rows[0].text);
 expect(screen.queryByRole('textbox')).toBeNull();
 expect(screen.getByLabelText('Studio de tiempos')).toHaveFocus();
});
