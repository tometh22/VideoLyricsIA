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
