import { describe, expect, it } from 'vitest';
import { updateSegmentTiming } from './segmentWords';
const row = { start: 1, end: 3, text: 'hola mundo', words: [
  { word: 'hola', start: 1, end: 2, confidence: .9 }, { word: 'mundo', start: 2, end: 3 },
] };
describe('word clocks follow operator timing edits', () => {
  it('translates real word clocks for a pure move without mutating the source', () => {
    const before = structuredClone(row);
    expect(updateSegmentTiming(row, 4, 6).words).toEqual([
      { word: 'hola', start: 4, end: 5, confidence: .9 }, { word: 'mundo', start: 5, end: 6 },
    ]);
    expect(row).toEqual(before);
  });
  it('keeps acoustic evidence on an outward resize if every word still fits', () => {
    expect(updateSegmentTiming(row, .5, 3.5).words).toEqual(row.words);
  });
  it('invalidates evidence clipped by a resize instead of saving impossible clocks', () => {
    expect(updateSegmentTiming(row, 1.5, 2.5)).not.toHaveProperty('words');
  });
  it.each([null, [{word:'hola',start:null,end:2}], [{word:'hola',start:NaN,end:2}], [{word:'hola',start:2,end:1}]])('rejects malformed metadata %j', words => {
    expect(updateSegmentTiming({...row,words},1,3)).not.toHaveProperty('words');
  });
});
