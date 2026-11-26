import { describe, it, expect } from "vitest";
import { proposeRepeatTiming, repeatTimingChanges } from "./repeatTiming";
const rows = [{ _id: 1, segment_id: 'a', text: 'mismo estribillo', start: 1, end: 2 }, { _id: 2, segment_id: 'b', text: 'mismo estribillo', start: 8, end: 9 }, { _id: 3, text: 'otro', start: 12, end: 13 }];
describe('optional repeated timing corrections', () => {
 it('keeps each occurrence onset, requires selection and binds both snapshots', () => {
  const proposal = proposeRepeatTiming(rows, 1, 1, 3, 20);
  const current = rows.map(row => row._id === 1 ? { ...row, end: 3, locked: true } : row);
  expect(repeatTimingChanges(current, proposal, [], 20)).toEqual([]);
  expect(repeatTimingChanges(current, proposal, [2], 20)).toMatchObject([{ id: 2, start: 8, end: 10 }]);
  expect(repeatTimingChanges(current.map(row => row._id === 2 ? { ...row, text: 'human edit' } : row), proposal, [2], 20)).toEqual([]);
  expect(repeatTimingChanges(current.map(row => row._id === 1 ? { ...row, end: 4 } : row), proposal, [2], 20)).toEqual([]);
 });
 it('excludes collisions, blank text, past occurrences and audio overflow', () => {
  expect(proposeRepeatTiming(rows, 1, 1, 7, 20)).toBeNull();
  expect(proposeRepeatTiming(rows, 1, 1, 3, 9)).toBeNull();
  expect(proposeRepeatTiming(rows, 2, 8, 10, 20)).toBeNull();
  expect(proposeRepeatTiming(rows.map(row => ({...row,text:''})), 1, 1, 3, 20)).toBeNull();
 });
 it('rechecks space after an intervening insertion', () => {
  const proposal = proposeRepeatTiming(rows, 1, 1, 3, 20);
  const current = rows.map(row => row._id === 1 ? {...row,end:3,locked:true}:row);
  expect(repeatTimingChanges([...current,{_id:4,start:9.5,end:10.5}],proposal,[2],20)).toEqual([]);
 });
});

it('binds the retimed source after an incompatible word window was invalidated', async () => {
 const {updateSegmentTiming}=await import('./segmentWords');
 const original=rows.map(row=>row._id===1?{...row,words:[{word:'mismo',start:1,end:1.4},{word:'estribillo',start:1.4,end:2}]}:row);
 const proposal=proposeRepeatTiming(original,1,1,1.5,20);
 const current=original.map(row=>row._id===1?{...updateSegmentTiming(row,1,1.5),locked:true}:row);
 expect(current[0].words).toBeUndefined();
 expect(repeatTimingChanges(current,proposal,[2],20)).toMatchObject([{id:2,start:8,end:8.5}]);
});
