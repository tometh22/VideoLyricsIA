import { describe, expect, it } from "vitest";
import { approvalConflict } from "./approvalSnapshot";
describe("approval is validation, never timing repair", () => {
  it("preserves the two historical 40ms clamp cases including locked", () => {
    const rows = [{start:90.2229,end:95.23,text:"line",locked:true},
      {start:95.24,end:97.42,text:"next"},
      {start:159.86,end:162.83,text:"line"},
      {start:162.84,end:164.8958,text:"next"}];
    const before = structuredClone(rows);
    expect(approvalConflict(rows)).toBeNull();
    expect(rows).toEqual(before);
  });
  it("rejects an actual overlap without changing even a locked row", () => {
    const rows=[{start:1,end:3,text:"first",locked:true},{start:2.9,end:4,text:"second"}];
    expect(approvalConflict(rows)).toMatch(/se solapan/);
    expect(rows[0].end).toBe(3);
  });
  it("allows adjacency and ignores only sub-resolution float representation", () => {
    expect(approvalConflict([{start:0,end:1.00000001,text:"a"},{start:1,end:2,text:"b"}])).toBeNull();
    expect(approvalConflict([{start:0,end:1.0001,text:"a"},{start:1,end:2,text:"b"}])).toMatch(/se solapan/);
  });
});
