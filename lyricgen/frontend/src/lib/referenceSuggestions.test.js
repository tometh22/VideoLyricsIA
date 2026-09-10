import { expect, it } from 'vitest';
import { findReferenceSuggestion, referenceSuggestionsById } from './referenceSuggestions';
it('declines unrelated phrases and chorus alternatives even with shared words', () => {
 expect(findReferenceSuggestion('Calavera, ropa y raja y tú te miras', ['Calavera, ropa y raja y tu te miras'])).toBeNull();
 expect(findReferenceSuggestion('Vos sos Román', ['Vos sos Dios', 'Vos sos Gardel'])).toBeNull();
 expect(findReferenceSuggestion('Y en la tumba de guardias de los desiertos', ['Y un quejido de fueye que tocás tan solo vos'])).toBeNull();
 expect(findReferenceSuggestion('I know my darling', ['Vos hablando y yo tratando de escucharte'])).toBeNull();
});
it('offers bounded orthography while declining ambiguity', () => {
 expect(findReferenceSuggestion('Vos sos Roman', ['Vos sos Román'])).toBe('Vos sos Román');
 expect(findReferenceSuggestion('Yo cantando bajo la lluvis', ['Yo cantando bajo la lluvia'])).toBe('Yo cantando bajo la lluvia');
 expect(findReferenceSuggestion('Yo cantando bajo la lluvis', ['Yo cantando bajo la lluvia','Yo cantando bajo la lluvio'])).toBeNull();
});
it('keys current suggestions by stable identity after insertion/reordering/edits', () => {
 const rows=[{_id:'inserted',text:'Otra frase'},{_id:'stable-b',text:'Vos sos Roman'}];
 expect(referenceSuggestionsById(rows,['Vos sos Román'])).toEqual({'inserted':null,'stable-b':'Vos sos Román'});
 rows[1].text='Vos sos Dios';
 expect(referenceSuggestionsById(rows,['Vos sos Román'])['stable-b']).toBeNull();
});
